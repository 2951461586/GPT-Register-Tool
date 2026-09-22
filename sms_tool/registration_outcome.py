"""
注册结果判定模块。

从 registration.py 解耦出来，包含：
- 账号创建阶段的错误归一化（_create_account_error）
- AT 稳定性探测（_probe_registration_access_token）
- 注册链路是否依赖 refresh_token / 手机验证码的开关（_requires_* 两个小函数）
- 「已创建但拿不到 session 且无密码」的显式分流
  （needs_manual_session_recovery —— 方案 B，2026-09-16）

这些函数与主流程的 register_loop 主入口解耦，便于单独测试或复用。
"""

import time
from collections.abc import Mapping

from .accounts.account_liveness import probe_account_liveness
from .config import CFG
from .error_classification import classify_error
from .registration_policy import registration_retry_decision
from .registration_progress import registration_stage
from .registration_result import build_registration_failure_result
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text
from .utils import _timing_summary


def _create_account_error(create_ok, create_data):
    """从 create_account 响应中提炼出人类可读的错误码/消息。"""
    if create_ok:
        return ""
    create_error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    create_code = str(create_error.get("code") or "").strip()
    create_message = str(create_error.get("message") or "").strip()
    error = "create_account_failed"
    if create_code:
        error += f":{create_code}"
    if create_message:
        error += f": {create_message}"
    return error


def _existing_account_error(create_data):
    """The address is already registered, and that is the cause worth reporting.

    ``registration_handlers.create_account`` deliberately flips ``create_ok`` to
    ``True`` on ``user_already_exists`` -- "the account exists, so *creating* it
    is not what failed". The side effect is that ``_create_account_error``
    returns ``""`` and the reported cause falls through to whatever the *re-login*
    fallback last hit.

    Measured 2026-09-14: the fallback sends a second OTP for the same mailbox and
    that send is rate-limited, so three addresses registered back on 09-06 were
    reported as ``existing_login_otp_send_failed:429`` (classified ``unknown``)
    while the signup lane had already answered ``user_already_exists``. The audit
    trail blamed the OTP layer for an already-registered mailbox, and the
    ``auth_state``-shaped symptoms retried the address -- one more OTP each time.

    ``user_already_exists`` is classified ``account`` (not retryable, batch
    dropped): no amount of retrying turns a used address into a free one, and
    every retry spends another OTP on it.

    The recovery action is appended when present because it is the only field
    that says what the caller is supposed to do next.
    """
    if not isinstance(create_data, dict):
        return ""
    error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    if str(error.get("code") or "").strip() != "user_already_exists":
        return ""
    recovery = error.get("userAlreadyExistsRecovery")
    action = ""
    if isinstance(recovery, dict):
        action = str(recovery.get("action") or "").strip()
    return "existing_account_user_already_exists" + (f":{action}" if action else "")


def _probe_registration_access_token(
    access_token,
    auth_session,
    proxy=None,
    *,
    cfg=None,
    probe_fn=None,
    stage_fn=None,
    sleep_fn=None,
):
    """
    多轮 AT 稳定性探测。

    连续 count 次探测 access_token 可用性，所有探测都 200 才算 AT 稳定；
    中间任意一轮非 200 立即返回，并附带每轮的 status_code 向量。
    """
    runtime_cfg = cfg if isinstance(cfg, Mapping) else CFG
    registration_value = runtime_cfg.get("registration")
    registration_cfg = registration_value if isinstance(registration_value, Mapping) else {}
    probe_fn = probe_fn or probe_account_liveness
    stage_fn = stage_fn or registration_stage
    sleep_fn = sleep_fn or time.sleep
    try:
        timeout = max(5, min(int(registration_cfg.get("at_probe_timeout_seconds") or 30), 120))
    except (TypeError, ValueError):
        timeout = 30
    try:
        count = max(1, min(int(registration_cfg.get("at_stability_probe_count") or 2), 3))
    except (TypeError, ValueError):
        count = 2
    try:
        delay = max(0.0, min(float(registration_cfg.get("at_stability_probe_delay_seconds") or 10), 60.0))
    except (TypeError, ValueError):
        delay = 10.0
    probes = []
    for index in range(count):
        probe = probe_fn(
            {"access_token": access_token, "auth_session": auth_session or {}},
            proxy=proxy,
            timeout=timeout,
        )
        probes.append(probe)
        if int(probe.get("status_code") or 0) != 200:
            break
        if index + 1 < count and delay:
            stage_fn("access_token_stability_wait")
            sleep_fn(delay)
            stage_fn("access_token_probe")
    result = dict(probes[-1] if probes else {})
    result["stability_probe_count"] = len(probes)
    result["stability_status_codes"] = [int(item.get("status_code") or 0) for item in probes]
    result["stability_window_seconds"] = round(delay * max(0, len(probes) - 1), 3)
    return result


def _retain_registration_checkpoint(success, access_token, at_probe):
    """Keep a post-create checkpoint when only the AT transport probe failed.

    The account and token already exist at this point.  Clearing the checkpoint
    forces the batch retry to submit the signup flow a second time, which turns
    a transient proxy failure into ``invalid_state`` or a duplicate signup.
    """
    if success or not str(access_token or "").strip():
        return False
    probe = at_probe if isinstance(at_probe, Mapping) else {}
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    return status_code == 0


def _registration_requires_refresh_token(runtime_cfg=None):
    """协议注册链路是否要求最终产出的 session 必须包含 refresh_token。"""
    source = runtime_cfg if isinstance(runtime_cfg, Mapping) else CFG
    value = source.get("codex_oauth")
    cfg = value if isinstance(value, Mapping) else {}
    return bool(cfg.get("require_registration_refresh_token", True))


def _registration_requires_phone_verification(phone_pool=None, runtime_cfg=None):
    """协议注册链路是否要求手机二次校验（默认：有 phone_pool 则开启）。"""
    source = runtime_cfg if isinstance(runtime_cfg, Mapping) else CFG
    value = source.get("codex_oauth")
    cfg = value if isinstance(value, Mapping) else {}
    default = bool(phone_pool)
    return bool(cfg.get("require_registration_phone_verification", default))


def _mailbox_snapshot(mailbox):
    if not mailbox:
        return {}
    return {
        "email": getattr(mailbox, "email", ""),
        "password": getattr(mailbox, "password", ""),
        "login_password": getattr(mailbox, "login_password", ""),
        "refresh_token": getattr(mailbox, "refresh_token", ""),
        "access_token": getattr(mailbox, "access_token", ""),
        "source": getattr(mailbox, "source", ""),
        "provider": getattr(mailbox, "provider", ""),
        "order_no": getattr(mailbox, "order_no", ""),
        "token": getattr(mailbox, "token", ""),
        "client_secret": getattr(mailbox, "client_secret", ""),
        "auth_mode": getattr(mailbox, "auth_mode", ""),
        "sender_name": getattr(mailbox, "sender_name", ""),
        "purchase_id": getattr(mailbox, "purchase_id", ""),
        "project_name": getattr(mailbox, "project_name", ""),
        "price": getattr(mailbox, "price", ""),
        "purchase_total_cost": getattr(mailbox, "purchase_total_cost", ""),
        "balance_after": getattr(mailbox, "balance_after", ""),
    }


def _browser_mailbox_snapshot(mailbox):
    """Return the non-secret mailbox metadata allowed in failed browser results.

    ``_mailbox_snapshot`` is also used by internal checkpoints and OAuth
    hand-off code, where mailbox credentials may still be needed. Successful
    browser results retain that snapshot until ``build_session_file`` persists
    the canonical artifact; failures use this allow-list because they are only
    operator-visible workflow payloads.
    """
    if not mailbox:
        return {}
    fields = (
        "email",
        "source",
        "provider",
        "order_no",
        "auth_mode",
        "sender_name",
        "purchase_id",
        "project_name",
        "price",
        "purchase_total_cost",
        "balance_after",
    )
    return {
        key: value
        for key in fields
        if (value := getattr(mailbox, key, "")) not in (None, "")
    }


def needs_manual_session_recovery(
    *,
    success,
    access_token="",
    existing_account=False,
    existing_account_password_known=False,
) -> bool:
    """True when the address exists server-side but we hold neither a session nor its password.

    This is 方案 B of ``docs/audits/plan-2026-09-16-partial-account-protocol-login.md``
    §五: the bucket of accounts that are **not failures** -- they exist -- but that
    no code path of ours can turn into a session, so the only move left is a human
    logging in once.  The point of naming it separately is that a panel can then
    say "these are not failures, they are waiting for one manual login" instead of
    showing them as 82 indistinguishable errors.

    Three facts, all already recorded, and none inferable from the others:

    * **Created** -- ``existing_account``.  Set only where ``create_account``
      answered ``user_already_exists``.  ``registration_state`` is
      ``partial_registered`` for exactly this flag, so this bucket is a *subset*
      of the half-registered rows, not a new population.
    * **No session** -- ``access_token`` is empty.  That also covers the
      ``not success`` half: ``_registration_outcome`` only reports success with a
      non-empty token *and* a 200 probe, so a token implies success.
    * **No password** -- ``existing_account_password_known`` is False.  This is
      the discriminator that makes the bucket actionable, and it is deliberately
      **not** ``password_unknown``: that flag is set for *every*
      ``user_already_exists`` (it is persistence hygiene -- see
      ``RegistrationAccount.existing_account_password_known``), so keying on it
      would sweep in the addresses whose password we do hold.  Those are
      recoverable for free by the password login lane, so calling them "needs a
      human" would be a false alarm on every one.

    Deliberately **not** keyed on ``registration_state``: ``finalize`` computes
    that string from the same flags, and ``_abort_result`` overwrites it with
    ``cancelled`` / ``partial_registered`` after the fact -- reading it back would
    make the bucket depend on which exit path the run happened to take.
    """
    if success or str(access_token or "").strip():
        return False
    return bool(existing_account) and not bool(existing_account_password_known)


def _failure_result(
    error,
    email="",
    mailbox=None,
    password="",
    *,
    existing_account=False,
    existing_account_password_known=False,
    access_token="",
):
    """协议路径失败装配 —— 2026-09-13 起走 ADR-0008 共享契约。

    此前这里手拼一份最小失败 dict（键集与浏览器路径漂移）。现在统一经由
    ``build_registration_failure_result``：完整 COMMON_RESULT_KEYS + 重试决策，
    协议路径专有的 timing 通过 extra 保留，密码保持历史脱敏语义。

    2026-09-16 起额外带 ``needs_manual_session_recovery``（方案 B）。三个原始事实
    走关键字参数而不是一个现成的 bool，是为了让判据只有
    :func:`needs_manual_session_recovery` 一个 owner —— 否则 ``finalize`` 那条装配
    路径会把同一个判据再抄一遍。``phone_registration`` 等调用方不传，默认全 False，
    即「不声称需要人工」，这对没有 ``existing_account`` 概念的手机泳道是对的。
    """
    decision = registration_retry_decision(error)
    result = build_registration_failure_result(
        error=_sanitize_text(error),
        failure_class=decision.failure_class,
        email=email,
        extra={
            "timing": _timing_summary(),
            "retryable": decision.retryable,
            "error_advice": decision.advice,
            "password": "[REDACTED]" if password else "",
            "mailbox": _mailbox_snapshot(mailbox) or {},
            "needs_manual_session_recovery": needs_manual_session_recovery(
                success=False,
                access_token=access_token,
                existing_account=existing_account,
                existing_account_password_known=existing_account_password_known,
            ),
        },
    )
    return _sanitize(result)


def _registration_outcome(create_ok, create_data, access_token, at_probe, existing_login_error=""):
    """Decide whether a registration produced a usable access token.

    ``existing_login_error`` carries the *cause* when an already-registered
    address could not be logged back in. Without a cause the outcome collapses to
    the generic ``missing_auth_session_access_token``, which hides a retryable
    ``invalid_state`` behind a name that suggests a code defect -- so the cause is
    always preferred over it.

    Precedence: a genuine ``create_account`` failure, then
    ``user_already_exists`` (the address is used; the re-login fallback's OTP
    symptom is only its consequence), then ``existing_login_error``.

    The generic name is itself classified ``auth_state`` (retryable) since
    2026-09-13: it used to be ``unknown``, which reads as terminal, and 18 of the
    179 failures over 09-08..09-13 ended on it -- protocol runs that reached
    ``finalize`` with no access token from the auth session.
    """
    probe = at_probe if isinstance(at_probe, dict) else {}
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    create_error = _create_account_error(create_ok, create_data or {})
    # ``user_already_exists`` is invisible to ``_create_account_error`` because
    # the handler marks ``create_ok`` True for it -- see
    # ``_existing_account_error``.  It must outrank the re-login fallback's
    # symptom, which is a *consequence* of the address already being registered.
    existing_account_error = _existing_account_error(create_data or {})
    success = bool(str(access_token or "").strip()) and status_code == 200
    if success:
        return True, "", create_error
    if not str(access_token or "").strip():
        cause = create_error or existing_account_error or str(existing_login_error or "").strip()
        return False, cause or "missing_auth_session_access_token", ""
    if status_code:
        return False, f"access_token_probe_http_{status_code}", create_error
    probe_error = str(probe.get("error") or probe.get("status") or "unknown").strip()
    return False, f"access_token_probe_failed:{probe_error}", create_error


def _oauth_result_summary(result):
    if not isinstance(result, dict):
        return {}
    summary = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        summary["has_access_token"] = bool(tokens.get("access_token"))
        summary["has_refresh_token"] = bool(tokens.get("refresh_token"))
        summary["has_id_token"] = bool(tokens.get("id_token"))
    return summary
