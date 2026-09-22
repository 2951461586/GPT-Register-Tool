"""Production stage runner and email-registration workflow."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

from .codex_oauth import collect_codex_oauth_tokens
from .auth_headers import current_auth_fingerprint, set_auth_fingerprint
from .auth_state import otp_dispatch_verdict, signup_lane_verdict
from .desktop_ipc import emit_event
from .failure_registry import (
    OTP_MAILBOX_SIDE_MARKER,
    OTP_NO_RESEND_MARKER,
    PASSWORDLESS_SIGNUP_CODE,
    is_passwordless_signup_mismatch,
)
# 纯谓词（只看配置 + 渠道名）⇒ 顶层 import，不走 ``r.`` 门面：门面是给**有状态
# 操作**做注入用的，把纯函数也塞进去只会让测试里的 ``Mock()`` 变成一个永真值。
from .otp_strategy import otp_resend_eligible
from .sanitizer import account_reference
from .telemetry import current_run_id
from .registration_cancel import RegistrationCancelled, ensure_not_cancelled
from .registration_outcome import needs_manual_session_recovery
from .registration_result import build_registration_result
from .registration_operations import RegistrationOperations
from .registration_retry_guard import DEAD_END_SIGNUP_ROUTED_TO_LOGIN, RegistrationRetryGuard
from .registration_runtime import RegistrationRuntimeState
from .mailbox_errors import MailboxEndpointUnavailableError
from .providers.mailbox_graph import MailboxAuthInvalidError
from . import registration_checkpoint
from . import registration_finalize as _registration_finalize
from .registration_state import (
    RegistrationContext,
    RegistrationStage,
    RegistrationStageOverrun,
    RegistrationState,
    RegistrationStateMachine,
    prepare_registration_context,
)


#: Existing-login lane failures that mean this address can never produce a
#: session for us, so the retry guard should skip it instead of spending an
#: email code to rediscover the same verdict.
#:
#: ``no_password_step`` is the probe's definitive answer -- the transaction
#: served a login form with no password input, so the account is passwordless.
#: ``password_step_unknown`` is the signup lane's refusal to guess: the address
#: is known-registered and the probe could not answer, and the email lane is a
#: measured dead end for that address.  ``password_required`` is deliberately
#: absent: that state is "the account has a password we do not hold", which a
#: later run supplying ``--password`` could still resolve, so blacklisting it
#: would skip an address we can actually log into.
EXISTING_LOGIN_DEAD_END_ERRORS = (
    "existing_login_no_password_step",
    "existing_login_password_step_unknown",
)


def _is_existing_login_dead_end_error(error: Any) -> bool:
    text = str(error or "")
    return any(text.startswith(marker) for marker in EXISTING_LOGIN_DEAD_END_ERRORS)


def _login_probe_password(state: Any) -> str:
    """The password the existing-login probe is allowed to submit, or ``""``.

    The probe can only *offer* the password step -- a login still needs a
    password to hand it, so every caller of
    ``_login_existing_account_with_email_otp`` has to answer "is the password we
    hold this account's own password?".  Answering it in three places invites
    three different answers, so it is answered once, here.

    ``password_unknown`` is the wrong gate on its own: ``create_account`` sets
    it for *every* ``user_already_exists`` answer, including the ones where the
    caller passed ``--password`` and we therefore do own the account's password
    (that case is recorded in ``existing_account_password_known``).  For a fresh
    registration the password is the one we just set, so it is submittable
    unless this run resumed an email verification without owning one.
    """
    password = str(getattr(state, "password", "") or "")
    if getattr(state, "existing_account", False):
        return password if getattr(state, "existing_account_password_known", False) else ""
    return "" if getattr(state, "password_unknown", False) else password


def _create_account_response_line(status: int, data: Any, sanitize: Callable[[Any], str]) -> str:
    """One line saying what the server *decided*, not everything it said.

    The 600-character budget exists for the failure case: ``user_already_exists``
    carries its ``userAlreadyExistsRecovery`` object past the 300-char mark, and
    that object is the only place the server states the recovery action
    (measured 2026-09-14).  A non-200 therefore still prints the raw body.

    A 200 does not.  There the budget goes to a URL the body repeats twice: the
    top-level ``continue_url`` in full, then ``page.payload.url`` with the same
    value cut off mid-query at character 182 (7/7 dumps measured 2026-09-14).
    That is unusable for a human *and* unparseable for a script, and the query
    string is a single-use OAuth code rather than a signal -- so a success prints
    the two fields the caller branches on, with the URL reduced to host + path.
    """
    try:
        rendered = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(data)
    if status != 200 or not isinstance(data, dict):
        return sanitize(rendered[:600])
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    page_type = str(page.get("type") or data.get("page_type") or "")
    target = str(data.get("continue_url") or "")
    if not page_type and not target:
        # An unrecognised 200 shape: keep the raw body rather than print a line
        # that says nothing about why the caller got here.
        return sanitize(rendered[:600])
    parts = [f"page.type={page_type or '?'}"]
    if target:
        parsed = urlsplit(target)
        parts.append(f"continue_url={parsed.scheme}://{parsed.netloc}{parsed.path}")
    error = data.get("error")
    if error:
        parts.append(f"error={json.dumps(error, ensure_ascii=False, default=str)[:200]}")
    return sanitize(" ".join(parts))


def _new_registration_session(proxy: str = "") -> Any:
    """Build the protocol session for one registration.

    P1-8: curl_cffi honours ``trust_env`` by default, so a machine-wide
    ``HTTP(S)_PROXY`` in the process environment silently *overrides* the
    per-account proxy set here -- every account would then exit through one
    shared IP, which defeats the whole proxy pool.  The preflight already pins
    this (``registration_preflight``), as do the other transports
    (``paypal_protocol``, ``gcash_transport``, ``phone_proxy``).  With no proxy
    configured we leave the default alone so an env-provided proxy still
    applies.
    """
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        session.trust_env = False
    return session


def _apply_protocol_fingerprint(ops: Any, config: Any, proxy: str) -> None:
    """Pick the pooled protocol fingerprint and bind *its* geo to this account.

    P1-1: the pool resolves the proxy's exit geo before handing back a profile
    (measuring it when the credential carries no region token).  This used to
    take only ``profile.name`` and drop the geo half, while the geo actually
    applied came from ``infer_proxy_country`` -- which only reads a region token
    in the proxy username.  A residential proxy has no such token, so it
    resolved to ``""`` and the account kept a US clock on, say, a Brazilian
    exit: the measurement was paid for and then ignored.
    """
    profile = None
    try:
        from .fingerprint_pool import shared_fingerprint_pool
        pool = shared_fingerprint_pool(config)
        if pool.size > 0:
            profile = pool.next(proxy)
    except Exception:
        profile = None
    if profile is not None:
        ops.set_fingerprint_geo(
            profile.country,
            timezone=profile.timezone,
            lang=profile.lang,
            lang_full=profile.lang_full,
        )
        set_auth_fingerprint(profile.name)
    else:
        from .paypal_proxy import infer_proxy_country
        ops.set_fingerprint_geo(infer_proxy_country(proxy))


class RegistrationPersistence(Protocol):
    """Persistence seam for registration checkpoints and device identity."""

    def save_checkpoint(self, email: str, state: str, payload: Mapping[str, Any], *, runtime_config: Mapping[str, Any] | None) -> Any: ...
    def upsert_account(self, payload: Mapping[str, Any], *, runtime_config: Mapping[str, Any] | None) -> Any: ...
    def get_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None) -> Mapping[str, Any]: ...
    def get_device_context(self, email: str) -> Mapping[str, Any]: ...
    def clear_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None) -> Any: ...


class StorageRegistrationPersistence:
    """Default adapter kept at the application seam, not inside stage logic."""

    def save_checkpoint(self, email, state, payload, *, runtime_config=None):
        from .storage import save_registration_checkpoint
        return save_registration_checkpoint(email, state, payload, runtime_config=runtime_config)

    def upsert_account(self, payload, *, runtime_config=None):
        from .storage import upsert_account
        return upsert_account(payload, runtime_config=runtime_config)

    def get_checkpoint(self, email, *, runtime_config=None):
        from .storage import get_registration_checkpoint
        return get_registration_checkpoint(email, runtime_config=runtime_config)

    def get_device_context(self, email):
        from .storage import get_device_context
        return get_device_context(email)

    def clear_checkpoint(self, email, *, runtime_config=None):
        from .storage import clear_registration_checkpoint
        return clear_registration_checkpoint(email, runtime_config=runtime_config)


class RegistrationStageRunner:
    """Run one production stage against a shared runtime and state machine.

    ``context`` is whatever the caller wants handlers to see; the email
    workflow passes its ``RegistrationRuntimeState`` (not a
    ``RegistrationContext``) and handlers mutate that same runtime. ``run_stage``
    is the only execution seam.
    """

    def __init__(
        self,
        context: Any,
        machine: RegistrationStateMachine,
    ) -> None:
        self.context = context
        self.machine = machine

    def run_stage(
        self,
        state: RegistrationState,
        handler: Callable[[], Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        return RegistrationStage(
            state,
            lambda _context: handler(),
            timeout_seconds=timeout_seconds,
        ).run(self.context, self.machine)


class RegistrationAbort(RuntimeError):
    """Expected workflow failure that is converted to a sanitized result."""


class RegistrationEmailWorkflow:
    """Ordered email-registration pipeline. Owns stage ordering; not payment/recovery.

    Stage-boundary contract (kept executable by the tests in
    ``tests/test_registration_*`` and by ``docs/architecture.md``):

    - ``run()`` is the ONLY place stages are sequenced; stages never call each
      other directly. Shared state flows through ``self.runtime``
      (``RegistrationRuntimeState``) and side effects through ``self.r``
      (``RegistrationOperations``, injected — a Mock in tests stays honest).
    - Each ``*_stage`` / stage-named method is independently addressable and
      independently testable. ``probe_access_token`` had zero direct tests until
      ``tests/test_registration_at_probe.py``; when you change a stage, add or
      extend its own contract test rather than relying on full-run integration.
    - Stage methods communicate *verdicts* via ``self.runtime`` fields and the
      checkpoint payload, never via return values (they all return ``None`` or
      a checkpoint dict). ``finalize()`` is the single place the terminal
      result is assembled.
    - The file is intentionally NOT split into per-stage modules: the stages
      share ``self.runtime``/``self.r`` too tightly for physical separation to
      be anything but an import cycle. The seam that IS enforced is
      ``RegistrationOperations`` (workflow → provider) and ``store``
      (persistence); see ``docs/architecture.md`` "Dependency Direction".
    """

    def __init__(
        self,
        machine: RegistrationStateMachine,
        *,
        proxy: Any = None,
        password: Any = None,
        sentinel_data: Mapping[str, Any] | None = None,
        mailbox: Any = None,
        phone_pool: Any = None,
        codex_oauth: bool = False,
        registration_mode: Any = None,
        browser_headless: bool | None = None,
        enroll_2fa: bool = True,
        config: Mapping[str, Any] | None = None,
        proxy_metadata: Mapping[str, Any] | None = None,
        operations: RegistrationOperations,
        persistence: RegistrationPersistence | None = None,
        post_process_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.machine = machine
        self.input_proxy = proxy
        self.input_password = password
        self.input_sentinel = sentinel_data
        self.input_mailbox = mailbox
        self.phone_pool = phone_pool
        # Protocol registration is AT-only. Keep the legacy argument accepted
        # for callers, but normalize it away instead of entering a dead stage.
        self.codex_oauth = False
        self.input_registration_mode = registration_mode
        self.browser_headless = browser_headless
        self.enroll_2fa = bool(enroll_2fa)
        self.config = config
        self.proxy_metadata = dict(proxy_metadata or {})
        self._operations = operations
        self.persistence = persistence or StorageRegistrationPersistence()
        self.post_process_result = post_process_result
        self.runtime = RegistrationRuntimeState()
        self.stage_runner = RegistrationStageRunner(self.runtime, machine)
        self._timing_open = False

    @property
    def r(self) -> RegistrationOperations:
        return self._operations

    def run(self) -> dict[str, Any]:
        r = self.r
        r._tl().clear()
        r.select_auth_fingerprint(rotate=True)
        config_scope = r.runtime_config_scope(self.config, workflow="registration")
        config_scope.__enter__()
        try:
            ensure_not_cancelled()
            self._bootstrap()
            resumed = self._resume_post_create()
            if resumed is not None:
                return resumed
            self._run_stage(RegistrationState.AUTH_FLOW, "2-Auth flow", self.auth_flow)
            self._run_stage(RegistrationState.USER_REGISTER, "3-User register (email+password)", self.user_register)
            self._run_stage(RegistrationState.EMAIL_OTP_SEND, "4-Trigger email OTP", self.send_email_otp)
            self._run_stage(RegistrationState.EMAIL_OTP_WAIT, "5-Get email OTP", self.wait_email_otp)
            self._run_stage(RegistrationState.EMAIL_OTP_VALIDATE, "6-Validate email OTP", self.validate_email_otp)
            self._run_stage(RegistrationState.CREATE_ACCOUNT, "7-Create account", self.create_account)
            self._run_stage(RegistrationState.AUTH_SESSION, "8-Fetch auth session", self.fetch_auth_session)
            self._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Validate access token", self.probe_access_token)
            self._set_outcome()
            # Opt-in, and deliberately *after* ``_set_outcome``: it must never be
            # able to change the registration verdict (see the method docstring).
            self.obtain_oauth_refresh_token()
            self._run_stage(RegistrationState.TOTP_ENROLL, "9-Enroll TOTP", self.enroll_totp)
            return self._run_stage(RegistrationState.FINALIZE, "10-Finalize registration", self.finalize)
        except RegistrationAbort as exc:
            return self._abort_result(str(exc))
        except RegistrationCancelled:
            # Cooperative cancellation: report the same cancelled contract the
            # batch runner and the browser path use, not an internal error.
            return self._abort_result("registration_cancelled", cancelled=True)
        except (MailboxEndpointUnavailableError, MailboxAuthInvalidError) as exc:
            return self._abort_result(str(exc))
        except Exception as exc:
            error = f"registration_internal_error:{type(exc).__name__}:{exc}"
            return self._abort_result(error)
        finally:
            self._close_sessions()
            config_scope.__exit__(None, None, None)

    def _abort_result(self, error: str, *, cancelled: bool = False) -> dict[str, Any]:
        """Single failure-result constructor for every run() exit path.

        三条 except 臂曾各拼一份结果（cancelled 臂多一个 registration_state），
        漂移只是时间问题——现在同一构造，cancelled 仅多打一个状态标记。
        """
        r = self.r
        if cancelled:
            if self.machine.state is not RegistrationState.FAILED:
                self.machine.fail("registration_cancelled")
        elif self.machine.state is not RegistrationState.FAILED:
            self.machine.fail(error)
        result = r._failure_result(
            error,
            email=self.runtime.username,
            mailbox=self.runtime.mailbox,
            password=self.runtime.password,
            existing_account=self.runtime.existing_account,
            existing_account_password_known=self.runtime.existing_account_password_known,
            access_token=self.runtime.access_token,
        )
        from .registration_result import safe_proxy_audit
        result["proxy_audit"] = safe_proxy_audit(getattr(self, "proxy_metadata", {}))
        if cancelled:
            result["registration_state"] = "cancelled"
        elif self.runtime.existing_account:
            result["registration_state"] = "partial_registered"
        if self.runtime.existing_login_error:
            # The re-login lane's own cause.  ``_registration_outcome`` prefers
            # ``user_already_exists`` over it -- correctly, that *is* the real
            # cause -- but the side effect is that whether the lane ran at all,
            # and whether it spent an email code, became unanswerable from
            # storage: measured 2026-09-15, ``existing_login`` appeared in 0 of
            # 4377 ``registration_audit.detail_json`` values, and the stdout log
            # that would have shown it stopped 09-15 04:18.  Carry it so the
            # "did this cost a code?" question stays answerable.
            result["existing_login_error"] = self.runtime.existing_login_error
        result["registration_machine"] = self.machine.snapshot()
        return result

    def _run_stage(self, state: RegistrationState, label: str, handler: Callable[[], Any]) -> Any:
        r = self.r
        # Checked before _tick: cancellation must surface as
        # RegistrationCancelled, not be reclassified as a stage transport
        # failure, and must not leave an open timing entry behind.
        ensure_not_cancelled()
        r._tick(label)
        self._timing_open = True
        try:
            self.stage_runner.context = self.runtime.context or self.runtime
            value = self.stage_runner.run_stage(
                state,
                handler,
                timeout_seconds=self._stage_timeout(state),
            )
        except RegistrationAbort:
            raise
        except RegistrationCancelled:
            # Raised from inside a handler (e.g. the OTP poll loop). Must not
            # be reclassified as a stage transport failure.
            raise
        except RegistrationStageOverrun as exc:
            raise RegistrationAbort(f"{state.value}_stage_budget_exceeded:{exc}") from exc
        except (
            NameError,
            AttributeError,
            ImportError,
            KeyError,
            TypeError,
            IndexError,
            UnboundLocalError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            # Programming/contract errors only. IndexError used to fall into the
            # transport catch-all below and get retried as a network failure --
            # the same "error name hides root cause" class as the RuntimeError
            # demotion fixed in error_classification. Keep this tuple in sync
            # with error_classification.INTERNAL_ERROR_MARKERS, which matches
            # the `{state}_internal:<Type>:` label text.
            raise RegistrationAbort(
                f"{state.value}_internal:{type(exc).__name__}:{exc}"
            ) from exc
        except Exception as exc:
            # Transport/protocol failures keep their message so
            # classify_error's marker vocabulary decides retryability.
            raise RegistrationAbort(f"{state.value}_transport:{exc}") from exc
        finally:
            if self._timing_open:
                r._safe_tock()
                self._timing_open = False
        if state is RegistrationState.FINALIZE and isinstance(value, dict):
            value["timing"] = r._timing_summary()
            r._print_timings()
        return value

    def _stage_timeout(self, state: RegistrationState) -> float | None:
        if self.config is None:
            return None
        registration_cfg = self.config.get("registration", {})
        if not isinstance(registration_cfg, Mapping):
            return None
        values = registration_cfg.get("stage_timeouts", {})
        if not isinstance(values, Mapping) or state.value not in values:
            return None
        try:
            return float(values[state.value])
        except (TypeError, ValueError):
            return None

    def _otp_poll_timeout(self) -> int:
        """Mailbox poll budget for the OTP wait stage.

        The stage budget is only observable after a handler returns, so the
        stage that can legitimately block for minutes hands the smaller of the
        two limits to the poll that actually blocks.
        """
        timeout = int(self.runtime.otp.email_cfg.get("otp_timeout", 300) or 300)
        budget = self._stage_timeout(RegistrationState.EMAIL_OTP_WAIT)
        if budget is None:
            return timeout
        return max(1, min(timeout, int(budget)))

    def _abort(self, error: str) -> None:
        raise RegistrationAbort(error)

    def _checkpoint_payload(self) -> dict[str, Any]:
        # 数据契约归 registration_checkpoint（候选1拆解）；编排仍在此处。
        payload = registration_checkpoint.build_checkpoint_payload(
            self.runtime, lambda: self.r._mailbox_snapshot(self.runtime.mailbox)
        )
        payload["auth_fingerprint_profile"] = str(current_auth_fingerprint().get("impersonate") or "")
        return payload

    def _persist_checkpoint(self, state: str) -> None:
        s = self.runtime
        if not s.username:
            return
        try:
            registration_checkpoint.persist_checkpoint(
                self.persistence, self.config, s, self._checkpoint_payload(), state,
            )
        except Exception as exc:
            print(f"  [Checkpoint] persist warning: {self.r._sanitize_text(exc)}")

    def _resume_post_create(self) -> dict[str, Any] | None:
        mailbox_email = str(getattr(self.runtime.mailbox, "email", "") or "").strip()
        if not mailbox_email or self.input_mailbox is None:
            return None
        payload = self.runtime.resume_checkpoint or registration_checkpoint.load_resumable_checkpoint(
            self.persistence, mailbox_email, self.config
        )
        if payload is None:
            return None
        if not payload.get("access_token"):
            error = registration_checkpoint.session_recovery_error(payload)
            if error:
                self._abort(error)
        registration_checkpoint.apply_resume_payload(self.runtime, payload)
        self.runtime.username = mailbox_email
        set_auth_fingerprint(str(payload.get("auth_fingerprint_profile") or ""))
        print(f"[*] Resuming saved registration checkpoint for {mailbox_email}")
        if not self.runtime.access_token:
            s = self.runtime
            s.session = _new_registration_session(s.proxy)
            registration_checkpoint.restore_session_cookies(s.session, payload)
            s.base_headers = dict(payload["auth_headers"]) if isinstance(payload.get("auth_headers"), dict) else (
                self.r.openai_auth_headers(s.device_id, accept="application/json", include_trace=True)
            )
            s.session_recovery_attempts += 1
            self._persist_checkpoint(registration_checkpoint.SESSION_PENDING_STATE)
            logging.getLogger(__name__).info(
                "Resuming created-account session attempt=%s; signup and OTP remain disabled",
                s.session_recovery_attempts, extra={"event": "auth_session_recovery"},
            )
            self._run_stage(RegistrationState.AUTH_SESSION, "8-Resume auth session", self.fetch_auth_session)
        self._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Resume AT probe", self.probe_access_token)
        self._set_outcome()
        self.obtain_oauth_refresh_token()
        return self._run_stage(RegistrationState.FINALIZE, "10-Finalize resumed registration", self.finalize)

    def _has_resume_checkpoint(self) -> bool:
        if self.input_mailbox is None:
            return False
        return (
            registration_checkpoint.load_resumable_checkpoint(
                self.persistence, self.runtime.username, self.config
            )
            is not None
        )

    def _bootstrap(self) -> None:
        r = self.r
        s = self.runtime
        if self.config is None:
            self.config = r.current_config_data()
        s.email_cfg = dict(self.config.get("email_registration") or {})
        r.validate_config(self.config, workflow="registration")
        s.proxy = r._resolve_proxy_scheme(self.input_proxy, cfg=self.config)
        preflight = r.registration_network_preflight(proxy=s.proxy, proxy_attempts=2)
        s.proxy = str(preflight.get("proxy") or s.proxy or "")
        s.mailbox = r._ensure_mailbox_account(self.input_mailbox)
        if not s.mailbox or not s.mailbox.email:
            self._abort("mailbox_required")
        s.username = str(getattr(s.mailbox, "email", "") or "").strip()
        s.resume_checkpoint = registration_checkpoint.load_resumable_checkpoint(
            self.persistence, s.username, self.config
        ) or {}
        if not s.resume_checkpoint:
            self._persist_checkpoint("mailbox_ready")
        from .mailbox_service import MailboxService
        s.mailbox_service = MailboxService.create(self.config)
        chatgpt_cfg = self.config.get("chatgpt", {})
        s.auth_base = chatgpt_cfg.get("auth_base_url", "https://auth.openai.com")
        s.chat_base = chatgpt_cfg.get("chat_base_url", "https://chatgpt.com")
        from .paypal_proxy import infer_proxy_country
        r.set_fingerprint_geo(infer_proxy_country(s.proxy))
        self.machine.transition(RegistrationState.MAILBOX_READY)
        if s.resume_checkpoint:
            print("[*] Resumable post-create checkpoint found; skipping mailbox/OTP stages")
            return
        # Snapshot the mailbox here, at the earliest point the mailbox is known
        # and before any OTP can be issued.
        #
        # This used to run inside ``send_email_otp``.  That was too late: in
        # passwordless mode the OTP is sent by the earlier ``auth_flow``
        # (authorize) step, so by the time ``send_email_otp`` snapshotted, the
        # code mail was already visible on the forwarding page and got written
        # into ``seen_message_ids`` -- which ``_latest_email_otp_candidate``
        # then skips.  The poll drained its full 300s window while the code sat
        # in plain sight (2026-09-11, batch 5e32aa85: 10 of 12 runs failed).
        #
        # Taking the snapshot up front makes it a true "what was already in the
        # inbox before this attempt" marker, matching the reference design
        # where ``before_ids`` is captured during identity resolution.
        print("[*] Snapshotting mailbox (pre-OTP baseline)")
        r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)
        print("[*] ChatGPT Email Registration Started")
        self._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", self.extract_sentinel)
        self._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", self.prepare_identity)

    def extract_sentinel(self) -> None:
        r = self.r
        s = self.runtime
        from .sentinel import sentinel_backend

        if self.input_sentinel:
            print("[*] Using provided sentinel tokens")
            s.sentinel_data = self.input_sentinel
        elif sentinel_backend(self.config) == "legacy":
            s.sentinel_data = r._extract_sentinel(
                proxy=s.proxy,
                force_fresh=True,
                persist=False,
                browser_headless=self.browser_headless,
            )
        else:
            device_context = dict(self.persistence.get_device_context(s.username) or {})
            s.sentinel_data = {
                "oai_did": str(device_context.get("device_id") or uuid.uuid4()),
                "sentinel_source": "node_sdk_runner",
            }
        if not s.sentinel_data or not r._sentinel_device_id(s.sentinel_data):
            self._abort("sentinel_extract_failed")
        r.think_stage("post_sentinel")

    def prepare_identity(self) -> None:
        r = self.r
        s = self.runtime
        device_context = dict(self.persistence.get_device_context(getattr(s.mailbox, "email", "")) or {})
        stored_device_id = str(device_context.get("device_id") or "").strip()
        sentinel_device_id = str(r._sentinel_device_id(s.sentinel_data) or "").strip()
        if stored_device_id and stored_device_id != sentinel_device_id:
            from .sentinel import sentinel_backend

            if sentinel_backend(self.config) == "legacy" or self.input_sentinel:
                print("  [Device] Regenerating Sentinel tokens for persisted device context")
                s.sentinel_data = r._extract_sentinel(
                    proxy=s.proxy,
                    force_fresh=True,
                    persist=False,
                    browser_headless=self.browser_headless,
                    device_id=stored_device_id,
                )
                if not s.sentinel_data:
                    self._abort("sentinel_extract_failed: persisted device token refresh failed")
            else:
                s.sentinel_data = {
                    **dict(s.sentinel_data),
                    "oai_did": stored_device_id,
                }

        s.context = prepare_registration_context(
            proxy=s.proxy,
            mailbox=s.mailbox,
            sentinel_data=s.sentinel_data,
            password=self.input_password,
            registration_mode=self.input_registration_mode,
            auth_base=s.auth_base,
            chat_base=s.chat_base,
            stored_password=r._stored_registration_password,
            generate_password=r._generate_password,
            random_name=r._random_name,
            random_birthdate=r._random_birthdate,
            normalize_mode=r._normalize_registration_mode,
            get_device_context=self.persistence.get_device_context,
            sentinel_device_id=r._sentinel_device_id,
            new_uuid=lambda: str(uuid.uuid4()),
            browser_headless=self.browser_headless,
        )
        c = s.context
        s.username = c.username
        s.password = c.password
        s.full_name = c.full_name
        s.birthdate = c.birthdate
        s.registration_mode = c.registration_mode
        s.device_id = c.device_id
        s.session_logging_id = c.session_logging_id
        s.flow_invocation_id = str(uuid.uuid4())
        self.browser_headless = c.browser_headless
        s.sentinel_token = str(s.sentinel_data.get("sentinel_token") or "")
        s.sentinel_authorize_token = str(s.sentinel_data.get("sentinel_authorize_continue_token") or "")
        s.sentinel_so_token = str(s.sentinel_data.get("sentinel_so_token") or "")
        try:
            r.assert_sentinel_device_id(s.sentinel_data, s.device_id)
        except ValueError as exc:
            self._abort(str(exc))
        if c.reused_device_context:
            print("  [Device] Reusing persisted device context")
        print(f"[*] Username: {s.username}  Password: [stored]  Name: {s.full_name}  Birth: {s.birthdate}")
        self._persist_checkpoint("identity_ready")
        s.session = _new_registration_session(s.proxy)
        if s.registration_mode == "passwordless":
            # Keep the Web/NextAuth flow isolated from the Sentinel extraction
            # prime session. Importing its auth.openai.com login cookies creates
            # a stale login transaction and routes authorize to /log-in/password.
            r._set_oai_did_cookie(s.session, s.device_id)
        else:
            r._import_sentinel_cookies(s.session, s.sentinel_data, s.device_id)
        r.set_fingerprint_device(s.device_id)
        _apply_protocol_fingerprint(r, self.config, s.proxy)
        s.base_headers = r.openai_auth_headers(
            s.device_id,
            accept="application/json",
            include_trace=True,
            session_id=s.session_logging_id,
            flow_invocation_id=s.flow_invocation_id,
        )
        if str(s.base_headers.get("oai-device-id") or "") != s.device_id:
            self._abort("sentinel_extract_failed: auth header device id mismatch")
        s.auth_flow_started = int(time.time())

    def _issue_sentinel(self, flow: str) -> Any:
        from .sentinel import issue_sentinel_flow, sentinel_backend

        s = self.runtime
        issued = issue_sentinel_flow(
            flow=flow,
            device_id=s.device_id,
            session=s.session,
            proxy=s.proxy,
            supplied_data=s.sentinel_data,
            config=self.config,
        )
        data = dict(s.sentinel_data)
        if flow == "username_password_create":
            data["sentinel_token"] = issued.token
            s.sentinel_token = issued.token
        elif flow == "authorize_continue":
            data["sentinel_authorize_continue_token"] = issued.token
            data["sentinel_authorize_continue_so_token"] = issued.so_token
            s.sentinel_authorize_token = issued.token
        elif flow == "oauth_create_account":
            data["sentinel_oauth_token"] = issued.token
            data["sentinel_so_token"] = issued.so_token
            s.sentinel_so_token = issued.so_token
        data["oai_did"] = issued.device_id
        data["sentinel_source"] = str(
            data.get("sentinel_source") or sentinel_backend(self.config)
        )
        s.sentinel_data = data
        return issued

    def auth_flow(self) -> None:
        r = self.r
        s = self.runtime
        s.auth_flow_started = int(time.time())
        if s.registration_mode == "passwordless":
            r.request_with_retry(
                s.session, "get", f"{s.chat_base}/", label="ChatGPT prime",
                headers={**r.chatgpt_headers(s.device_id, session_id=s.session_logging_id, flow_invocation_id=s.flow_invocation_id, accept="text/html,application/xhtml+xml", referer=f"{s.chat_base}/")},
                impersonate=r.auth_impersonate(),
                attempts=1,
            )
        else:
            r.request_with_retry(
                s.session, "get", f"{s.auth_base}/create-account", label="Auth prime",
                headers={**s.base_headers, "Accept": "text/html,application/xhtml+xml"},
                impersonate=r.auth_impersonate(),
            )
        csrf_resp = r.request_with_retry(
            s.session, "get", f"{s.chat_base}/api/auth/csrf", label="Auth csrf",
            headers=r.nextauth_headers(s.device_id, session_id=s.session_logging_id, referer=f"{s.chat_base}/", origin=s.chat_base),
            impersonate=r.auth_impersonate(),
        )
        s.csrf_token = (r._json_or_raw(csrf_resp).get("csrfToken") or "").strip()
        s.signup_state = r._prepare_signup_auth_state(
            s.session,
            s.username,
            s.device_id,
            s.session_logging_id,
            s.auth_base,
            s.chat_base,
            s.base_headers,
            s.csrf_token,
            sentinel_token=s.sentinel_token,
            authorize_sentinel_token=s.sentinel_authorize_token,
            sentinel_so_token=s.sentinel_so_token,
            proxy=s.proxy,
            passwordless_web=s.registration_mode == "passwordless",
            attempts=r._passwordless_signin_attempts() if s.registration_mode == "passwordless" else r._signup_signin_attempts(),
        )
        # P0-1 判据 A 的**取证埋点**（先扩样本，暂不硬止损）。
        #
        # ``passwordless_login_magic_link_sent`` 出现在**取码之前**，意味着服务端
        # 把这次 authorize 当成**登录**处理 ⇒ 该地址已存在，注册必然以
        # ``user_already_exists`` 收场。实测（批次 25288）精确率 3/3，但召回率只有
        # 3/11 —— n 太小，还不能拿它去写死路账本（误判的代价是把一个可注册地址
        # 永久拉黑）。所以这里只保留 dump + 打一行**每 run 可归因**的判定，让后续
        # 批次把精确率/召回率补到能拍板为止。
        #
        # 不要用日志里的 ``client_auth_session_dump`` 行做这件事：那些行按 stage
        # 做进程级降噪，跨 run 共享（见 ``auth_state._LAST_DUMP_TEXT``）。
        signup_dump = r._fetch_client_auth_session_dump(
            s.session, s.auth_base, s.base_headers, "after_signup_state"
        )
        s.signup_dump = signup_dump if isinstance(signup_dump, dict) else {}
        s.signup_lane = signup_lane_verdict(s.signup_dump)
        if s.signup_lane == "login":
            print("  Signup lane hint: login (transaction says this address already has a login magic link)")
        if int(s.signup_state.get("status") or 0) == 429:
            from .registration_concurrency import mark_registration_rate_limited

            retry_after = float(s.signup_state.get("retry_after_seconds") or 300)
            mark_registration_rate_limited(retry_after)
            self._abort(f"registration_rate_limited:retry_after={retry_after:.0f}s")
        if not s.signup_state.get("ok"):
            self._abort(f"signup_auth_state:{json.dumps(s.signup_state, ensure_ascii=False)[:300]}")
        if r._is_chatgpt_auth_login_landing(s.signup_state.get("url", "")):
            self._abort("signup_auth_state:redirected_to_chatgpt_login")
        self._persist_checkpoint("auth_flow")

    def _password_lane_active(self) -> bool:
        """True when this run must go through the password step.

        Two independent ways in, and they are **not** the same condition:

        * ``registration_mode != "passwordless"`` -- the operator opted into
          password-first registration (``email_registration.registration_mode``).
        * ``password_fallback`` -- the **server** moved the transaction to
          ``/log-in/password`` (``auth_flow._is_existing_login_redirect``), or the
          current step URL already is the password step.

        ``user_register`` and ``send_email_otp`` both branch on this.  It used to
        be written out twice as
        ``bool(signup_state.get("password_fallback")) or _is_signup_password_step(...)``;
        a single owner keeps the two call sites from drifting, and it removes the
        duplicated expression that made text-anchored mutation specs ambiguous.
        """
        s = self.runtime
        if s.registration_mode != "passwordless":
            return True
        return bool(s.signup_state.get("password_fallback")) or self.r._is_signup_password_step(
            s.signup_state.get("url", "")
        )

    def user_register(self) -> None:
        s = self.runtime
        if not self._password_lane_active():
            # The passwordless lane must not POST ``user/register``.
            #
            # It used to probe it first (shipped 2026-09-15) so an
            # already-registered address would be recognised before a mailbox
            # poll and a spent email code.  That probe was removed the next day.
            #
            #   * 🔴 The destructiveness is **not** in the POST -- it is in
            #     ignoring the POST's *response*.  The 200 body carries its own
            #     follow instructions:
            #         {"continue_url": ".../api/accounts/email-otp/send",
            #          "method": "GET", "page": {"type": "email_otp_send"}}
            #     The probe never followed it -- ``[4-Trigger email OTP]`` took
            #     the synthetic ``assumed_pre_sent`` branch below -- so every
            #     later ``email-otp/validate`` answered 409 "Your sign-in session
            #     is no longer valid".  Measured on batch 28260: 89 accepted
            #     probes -> 85 such 409s, against 75 of 77 rejected probes
            #     validating fine.
            #     Confirmed 2026-09-16 against a natural control group built on
            #     the *same* ``_post_user_register`` payload: follow the response
            #     -> 0 of 12 validations 409 (and all 12 finalized); ignore it ->
            #     85 of 85.  Fisher one-sided p = 1.4e-15.  See
            #     ``docs/audits/plan-2026-09-16-password-first-registration.md``
            #     §2.0.  The password lane below already follows the response:
            #     ``send_email_otp`` -> ``_email_otp_send_url`` ->
            #     ``_follow_continue_url``, which is a GET and therefore matches
            #     the ``"method": "GET"`` the server asks for.
            #   * It never answered the question it was built for: across 168
            #     probe runs ``user/register`` returned ``user_already_exists``
            #     **zero** times, while ``create_account`` returned it 70 times
            #     in that same batch.  ``user/register`` is not an existence
            #     oracle, so no response-shape logic could have rescued it.
            #
            # Do not reintroduce the probe **as an existence oracle** -- that part
            # is settled.  Raw counts live in the 2026-09-15 work log.
            s.reg_data = {
                "mode": "passwordless_signup",
                "auth_state": {
                    "attempt": s.signup_state.get("attempt", ""),
                    "url": s.signup_state.get("url", ""),
                    "status": s.signup_state.get("status", 0),
                },
            }
            s.password_unknown = True
            print("  Registration mode: passwordless_signup (HAR login_or_signup)")
            return
        self._post_user_register()
        if s.reg_response.status_code != 200:
            err_code = s.reg_data.get("error", {}).get("code", "")
            err_msg = s.reg_data.get("error", {}).get("message", str(s.reg_data))
            state_url = str(s.signup_state.get("url") or "")
            if err_code == "invalid_auth_step" and "email-verification" in state_url:
                # 🔴 2026-09-16 拍板：密码泳道**失败即 abort，不回落 passwordless**。
                #
                # ``invalid_auth_step`` 的意思是「事务不在密码步」——服务端
                # **没有接受我们的密码**。再往下走就是 ``create_account``，那会建出
                # 一个**无密码账号**，而「不产生无密码账号」正是密码优先模式存在的
                # 全部理由（aBai 同一处选择 ``raise``：宁可不注册）。
                #
                # 与下面 ``else`` 分支的唯一区别是**时机**：这里在发码之前就停手，
                # 所以不会为一个注定要被拒绝的地址烧掉一个邮箱 OTP。
                # passwordless 泳道**不受影响** —— 它的门禁在
                # ``_password_lane_active()`` 那里就返回了，根本走不到这个分支。
                #
                # 🔴 这里原先还有一条恢复路径（``print("...resuming OTP step...")``
                # + ``s.resume_email_verification = True``），已删除。三条独立理由，
                # 任何一条单独成立就足以删掉它：
                #
                #   1. **它已不可达。** 函数入口的 ``_password_lane_active()`` 门禁
                #      为假时直接 ``return``，而 ``_post_user_register()`` 既不写
                #      ``registration_mode`` 也不写 ``signup_state`` ⇒ 走到这里时该
                #      谓词必然仍为真。旧写法把同一个谓词又判了一遍，那个 ``if``
                #      恒真、``else`` 之后的恢复分支恒不可达。
                #   2. **它本来就是我们要禁的那条路。** 那个标志只改 OTP 的发码
                #      端点，**不跳过** ``run()`` 的第 7 步 ``create_account``
                #      ⇒ 恢复下去照样建出无密码账号。
                #   3. **即使不建号也不值得做。** 对已存在账号，email OTP 能走通但
                #      ``continue_url`` 仍落 ``/about-you``，NextAuth session cookie
                #      **从不下发**（2026-09-14 实测 5/5）⇒ 恢复 = 白烧一个 OTP。
                #
                # 零成本佐证：全量留存日志（5919 个 .log/.jsonl/.txt/.json，含
                # ``runtime/logs/processes/*/``）里 ``resuming OTP step`` **0 次命中**
                # ⇒ 删除不改变任何已观测行为。
                #
                # ✅ 2026-09-16 收尾（老板拍板）：该标志 ``resume_email_verification``
                # **已整体删除** —— 状态字段、两处读点、以及 ``_email_otp_send_url``
                # 里那个回落分支和它唯一的消费者 ``auth_base`` 参数。此前它挂在
                # ``READ_BUT_NEVER_WRITTEN`` 里「待拍板」，现在白名单只剩
                # ``phone_result`` 一条。要复活它，先推翻 ``account_creation``
                # 那句「缺 ``continue_url`` 必须报错，不许猜端点」。
                #
                # ``err_code`` 在这里**必然**等于 ``"invalid_auth_step"``（上面那个
                # ``if`` 的条件），所以不要写成 ``err_code or f"http_{...}"`` ——
                # 那个回退是死代码。仍然插值 ``err_code`` 而不是写死字符串，是为了
                # 将来放宽条件（例如把 ``invalid_state`` 也收进来）时后缀自动跟随。
                self._abort(f"password_step_unconfirmed:{err_code}")
            elif err_code == "invalid_auth_step" and self.r._is_existing_login_redirect(state_url):
                # 🔴 2026-09-16（P1）：服务端把这次注册**路由到了登录页** ——
                # 这是「地址已存在」的**第三种、也是最早的一种**表达方式。
                #
                # 前两种都出现在 ``create_account``（``user_already_exists`` /
                # ``identity_provider_mismatch``，见 ``DEAD_END_MARKERS``）；这一种
                # 提前到了 ``login_or_signup`` 的**路由落点**：服务端直接把事务送进
                # ``/log-in/password``，随后对 ``user/register`` 回 400
                # ``invalid_auth_step``。
                #
                # 生产实测（2026-09-16 两批）：``login_or_signup`` 落
                # ``/log-in/password`` 的账号**全部**以这个 code 收场 ——
                # 批次 34632 是 21/21，批次 28512 是 10/10，合计 **31/31，0 例外**；
                # 同期落 ``/email-verification`` 的那个成功了。落点与结果完全分离，
                # 且两批共用同一个出口（预检都选中 ``global.9http.com:9091``）
                # ⇒ 这是**地址**属性，不是出口、也不是时间窗。
                #
                # 为什么不能沿用上面那条 ``password_step_unconfirmed``：那条是
                # ``auth_state``（``retryable`` + ``batch_retry``），前提是「什么都没
                # 被消费，下一批换个出口再试」。这里正相反 —— 服务端已明说地址归它
                # 所有，重试只会把整个握手重走一遍再拿到同一个答案。归 ``account``
                # （``batch_dropped``）才对，也不会误伤：本批唯一的
                # ``/email-verification`` 落点碰不到这条分支。
                #
                # 🔴 判据是 ``_is_existing_login_redirect``（``/log-in*``），**不是**
                # ``_is_signup_password_step``（``/create-account/password``）。两者都
                # 会让 ``_password_lane_active`` 为真，但只有前者表达「地址已存在」；
                # 后者落进下面的 ``else``，保持既有行为。
                #
                # ``existing_account`` 置真会让 ``_abort_result`` 把终态装成
                # ``partial_registered`` —— 与 09-15 那 305 个 ``user_already_exists``
                # 同类，只是发现得更早、不烧 OTP。
                s.existing_account = True
                self._mark_partial_registration(reason=DEAD_END_SIGNUP_ROUTED_TO_LOGIN)
                self._abort(f"existing_account_{DEAD_END_SIGNUP_ROUTED_TO_LOGIN}")
            else:
                # 🔴 2026-09-16（P0）：拼 **code**，不要拼 message。
                #
                # 分类器（``failure_registry`` / ``error_classification``）按 **code**
                # 形式做子串匹配，marker 表里写的是 ``invalid_auth_step``。原先这里拼
                # ``err_msg``，串里只有 ``Invalid authorization step.`` —— 两者**没有
                # 共同子串**，分类掉进兜底类 ``unknown``；而 ``unknown`` 既不在
                # ``BATCH_RETRY_CLASSES`` 也不在 ``BATCH_DROPPED_CLASSES`` ⇒ 不重试、
                # 不记掉号、不告警，**整批静默蒸发**。
                #
                # 生产实测（批次 34632，2026-09-16 11:00）：21 个地址全部
                # ``user_register:Invalid authorization step.`` +
                # ``failure_class=unknown``，成功率 1/22，而日志上只留一行看不出原因
                # 的错误。同一个响应，改拼 ``user_register:invalid_auth_step`` 后分类
                # 是 ``auth_state``（可重试）。
                #
                # ``err_code or err_msg``：code 缺失（服务端只给 message）时仍保留
                # 服务端原文，不丢诊断信息。Pinned by
                # ``tests/test_user_register_response_contract.py``.
                self._abort(f"user_register:{err_code or err_msg}")

    def _post_user_register(self) -> Any:
        """POST ``user/register`` (password + username) and stash the parsed body.

        Only the password lane calls this.  The passwordless lane must not: an
        accepted POST advances the server-side auth transaction, and the email
        OTP that follows then validates against a session the server has already
        moved past.  See ``user_register`` for the measured counts.
        """
        r = self.r
        s = self.runtime
        username_sentinel = self._issue_sentinel("username_password_create")
        s.reg_response = r.request_with_retry(
            s.session, "post", f"{s.auth_base}/api/accounts/user/register", label="User register",
            json={"password": s.password, "username": s.username},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/create-account/password",
                origin=s.auth_base,
                sentinel_token=username_sentinel.token,
            ),
            impersonate=r.auth_impersonate(),
        )
        try:
            s.reg_data = s.reg_response.json()
        except (ValueError, TypeError):
            s.reg_data = {"_raw": s.reg_response.text[:300]}
        print(f"  Status: {s.reg_response.status_code}")
        print(f"  Response: {r._sanitize_text(json.dumps(s.reg_data, ensure_ascii=False)[:300])}")
        return s.reg_response

    def send_email_otp(self) -> None:
        r = self.r
        s = self.runtime
        # Do NOT snapshot here.  The pre-OTP baseline is taken once in
        # ``_bootstrap``; re-snapshotting at this point would be a race --
        # passwordless sends the OTP during ``auth_flow``, so the code mail can
        # already be visible and would be recorded as "seen", permanently
        # hiding it from ``_latest_email_otp_candidate``.
        continue_url = r._email_otp_send_url(s.reg_data)
        otp_send_started = int(time.time())
        if not self._password_lane_active():
            # authorize with login_hint sends the first OTP itself. Do not
            # immediately POST resend: that endpoint is rate-limited for this
            # flow and the reference browser path only polls the pre-sent code.
            response = r.SyntheticResponse(
                204,
                {"assumed_pre_sent": True},
                url=s.signup_state.get("url", ""),
            )
        else:
            response = r._follow_continue_url(
                s.session,
                continue_url,
                s.base_headers,
                referer=f"{s.auth_base}/create-account/password",
                label="Email OTP send",
            )
        # 保留 summary，不要只打印后丢弃：``wait_email_otp`` 要用它分辨超时
        # 根因（服务端没派发 vs 邮箱没收到），见 ``_otp_timeout_error``。
        dump = r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_otp_send")
        s.otp_send_dump = dump if isinstance(dump, dict) else {}
        if response is None:
            self._abort("email_otp_send_missing_continue_url")
        if getattr(response, "status_code", 0) not in (200, 202, 204):
            self._abort(f"email_otp_send_failed:{response.status_code}")
        s.otp_issued_after = otp_send_started
        if s.registration_mode == "passwordless" and r._json_or_raw(response).get("assumed_pre_sent"):
            s.otp_issued_after = max(0, s.auth_flow_started - 5)

    def wait_email_otp(self) -> None:
        r = self.r
        s = self.runtime
        email_cfg = self.config.get("email_registration", {})
        s.email_cfg = email_cfg if isinstance(email_cfg, dict) else {}
        # P0-1 判据 B：事务里还挂着 ``passwordless_email_otp_send_pending``
        # ⇒ 服务端**没有完成**派发 ⇒ 这一轮**不可能**拿到码。实测 6/6 超时的
        # run 都停在这个形状上（15 个拿到码的一个都没有），所以直接止损，
        # 不去烧满 300s 轮询。
        #
        # 单独一个错误名而不是给 ``email_otp_poll_timeout`` 加后缀：这条路径
        # **根本没轮询**，说「poll timeout」会让日志撒谎。
        if otp_dispatch_verdict(s.otp_send_dump) == "stuck":
            print("  Email OTP send is still pending on the server; skipping the mailbox poll")
            self._abort("email_otp_send_stuck")
        s.email_code = r._poll_registration_email_otp(
            s.mailbox,
            subject_keyword=r.REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
            timeout=self._otp_poll_timeout(),
            issued_after_unix=s.otp_issued_after,
            proxy=s.proxy,
            resend_callback=lambda: r._send_registration_email_otp(
                s.session,
                s.auth_base,
                s.base_headers,
                current_url=s.signup_state.get("url", ""),
                mode="passwordless" if s.registration_mode == "passwordless" else "send",
            ),
            resend_after_seconds=s.email_cfg.get("remail_otp_resend_after_seconds", 30),
            poll_otp_fn=s.mailbox_service.poll_otp,
        )
        if not s.email_code:
            self._abort(self._otp_timeout_error())

    def _otp_provider(self) -> str:
        """当前邮箱渠道名；拿不到就是空串（空串不在重发名单里 ⇒ 判「无重发」）。"""
        mailbox = getattr(self.runtime, "mailbox", None)
        return str(getattr(mailbox, "provider", "") or "").strip().lower()

    def _otp_timeout_error(self) -> str:
        """``email_otp_poll_timeout`` 加后缀，说明**码为什么没到**。

        脉冲调度（``registration_pulse``）把「一轮里 OTP 失败聚集」当成出口被
        封禁的证据，命中就暂停换池。旧判据只做子串匹配（``OTP_BAN_MARKERS``
        里的 ``otp_poll_timeout`` 命中 ``email_otp_poll_timeout``），于是把两种
        完全不同的根因混为一谈。

        走到这里说明**已经轮询过**（判据 B 在 ``wait_email_otp`` 里拦掉了
        「服务端没派发」那种），所以只剩两种可能：

        * ``mailbox_side_no_code`` —— dump 可读且没有挂起键（服务端侧发码事务
          走完了），但整个轮询窗口内**邮箱侧没有产出可用验证码**（**邮箱侧**，
          与出口无关，不该触发封禁暂停）；
        * 裸名 —— dump 不可用，**没有证据**，回落到旧行为（算派发侧）：
          没有证据时宁可多停 60s，也不要让一整轮撞在被封的出口上。

        🔴 **2026-09-16 改名（原 ``code_not_delivered``）**：那个名字在撒谎。
        判据只是 ``otp_dispatch_verdict() == "dispatched"``，而那是**服务端**的
        判定，推不出「邮件投递到了邮箱」。实测反例：批次 25116 的 10 个账号
        全走 ``ima3.52dfd.top``（返回 72 字节 JSON，HTML 解析器结构性读不到），
        10/10 仍被标成 ``code_not_delivered`` ⇒ 真实语义是「**我们没读出来**」。
        新名字只陈述已知事实，且**不能**反读成「邮件一定到了」——「没收到」与
        「读不出」用当前数据区分不了（轮询器只回码，不回观测元数据）。

        ⚠ ``no_resend_for_channel`` 是**渠道能力**后缀，不是第三个根因：该渠道
        不在 ``otp_strategy.otp_resend_eligible()`` 名单里 ⇒ 整段 ``otp_timeout``
        只发过一次邮件。它只与 ``mailbox_side_no_code`` **组合**出现，语义是
        「邮箱侧没有码，而且连重发这个补救手段都没有」。

        ⚠ 后缀只描述**这一个账号**为什么没拿到码；够不够格叫「IP 封禁」由
        ``registration_pulse._detect_ip_ban`` 的整轮一致性决定（每个账号钉在池里
        各自的出口上，一轮里有账号拿到码就证明出口是通的）。
        """
        if otp_dispatch_verdict(self.runtime.otp_send_dump) == "dispatched":
            suffix = OTP_MAILBOX_SIDE_MARKER
            if not otp_resend_eligible(self._otp_provider()):
                suffix = f"{suffix}:{OTP_NO_RESEND_MARKER}"
            return f"email_otp_poll_timeout:{suffix}"
        return "email_otp_poll_timeout"

    def validate_email_otp(self) -> None:
        r = self.r
        s = self.runtime
        otp_ok, s.otp_data = r._validate_email_otp(
            s.session,
            s.auth_base,
            s.base_headers,
            s.email_code,
            sentinel_data=s.sentinel_data,
            use_sentinel=False,
        )
        if not otp_ok and r._is_wrong_email_otp_code(s.otp_data):
            print("  Email OTP was rejected; retrying latest mailbox code once...")
            retry_code = s.mailbox_service.poll_otp(
                s.mailbox,
                subject_keyword=r.REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
                timeout=min(60, int(s.email_cfg.get("otp_timeout", 300))),
                issued_after_unix=max(0, s.auth_flow_started - 5),
                proxy=s.proxy,
                excluded_otps={s.email_code},
            )
            if retry_code and retry_code != s.email_code:
                s.email_code = retry_code
                otp_ok, s.otp_data = r._validate_email_otp(
                    s.session,
                    s.auth_base,
                    s.base_headers,
                    s.email_code,
                    sentinel_data=s.sentinel_data,
                    use_sentinel=False,
                )
        if not otp_ok:
            r._fetch_client_auth_session_dump(
                s.session,
                s.auth_base,
                s.base_headers,
                "after_otp_validate_failed",
            )
            self._abort(f"email_otp_validate:{json.dumps(s.otp_data, ensure_ascii=False)[:300]}")
        try:
            r._follow_continue_url(
                s.session,
                s.otp_data.get("continue_url", ""),
                s.base_headers,
                referer=f"{s.auth_base}/verify-email",
                label="Email OTP continue",
            )
        except Exception as exc:
            print(f"  Email OTP continue transport warning: {r._sanitize_text(exc)}")

    def create_account(self) -> None:
        r = self.r
        s = self.runtime
        create_sentinel = self._issue_sentinel("oauth_create_account")
        response = r.request_with_retry(
            s.session, "post", f"{s.auth_base}/api/accounts/create_account", label="Create account",
            json={"name": s.full_name, "birthdate": s.birthdate},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/about-you",
                origin=s.auth_base,
                sentinel_token=create_sentinel.token,
                sentinel_so_token=create_sentinel.so_token,
            ),
            impersonate=r.auth_impersonate(),
        )
        try:
            s.create_data = response.json()
        except (ValueError, TypeError):
            s.create_data = {"_raw": response.text[:300]}
        print(f"  Status: {response.status_code}")
        # 600, not 300: ``user_already_exists`` carries a structured
        # ``userAlreadyExistsRecovery`` object past the 300-char cut, and that
        # object is the only place the server states the recovery action.
        # Measured 2026-09-14 -- the truncated line read
        # ``"action": "continue_to_`` with no way to see the rest.
        # A 200 keeps the same budget but spends it differently; see
        # ``_create_account_response_line``.
        print(f"  Response: {_create_account_response_line(response.status_code, s.create_data, r._sanitize_text)}")
        s.create_ok = response.status_code == 200
        r.think_stage("post_create_account")
        s.existing_account = r._is_user_already_exists(s.create_data)
        if s.existing_account:
            self._mark_partial_registration()
        elif is_passwordless_signup_mismatch(s.create_data):
            # 🔴 The **second** code the server uses for "this address is already
            # registered", measured 2026-09-15 07:33:37 (run ``b93f598d``):
            # ``create_account`` answered 400 ``identity_provider_mismatch`` for
            # an address whose signup record used a non-password method.
            #
            # ``_is_user_already_exists`` only knows ``user_already_exists``, so
            # without this branch the address got **none** of the three
            # protections: no ``partial_registered`` state, no dead-end ledger
            # row, and the reported cause was classified ``unknown`` (advice
            # empty) -- so the next batch re-drove it and burned a fresh email
            # OTP to rediscover the same fact.
            #
            # 🔴 Deliberately **not** setting ``s.existing_account = True`` here.
            # That flag flips ``create_ok`` to ``True`` twenty lines below and
            # ``_create_account_error`` then returns ``""``, so the run would
            # blame whatever the re-login fallback last hit -- exactly the
            # mis-attribution ``registration_outcome._existing_account_error``
            # documents (three 09-06 addresses reported as
            # ``existing_login_otp_send_failed:429`` while the signup lane had
            # already answered).  Keeping the flag ``False`` preserves
            # ``create_account_failed:identity_provider_mismatch: …`` as the
            # reported cause while still taking the two protections we want.
            #
            # Zero risk of killing a fresh address: this code can only come back
            # from ``create_account`` on an address the server already has a
            # signup record for.
            self._mark_partial_registration(reason=PASSWORDLESS_SIGNUP_CODE)
        c = s.context
        # 🔴 ``password_unknown`` used to be re-derived here from
        # ``resume_email_verification``.  Once that flag was deleted the whole
        # statement reduced to ``s.password_unknown = s.password_unknown`` -- a
        # no-op -- so it was removed rather than left behind as a decoy that
        # looks like it protects something.  The real writers are
        # ``user_register`` (the passwordless lane) and the ``existing_account``
        # branch twenty lines below.
        if s.create_ok and not s.existing_account:
            s.session_recovery_started_at = int(time.time())
            self._persist_checkpoint(registration_checkpoint.SESSION_PENDING_STATE)
        if not s.create_ok and s.existing_account:
            # The server has stated this address is already registered.  Keep
            # ``create_ok`` true so the outcome wording stays
            # ``existing_account_user_already_exists:continue_to_login``.
            #
            # The passwordless email lane cannot turn this state into a session
            # -- it re-verifies an OTP and lands on ``/about-you``, which is a
            # *signup* step the server never follows with a NextAuth session
            # (measured 2026-09-14: 5/5 landings, 0 sessions) -- so
            # ``fetch_auth_session`` forbids it for this address and asks the
            # login-method probe instead.  Record whether a password login is
            # even possible: the probe can only *offer* the password step, the
            # caller still needs a password to submit.
            # 🔴 2026-09-16: this used to read "...clearing stored password.",
            # which was **false** -- nothing is cleared here (no
            # ``accounts.password`` write, no ``s.password`` reset).  Only the
            # three flags below are set.  The wording sent operators looking
            # for a credential wipe that never happened, so it now says what
            # the code does: the *generated* password is not evidence about
            # this account, and ``existing_account_password_known`` records
            # whether we hold one that is.
            print(
                "  Account already exists; the generated password is not evidence "
                "about this account (no stored credential is modified)."
            )
            s.create_ok = True
            s.password_unknown = True
            s.existing_account_password_known = bool(
                c is not None and (c.explicit_password or c.password_from_storage)
            )
        try:
            r._follow_continue_url(
                s.session,
                r._create_account_continue_url(s.create_data),
                s.base_headers,
                referer=f"{s.auth_base}/about-you",
                label="Create account continue",
            )
        except Exception as exc:
            print(f"  Create account continue transport warning: {r._sanitize_text(exc)}")

    def _mark_partial_registration(self, *, reason: str = "user_already_exists") -> None:
        """Record the server's existence verdict as a permanent dead end.

        ``reason`` is the raw verdict and must be a member of
        ``DEAD_END_MARKERS`` for the ledger's ``dead_end_reason`` to name it
        accurately -- ``RegistrationRetryGuard.mark_dead_end`` silently falls
        back to ``"user_already_exists"`` for anything it does not recognise,
        which is how a second spelling of the same verdict would have been
        recorded under the first spelling's name.
        """
        s = self.runtime
        RegistrationRetryGuard(self.config).mark_dead_end(s.username, reason=reason)
        logging.getLogger(__name__).info(
            "Server reports existing account; registration_status=partial_registered",
            extra={"event": "registration_status_changed", "account_ref": account_reference(s.username)},
        )
        emit_event({
            "domain": "registration", "operation": "registration", "stage": "registration_status_changed",
            "status": "running", "detail": "半注册", "registration_status": "partial_registered",
            "account_ref": account_reference(s.username), "run_id": current_run_id.get(),
        })

    def fetch_auth_session(self) -> None:
        r = self.r
        s = self.runtime
        s.auth_session = r._fetch_auth_session(s.session, s.chat_base, s.base_headers)
        s.auth_body = s.auth_session.get("body") or {}
        s.access_token = r._auth_session_access_token(s.auth_body)
        self._persist_checkpoint(
            "at_probe_pending" if s.access_token else registration_checkpoint.SESSION_PENDING_STATE
        )
        if not s.existing_account or s.access_token:
            return
        print("  Existing account has no ChatGPT session yet; probing the login method before spending an email code...")
        s.login_session = curl_requests.Session()
        if s.proxy:
            s.login_session.proxies = {"http": s.proxy, "https": s.proxy}
        r._set_oai_did_cookie(s.login_session, s.device_id)
        try:
            existing_login = r._login_existing_account_with_email_otp(
                session=s.login_session,
                username=s.username,
                mailbox=s.mailbox,
                did=s.device_id,
                session_logging_id=s.session_logging_id,
                auth_base=s.auth_base,
                chat_base=s.chat_base,
                base_headers=s.base_headers,
                csrf_token=s.csrf_token,
                proxy=s.proxy,
                sentinel_token=s.sentinel_token,
                sentinel_so_token=s.sentinel_so_token,
                # The probe can only *offer* the password step; a login still
                # needs a password to submit.  ``_login_probe_password`` owns
                # the "is it this account's own password?" decision.
                password=_login_probe_password(s),
                # A password login is followed by the account's *own* MFA
                # challenge, so the secret has to come along or the lane can
                # only answer ``existing_login_totp_secret_missing``.  Nothing
                # has been enrolled in this run yet, so read it from storage.
                totp_secret=r._stored_registration_totp(s.username),
                # ``existing_account`` is only ever set by the
                # ``user_already_exists`` answer, and for that address the
                # passwordless lane is a measured dead end (5/5 landings on the
                # signup profile step, 0 sessions).  So an inconclusive probe
                # must not fall back to it either.
                allow_passwordless=not s.existing_account,
            )
        except Exception as exc:
            existing_login = {"ok": False, "error": f"existing_login_transport:{exc}"}
        if not existing_login.get("ok"):
            s.existing_login_error = r._sanitize_text(existing_login.get("error") or "unknown")
            print(f"  Existing account login failed: {s.existing_login_error}")
            if _is_existing_login_dead_end_error(existing_login.get("error")):
                try:
                    RegistrationRetryGuard(self.config).mark_dead_end(
                        s.username,
                        reason="user_already_exists",
                        error=str(s.existing_login_error),
                    )
                except Exception as exc:
                    print(f"  Dead-end bookkeeping warning: {r._sanitize_text(exc)}")
            return
        s.auth_session = r._fetch_auth_session(s.login_session, s.chat_base, s.base_headers)
        s.auth_body = s.auth_session.get("body") or {}
        s.access_token = r._auth_session_access_token(s.auth_body)
        self._persist_checkpoint("at_probe_pending")
        if s.access_token:
            old_session = s.session
            s.session = s.login_session
            s.login_session = old_session

    def probe_access_token(self) -> None:
        r = self.r
        s = self.runtime
        if not s.access_token:
            return
        r.think_stage("pre_at_probe")
        s.at_probe = r._probe_registration_access_token(
            s.access_token,
            s.auth_body,
            proxy=s.proxy,
            cfg=self.config,
        )
        print(f"  Access token probe: HTTP {s.at_probe.get('status_code') or 'unknown'}")
        self._persist_checkpoint(
            "at_probe_complete" if s.at_probe.get("status_code") == 200 else "at_probe_transport_unknown"
        )

    def _obtain_refresh_token_enabled(self) -> bool:
        # ``self.config`` is None on the bare-handler path some tests build, and
        # the opt-in check must fail *closed* there rather than raise.
        cfg = (self.config or {}).get("registration", {})
        if not isinstance(cfg, Mapping):
            return False
        return bool(cfg.get("obtain_refresh_token", False))

    def obtain_oauth_refresh_token(self) -> None:
        return _registration_finalize.obtain_oauth_refresh_token(self)

    def _set_outcome(self) -> None:
        r = self.r
        s = self.runtime
        s.success, s.error, s.registration_warning = r._registration_outcome(
            s.create_ok,
            s.create_data,
            s.access_token,
            s.at_probe,
            s.existing_login_error,
        )
        # Email registration is AT-only. OAuth/phone recovery remains in its
        # own entry points; these impossible branches added hidden dependencies.
        s.post_registration_ready = True

    def enroll_totp(self) -> None:
        return _registration_finalize.enroll_totp(self)

    def finalize(self) -> dict[str, Any]:
        return _registration_finalize.finalize(self)

    def _close_sessions(self) -> None:
        seen: set[int] = set()
        resources = self.runtime.resources
        for session in (resources.session, resources.login_session):
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
