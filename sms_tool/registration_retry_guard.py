"""Cross-batch retry circuit breaker for disposable mailbox registrations."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from .paths import runtime_file
from .cross_process_gate import cross_process_write_lock
from .registration_policy import RETRYABLE_CLASSES, registration_retry_decision
from .sanitizer import sanitize_text


_LOCK = threading.Lock()
PARTIAL_REGISTERED = "partial_registered"


def mailbox_registration_status(record: Mapping | None, *, known_partial: bool = False) -> str:
    record = record or {}
    status = str(record.get("status") or "").strip().casefold()
    if status == "registered":
        return "registered"
    if known_partial or status == PARTIAL_REGISTERED or record.get("registration_state") == PARTIAL_REGISTERED:
        return PARTIAL_REGISTERED
    return "unknown"


#: Marker for "``login_or_signup`` routed this signup to the login page".
#:
#: The **earliest** shape of "this address is already registered" (2026-09-16):
#: the server routes ``login_or_signup`` to ``/log-in/password`` and answers
#: ``user/register`` with 400 ``invalid_auth_step``.  Named after what was
#: observed, not after the verdict it implies -- see its entry in
#: ``DEAD_END_MARKERS``.
DEAD_END_SIGNUP_ROUTED_TO_LOGIN = "signup_routed_to_login"

# Failures that can never succeed on a retry, so a *cooldown* is the wrong
# tool: it expires and the address walks straight back into the same dead end.
#
# * ``user_already_exists`` -- the server has already stated this address is a
#   registered account; re-driving it only burns an email OTP on the way to
#   ``/about-you``.  Measured 2026-09-14: three such addresses cost 25 attempts /
#   2595s / ~25 OTPs in a single day, and every one of them ended at
#   ``/about-you`` (see ``2026-09-14.md`` §22/§23).
#
# * ``auth_session_recovery_*`` -- added 2026-09-16 (P1-2).  Raised by
#   ``_resume_post_create`` for a checkpoint whose account *was* created
#   (``create_ok``) but whose session can no longer be recovered.  Permanent by
#   construction: ``session_recovery_started_at`` is written once at
#   ``create_account`` and **never refreshed**, the TTL is only
#   ``SESSION_RECOVERY_TTL_SECONDS`` (900s), and ``session_recovery_attempts``
#   is incremented only on the branch that *passes* the gate -- so once one of
#   these fires it can never become recoverable again.
#
#   Before this they were merely non-retryable, so they fell into the ``else``
#   branch of :meth:`RegistrationRetryGuard.record`, which **deleted the row**:
#   no cooldown, no flag, no memory.  The address was re-driven on every single
#   batch, each time claiming a mailbox slot and aborting before any stage ran
#   (measured: one address 8 times, another 3 times).  It is the same shape as
#   the ``user_already_exists`` bug above -- an unretryable verdict that erased
#   its own evidence.
DEAD_END_MARKERS = (
    "user_already_exists",
    # ``identity_provider_mismatch`` is the **second** spelling of the same
    # verdict, and it arrives from ``create_account`` too.  Measured
    # 2026-09-15 07:33:37 (run ``b93f598d``): ``create_account`` answered 400
    # with
    #   {"error": {"code": "identity_provider_mismatch",
    #              "message": "You tried signing in as \"...\" using a password,
    #                          which is not the authentication method you used
    #                          during sign up. ..."}}
    # on an address that was already registered passwordless.  The message
    # asserts a signup record exists, so it is as permanent as
    # ``user_already_exists`` -- but only the structured ``code`` says so.
    # 🔴 Do **not** replace this with the message tail: ``record()`` truncates
    # ``last_error`` to 160 chars and the ``email_otp_validate:{"endpoint": …}``
    # prefix already spends 110+ of them (that string's tail is measurably cut).
    # Here the marker sits at offset ~22 of
    # ``create_account_failed:identity_provider_mismatch: …`` so it survives.
    "identity_provider_mismatch",
    # Added 2026-09-16 17:3x（P1）.  A **third** spelling of "this address is
    # already registered", and the earliest one yet: the server routes
    # ``login_or_signup`` straight to ``/log-in/password`` and then answers
    # ``user/register`` with 400 ``invalid_auth_step``.
    #
    # Measured on batches 34632 (11:00) and 28512 (17:22): every account whose
    # ``login_or_signup`` landed on ``/log-in/password`` failed with that code --
    # 31 landings, 31 failures, 0 exceptions.  The other landing
    # (``/email-verification``) succeeded, so the split is by address, not by
    # time window or exit.
    #
    # 🔴 The name states **only what was observed** -- the signup was routed to
    # the login page.  It does **not** claim ``user_already_exists``: the server
    # never said that in this shape, and recording it under that name would make
    # the ledger name an error string that never appeared.
    #
    # ⚠️ ``mark_dead_end`` silently falls back to ``user_already_exists`` for an
    # unrecognised reason, so this entry is what keeps the ledger honest.
    # Pinned by ``tests/test_user_register_response_contract.py``.
    DEAD_END_SIGNUP_ROUTED_TO_LOGIN,
    "auth_session_recovery_expired",
    "auth_session_recovery_exhausted",
    "auth_session_recovery_context_missing",
)


def _dead_end_reason(error: object) -> str:
    """Return the matched marker when ``error`` is a permanent dead end."""
    text = str(error or "").casefold()
    for marker in DEAD_END_MARKERS:
        if marker in text:
            return marker
    return ""


class RegistrationRetryGuard:
    """Track consecutive retryable failures per mailbox.

    The guard is intentionally separate from account storage: a failed signup
    must not create a misleading account row, while repeated attempts still
    need a durable cooldown across WPF/CLI invocations.

    A cooldown and a dead end are deliberately different states: the first is
    released by the clock, the second never is (see ``DEAD_END_MARKERS``).
    """

    RETRYABLE_CLASSES = RETRYABLE_CLASSES

    def __init__(
        self,
        config: Mapping | None = None,
        *,
        path: Path | None = None,
        threshold: int = 2,
        cooldown_seconds: int | None = None,
        otp_pending_quarantine_threshold: int | None = None,
        otp_pending_quarantine_seconds: int | None = None,
    ) -> None:
        registration = config.get("registration") if isinstance(config, Mapping) else {}
        registration = registration if isinstance(registration, Mapping) else {}
        retry_policy = registration.get("retry_policy")
        retry_policy = retry_policy if isinstance(retry_policy, Mapping) else {}
        self.path = path or runtime_file(config or {}, "registration_retry_guard.json")
        self.threshold = max(1, int(threshold or 2))
        configured_cooldown = retry_policy.get("cross_batch_cooldown_seconds", 1800)
        self.cooldown_seconds = max(
            60,
            int(configured_cooldown if cooldown_seconds is None else cooldown_seconds),
        )
        configured_quarantine = retry_policy.get("otp_pending_quarantine_threshold", 2)
        self.otp_pending_quarantine_threshold = max(
            2,
            int(
                configured_quarantine
                if otp_pending_quarantine_threshold is None
                else otp_pending_quarantine_threshold
            ),
        )
        # 🔴 2026-09-18 拍板：OTP-pending 隔离**有期限**，不再是永久。
        #
        # 原先 ``quarantined: true`` 配合 ``cooldown_until: 0`` 表示「永久」，
        # 那只在「服务端对这个**地址**永久拒绝派发」的假设下成立。而
        # ``passwordless_email_otp_send_pending`` 是**服务端事务状态**，完全
        # 可能只反映一次灰度 / 限流窗口 —— 窗口关闭后同一地址又能拿到码。
        # 实测（09-18 批次 29896）被隔离的地址首次 stuck 与本次相隔数小时、
        # 换了出口，是**地址属性**没错，但原先**没有任何机制让它们回来**。
        #
        # 到期语义是「**重新计时**」而不是「永久豁免」：读取侧把过期隔离视为
        # 未隔离（``_quarantine_active``），``record`` 同时把
        # ``otp_pending_count`` 归零 —— 否则下一次 stuck 会从旧计数继续累加、
        # 立刻又越过阈值，TTL 等于白加。
        #
        # 下限 60s 与 ``cooldown_seconds`` 同款：写 0 会让「到期时刻」与
        # 「写入时刻」重合，而 ``_quarantine_active`` 用 ``until <= 0`` 表示
        # 「未设 TTL」⇒ 语义会打架。
        configured_quarantine_ttl = retry_policy.get("otp_pending_quarantine_seconds", 86400)
        self.otp_pending_quarantine_seconds = max(
            60,
            int(
                configured_quarantine_ttl
                if otp_pending_quarantine_seconds is None
                else otp_pending_quarantine_seconds
            ),
        )

    @staticmethod
    def _email(email: object) -> str:
        return str(email or "").strip().casefold()

    def _quarantine_active(self, row: Mapping | None, now: float) -> bool:
        """True when an OTP-pending quarantine is still inside its TTL window.

        **单一 owner**：``check`` / ``quarantined_emails`` /
        ``blocked_email_states`` / ``record`` 四个读点全部走这里，所以它们不会
        各自漂移（曾经的形态是四处各自 ``row.get("quarantined")``）。

        ``quarantine_until`` 缺失时回落到
        ``last_attempt_at + otp_pending_quarantine_seconds`` —— 这样**加 TTL
        之前**写下的记录（09-18 及更早隔离的那些地址）也会自然到期，不需要
        手工清理脚本。只有两个字段都读不到才答 ``True``（保守：误放行一个地址
        要烧一个邮箱 OTP，宁可多关一会儿）。
        """
        if not isinstance(row, Mapping) or not row.get("quarantined"):
            return False
        until = float(row.get("quarantine_until") or 0)
        if until <= 0:
            last = float(row.get("last_attempt_at") or 0)
            until = last + self.otp_pending_quarantine_seconds if last > 0 else 0
        return until <= 0 or until > now

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, value: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(self.path)

    @contextmanager
    def _locked(self):
        with _LOCK, cross_process_write_lock(self.path.with_suffix(".lock"), timeout=10):
            yield

    def check(self, email: object) -> dict[str, object]:
        key = self._email(email)
        if not key:
            return {"deferred": False, "consecutive": 0, "remaining_seconds": 0}
        with self._locked():
            row = self._read().get(key) or {}
        now = time.time()
        until = float(row.get("cooldown_until") or 0) if isinstance(row, Mapping) else 0
        remaining = max(0, int(until - now))
        dead_end = bool(row.get("dead_end")) if isinstance(row, Mapping) else False
        # 过期隔离在这里被释放 —— 调用方不需要任何额外的清理步骤。
        quarantined = self._quarantine_active(row, now)
        return {
            # A dead end also reports ``deferred`` so any caller that only
            # understands the cooldown contract still refuses to attempt it --
            # the two states differ in *why* they are skipped, not in whether
            # the address may be driven again.
            "deferred": remaining > 0 or dead_end or quarantined,
            "dead_end": dead_end,
            "registration_status": PARTIAL_REGISTERED if dead_end else "",
            "dead_end_reason": str(row.get("dead_end_reason") or "") if isinstance(row, Mapping) else "",
            "quarantined": quarantined,
            "quarantine_reason": str(row.get("quarantine_reason") or "") if isinstance(row, Mapping) else "",
            "otp_pending_count": int(row.get("otp_pending_count") or 0) if isinstance(row, Mapping) else 0,
            "consecutive": int(row.get("consecutive") or 0) if isinstance(row, Mapping) else 0,
            "remaining_seconds": remaining,
            "failure_class": str(row.get("failure_class") or "") if isinstance(row, Mapping) else "",
        }

    def record(self, email: object, *, failure_class: str = "", error: str = "", success: bool = False) -> None:
        key = self._email(email)
        if not key:
            return
        with self._locked():
            data = self._read()
            previous = data.get(key) if isinstance(data.get(key), Mapping) else {}
            now = int(time.time())
            if previous.get("quarantined") and not self._quarantine_active(previous, now):
                # 隔离已过 TTL ⇒ 当作全新开始。**必须整行丢弃 previous**：只把
                # ``quarantined`` 当成 False 的话，``otp_pending_count`` 会从旧值
                # 继续累加，下一次 stuck 立刻又越过阈值重新隔离 —— TTL 白加。
                previous = {}
            if success:
                data.pop(key, None)
            elif previous.get("dead_end") or self._quarantine_active(previous, now):
                # A later failed attempt cannot erase a terminal guard verdict.
                return
            else:
                decision = registration_retry_decision(
                    error,
                    failure_class=str(failure_class or "").strip().lower(),
                )
                if decision.guard_action == "otp_pending":
                    pending_count = int(previous.get("otp_pending_count") or 0) + 1
                    quarantined = pending_count >= self.otp_pending_quarantine_threshold
                    data[key] = {
                        "consecutive": pending_count,
                        "otp_pending_count": pending_count,
                        "failure_class": str(failure_class or decision.failure_class)[:40],
                        "last_error": sanitize_text(error)[:160],
                        "last_attempt_at": now,
                        # 第一次观察给一个真正的跨批冷却；第二次隔离该地址，但
                        # **只在 TTL 窗口内**（判据见 ``_quarantine_active``）——
                        # 服务端可能只是在一个灰度窗口里拒绝派发，窗口关闭后
                        # 这个地址必须能自己回来，不需要人工介入。
                        "cooldown_until": int(now + self.cooldown_seconds)
                        if not quarantined else 0,
                        "quarantined": quarantined,
                        "quarantine_reason": "email_otp_send_stuck" if quarantined else "",
                        "quarantine_until": int(now + self.otp_pending_quarantine_seconds)
                        if quarantined else 0,
                    }
                elif decision.guard_action == "cooldown":
                    same_class = str(previous.get("failure_class") or "") == str(failure_class or "")
                    consecutive = int(previous.get("consecutive") or 0) + 1 if same_class else 1
                    data[key] = {
                        "consecutive": consecutive,
                        "failure_class": str(failure_class or "")[:40],
                        "last_error": sanitize_text(error)[:160],
                        "last_attempt_at": now,
                        "cooldown_until": int(now + self.cooldown_seconds)
                        if consecutive >= self.threshold else 0,
                    }
                elif decision.guard_action == "dead_end":
                    reason = _dead_end_reason(error)
                    if reason:
                        data[key] = {
                            "consecutive": int(previous.get("consecutive") or 0),
                            "failure_class": str(failure_class or "")[:40],
                            "last_error": sanitize_text(error)[:160],
                            "last_attempt_at": now,
                            "cooldown_until": 0,
                            "dead_end": True,
                            "registration_status": PARTIAL_REGISTERED,
                            "dead_end_reason": reason,
                        }
                    else:
                        data.pop(key, None)
                else:
                    data.pop(key, None)
            self._write(data)

    def dead_end_emails(self) -> set[str]:
        """Every address carrying a permanent dead-end verdict, in one read.

        Callers that filter a whole pool must not call :meth:`check` per
        address -- that re-reads the JSON file each time (791 addresses would
        mean 791 reads).  This reads once and returns the address set.
        """
        with self._locked():
            data = self._read()
        return {
            str(key).strip().casefold()
            for key, row in data.items()
            if isinstance(row, Mapping) and row.get("dead_end")
        }

    def quarantined_emails(self) -> set[str]:
        """Addresses isolated after repeated OTP dispatch pending verdicts.

        过期的隔离在这里就被释放，所以按池过滤的调用方不需要任何清理步骤。
        """
        now = time.time()
        with self._locked():
            data = self._read()
        return {
            str(key).strip().casefold()
            for key, row in data.items()
            if self._quarantine_active(row, now)
        }

    def blocked_email_states(self) -> dict[str, str]:
        """Read all cross-batch blocks once for candidate-pool filtering."""
        now = time.time()
        with self._locked():
            data = self._read()
        result: dict[str, str] = {}
        for key, row in data.items():
            if not isinstance(row, Mapping):
                continue
            normalized = str(key).strip().casefold()
            if row.get("dead_end"):
                result[normalized] = "dead_end"
            elif self._quarantine_active(row, now):
                result[normalized] = "otp_pending_quarantine"
            elif float(row.get("cooldown_until") or 0) > now:
                result[normalized] = "cooldown"
        return result

    def mark_dead_end(self, email: object, *, reason: str = "", error: str = "") -> None:
        """Permanently block an address the server already called registered.

        Used when the verdict arrives *before* a batch outcome exists --
        ``create_account`` answering ``user_already_exists`` -- so the next
        batch skips the address instead of spending an email OTP to rediscover
        the same fact.
        """
        key = self._email(email)
        if not key:
            return
        detail = sanitize_text(error or reason or "user_already_exists")
        marker = _dead_end_reason(reason) or _dead_end_reason(error) or "user_already_exists"
        with self._locked():
            data = self._read()
            previous = data.get(key) if isinstance(data.get(key), Mapping) else {}
            data[key] = {
                "consecutive": int(previous.get("consecutive") or 0),
                "failure_class": str(previous.get("failure_class") or "account")[:40],
                "last_error": detail[:160],
                "last_attempt_at": int(time.time()),
                "cooldown_until": 0,
                "dead_end": True,
                "registration_status": PARTIAL_REGISTERED,
                "dead_end_reason": marker,
            }
            self._write(data)


__all__ = ["RegistrationRetryGuard"]
