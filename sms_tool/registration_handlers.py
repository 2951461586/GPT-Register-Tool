"""Stage handler protocol and runner for registration orchestration."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from curl_cffi import requests as curl_requests

from .registration_state import (
    RegistrationContext,
    RegistrationStage,
    RegistrationStageOverrun,
    RegistrationState,
    RegistrationStateMachine,
    prepare_registration_context,
)


class RegistrationStageHandler(Protocol):
    state: RegistrationState

    def __call__(self, context: Any, state: dict[str, Any]) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class BoundRegistrationStage:
    """Bind a handler that returns state deltas to a stage.

    This is the generic dict-delta seam (a handler returns a mapping that is
    merged into shared ``state``).  The email workflow does not use it: it keeps
    mutable outputs on ``RegistrationRuntimeState`` and drives stages one at a
    time through :meth:`RegistrationStageRunner.run_stage`.  ``context`` is the
    opaque object handed to the stage and may be ignored.
    """

    stage: RegistrationStage
    handler: RegistrationStageHandler

    def run(self, context: Any, machine: RegistrationStateMachine, state: dict[str, Any]) -> None:
        result = RegistrationStage(
            self.stage.state,
            lambda current: self.handler(current, state),
            self.stage.timeout_seconds,
        ).run(context, machine)
        if isinstance(result, Mapping):
            state.update(result)


class RegistrationStageRunner:
    """Run stages against one machine, sharing an opaque context object.

    ``context`` is whatever the caller wants handlers to see; the email
    workflow passes its ``RegistrationRuntimeState`` (not a
    ``RegistrationContext``) and its handlers close over that instead of reading
    the argument.  ``run_stage`` is the production execution seam; ``run`` is the
    generic multi-stage helper used by focused tests.
    """

    def __init__(
        self,
        context: Any,
        machine: RegistrationStateMachine,
        *,
        cleanup: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.context = context
        self.machine = machine
        self.cleanup = cleanup
        self.state: dict[str, Any] = {}

    def run(self, stages: list[BoundRegistrationStage]) -> dict[str, Any]:
        try:
            for bound in stages:
                bound.run(self.context, self.machine, self.state)
            return self.state
        finally:
            if self.cleanup:
                self.cleanup(self.state)

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


@dataclass
class RegistrationRuntimeState:
    """Mutable outputs shared by otherwise independent registration stages."""

    proxy: str = ""
    mailbox: Any = None
    mailbox_service: Any = None
    sentinel_data: Mapping[str, Any] = field(default_factory=dict, repr=False)
    context: RegistrationContext | None = None
    session: Any = None
    login_session: Any = None
    auth_base: str = ""
    chat_base: str = ""
    base_headers: dict[str, Any] = field(default_factory=dict)
    username: str = ""
    password: str = field(default="", repr=False)
    password_unknown: bool = False
    full_name: str = ""
    birthdate: str = ""
    registration_mode: str = ""
    device_id: str = ""
    session_logging_id: str = ""
    flow_invocation_id: str = ""
    sentinel_token: str = field(default="", repr=False)
    sentinel_authorize_token: str = field(default="", repr=False)
    sentinel_so_token: str = field(default="", repr=False)
    auth_flow_started: int = 0
    csrf_token: str = field(default="", repr=False)
    signup_state: dict[str, Any] = field(default_factory=dict)
    reg_response: Any = None
    reg_data: dict[str, Any] = field(default_factory=dict)
    resume_email_verification: bool = False
    otp_issued_after: int = 0
    email_cfg: dict[str, Any] = field(default_factory=dict)
    email_code: str = field(default="", repr=False)
    otp_data: dict[str, Any] = field(default_factory=dict)
    create_data: dict[str, Any] = field(default_factory=dict)
    create_ok: bool = False
    existing_account: bool = False
    auth_session: dict[str, Any] = field(default_factory=dict)
    auth_body: dict[str, Any] = field(default_factory=dict)
    access_token: str = field(default="", repr=False)
    oauth_result: dict[str, Any] = field(default_factory=dict)
    oauth_tokens: dict[str, Any] = field(default_factory=dict, repr=False)
    phone_result: dict[str, Any] = field(default_factory=dict)
    oauth_refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    at_probe: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    error: str = ""
    registration_warning: str = ""
    post_registration_ready: bool = False
    totp_secret: str = field(default="", repr=False)
    twofa_result: dict[str, Any] = field(default_factory=dict, repr=False)


class RegistrationEmailWorkflow:
    """Email-registration stage handlers with one failure and cleanup policy."""

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
        operations: Any,
    ) -> None:
        self.machine = machine
        self.input_proxy = proxy
        self.input_password = password
        self.input_sentinel = sentinel_data
        self.input_mailbox = mailbox
        self.phone_pool = phone_pool
        # Kept in the signature for compatibility with older callers. Protocol
        # registration no longer performs the Codex OAuth refresh-token stage.
        self.codex_oauth = False
        self.input_registration_mode = registration_mode
        self.browser_headless = browser_headless
        self.enroll_2fa = bool(enroll_2fa)
        self.config = config
        self._operations = operations
        self.runtime = RegistrationRuntimeState()
        self.stage_runner = RegistrationStageRunner(self.runtime, machine)
        self._timing_open = False

    @property
    def r(self) -> Any:
        return self._operations

    def run(self) -> dict[str, Any]:
        r = self.r
        r._tl().clear()
        r.select_auth_fingerprint(rotate=True)
        config_scope = r.runtime_config_scope(self.config, workflow="registration")
        config_scope.__enter__()
        try:
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
            self._run_stage(RegistrationState.TOTP_ENROLL, "9-Enroll TOTP", self.enroll_totp)
            return self._run_stage(RegistrationState.FINALIZE, "10-Finalize registration", self.finalize)
        except RegistrationAbort as exc:
            if self.machine.state is not RegistrationState.FAILED:
                self.machine.fail(str(exc))
            result = r._failure_result(
                str(exc),
                email=self.runtime.username,
                mailbox=self.runtime.mailbox,
                password=self.runtime.password,
            )
            result["registration_machine"] = self.machine.snapshot()
            return result
        except Exception as exc:
            error = f"registration_internal_error:{type(exc).__name__}:{exc}"
            if self.machine.state is not RegistrationState.FAILED:
                self.machine.fail(error)
            result = r._failure_result(
                error,
                email=self.runtime.username,
                mailbox=self.runtime.mailbox,
                password=self.runtime.password,
            )
            result["registration_machine"] = self.machine.snapshot()
            return result
        finally:
            self._close_sessions()
            config_scope.__exit__(None, None, None)

    def _run_stage(self, state: RegistrationState, label: str, handler: Callable[[], Any]) -> Any:
        r = self.r
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
        except RegistrationStageOverrun as exc:
            raise RegistrationAbort(f"{state.value}_stage_budget_exceeded:{exc}") from exc
        except Exception as exc:
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
        timeout = int(self.runtime.email_cfg.get("otp_timeout", 300) or 300)
        budget = self._stage_timeout(RegistrationState.EMAIL_OTP_WAIT)
        if budget is None:
            return timeout
        return max(1, min(timeout, int(budget)))

    def _abort(self, error: str) -> None:
        raise RegistrationAbort(error)

    def _checkpoint_payload(self) -> dict[str, Any]:
        s = self.runtime
        r = self.r
        return {
            "email": s.username,
            "source": "register",
            "register_method": "email" if s.registration_mode != "phone" else "phone",
            "session_type": "at_only" if s.registration_mode == "at_only" else "web",
            "plan_type": "unknown",
            "success": False,
            "status": "at_probe_pending",
            "password": s.password,
            "device_id": s.device_id,
            "auth_session_logging_id": s.session_logging_id,
            "access_token": s.access_token,
            "id_token": s.id_token,
            "cookie_header": s.auth_session.get("cookie_header", "") if isinstance(s.auth_session, dict) else "",
            "auth_session": s.auth_body,
            "mailbox": r._mailbox_snapshot(s.mailbox),
            "registration_mode": s.registration_mode,
        }

    def _persist_checkpoint(self, state: str) -> None:
        s = self.runtime
        if not s.username:
            return
        try:
            from .storage import save_registration_checkpoint, upsert_account

            payload = self._checkpoint_payload()
            payload["registration_state"] = state
            save_registration_checkpoint(s.username, state, payload, runtime_config=self.config)
            if s.access_token:
                upsert_account(payload, runtime_config=self.config)
        except Exception as exc:
            print(f"  [Checkpoint] persist warning: {self.r._sanitize_text(exc)}")

    def _resume_post_create(self) -> dict[str, Any] | None:
        mailbox_email = str(getattr(self.runtime.mailbox, "email", "") or "").strip()
        if not mailbox_email or self.input_mailbox is None:
            return None
        try:
            from .storage import get_registration_checkpoint

            checkpoint = get_registration_checkpoint(mailbox_email, runtime_config=self.config)
            payload = checkpoint.get("payload") if isinstance(checkpoint, dict) else {}
        except Exception:
            checkpoint, payload = {}, {}
        if not isinstance(payload, dict) or not payload.get("access_token"):
            return None
        state = str(payload.get("registration_state") or checkpoint.get("state") or "")
        if state not in {"at_probe_pending", "at_probe_transport_unknown"}:
            return None
        s = self.runtime
        s.username = mailbox_email
        s.password = str(payload.get("password") or "")
        s.device_id = str(payload.get("device_id") or "")
        s.session_logging_id = str(payload.get("auth_session_logging_id") or "")
        s.access_token = str(payload.get("access_token") or "")
        s.id_token = str(payload.get("id_token") or "")
        s.auth_body = payload.get("auth_session") if isinstance(payload.get("auth_session"), dict) else {}
        s.auth_session = {"cookie_header": str(payload.get("cookie_header") or "")}
        s.registration_mode = str(payload.get("registration_mode") or "passwordless")
        s.create_ok = True
        print(f"[*] Resuming saved registration checkpoint for {mailbox_email}")
        self._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Resume AT probe", self.probe_access_token)
        self._set_outcome()
        return self._run_stage(RegistrationState.FINALIZE, "10-Finalize resumed registration", self.finalize)

    def _has_resume_checkpoint(self) -> bool:
        if self.input_mailbox is None:
            return False
        try:
            from .storage import get_registration_checkpoint

            checkpoint = get_registration_checkpoint(self.runtime.username, runtime_config=self.config)
            payload = checkpoint.get("payload") if isinstance(checkpoint, dict) else {}
            state = str((payload or {}).get("registration_state") or checkpoint.get("state") or "")
            return bool((payload or {}).get("access_token")) and state in {
                "at_probe_pending", "at_probe_transport_unknown"
            }
        except Exception:
            return False

    def _bootstrap(self) -> None:
        r = self.r
        s = self.runtime
        if self.config is None:
            self.config = r.current_config_data()
        r.validate_config(self.config, workflow="registration")
        s.proxy = r._resolve_proxy_scheme(self.input_proxy, cfg=self.config)
        preflight = r.registration_network_preflight(proxy=s.proxy, proxy_attempts=2)
        s.proxy = str(preflight.get("proxy") or s.proxy or "")
        s.mailbox = r._ensure_mailbox_account(self.input_mailbox)
        if not s.mailbox or not s.mailbox.email:
            self._abort("mailbox_required")
        s.username = str(getattr(s.mailbox, "email", "") or "").strip()
        self._persist_checkpoint("mailbox_ready")
        from .mailbox_service import MailboxService
        s.mailbox_service = MailboxService.create(self.config)
        chatgpt_cfg = self.config.get("chatgpt", {})
        s.auth_base = chatgpt_cfg.get("auth_base_url", "https://auth.openai.com")
        s.chat_base = chatgpt_cfg.get("chat_base_url", "https://chatgpt.com")
        from .paypal_proxy import infer_proxy_country
        r.set_fingerprint_geo(infer_proxy_country(s.proxy))
        self.machine.transition(RegistrationState.MAILBOX_READY)
        if self._has_resume_checkpoint():
            print("[*] Resumable post-create checkpoint found; skipping mailbox/OTP stages")
            return
        print("[*] ChatGPT Email Registration Started")
        self._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", self.extract_sentinel)
        self._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", self.prepare_identity)

    def extract_sentinel(self) -> None:
        r = self.r
        s = self.runtime
        if self.input_sentinel:
            print("[*] Using provided sentinel tokens")
            s.sentinel_data = self.input_sentinel
        else:
            s.sentinel_data = r._extract_sentinel(
                proxy=s.proxy,
                force_fresh=True,
                persist=False,
                browser_headless=self.browser_headless,
            )
        if not s.sentinel_data or not s.sentinel_data.get("sentinel_token"):
            self._abort("sentinel_extract_failed")
        r.think_stage("post_sentinel")

    def prepare_identity(self) -> None:
        r = self.r
        s = self.runtime
        from .storage import get_device_context

        device_context = dict(get_device_context(getattr(s.mailbox, "email", "")) or {})
        stored_device_id = str(device_context.get("device_id") or "").strip()
        sentinel_device_id = str(r._sentinel_device_id(s.sentinel_data) or "").strip()
        if stored_device_id and stored_device_id != sentinel_device_id:
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
            get_device_context=get_device_context,
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
        s.session = curl_requests.Session()
        if s.proxy:
            s.session.proxies = {"http": s.proxy, "https": s.proxy}
        if s.registration_mode == "passwordless":
            # Keep the Web/NextAuth flow isolated from the Sentinel extraction
            # prime session. Importing its auth.openai.com login cookies creates
            # a stale login transaction and routes authorize to /log-in/password.
            r._set_oai_did_cookie(s.session, s.device_id)
        else:
            r._import_sentinel_cookies(s.session, s.sentinel_data, s.device_id)
        from .paypal_proxy import infer_proxy_country
        r.set_fingerprint_geo(infer_proxy_country(s.proxy))
        r.set_fingerprint_device(s.device_id)
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

    def auth_flow(self) -> None:
        r = self.r
        s = self.runtime
        s.auth_flow_started = int(time.time())
        if s.registration_mode == "passwordless":
            r.request_with_retry(
                s.session, "get", f"{s.chat_base}/", label="ChatGPT prime",
                headers={**r.chatgpt_headers(s.device_id, session_id=s.session_logging_id, flow_invocation_id=s.flow_invocation_id, accept="text/html,application/xhtml+xml", referer=f"{s.chat_base}/")},
                impersonate=r.auth_impersonate(),
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
        r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_signup_state")
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

    def user_register(self) -> None:
        r = self.r
        s = self.runtime
        password_fallback = bool(s.signup_state.get("password_fallback")) or r._is_signup_password_step(s.signup_state.get("url", ""))
        if s.registration_mode == "passwordless" and not password_fallback:
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
        s.reg_response = r.request_with_retry(
            s.session, "post", f"{s.auth_base}/api/accounts/user/register", label="User register",
            json={"password": s.password, "username": s.username},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/create-account/password",
                origin=s.auth_base,
                sentinel_token=s.sentinel_token,
            ),
            impersonate=r.auth_impersonate(),
        )
        try:
            s.reg_data = s.reg_response.json()
        except (ValueError, TypeError):
            s.reg_data = {"_raw": s.reg_response.text[:300]}
        print(f"  Status: {s.reg_response.status_code}")
        print(f"  Response: {r._sanitize_text(json.dumps(s.reg_data, ensure_ascii=False)[:300])}")
        if s.reg_response.status_code != 200:
            err_code = s.reg_data.get("error", {}).get("code", "")
            err_msg = s.reg_data.get("error", {}).get("message", str(s.reg_data))
            state_url = str(s.signup_state.get("url") or "")
            if err_code == "invalid_auth_step" and "email-verification" in state_url:
                print("  Account already in email-verification flow, resuming OTP step...")
                s.resume_email_verification = True
            else:
                self._abort(f"user_register:{err_msg}")

    def send_email_otp(self) -> None:
        r = self.r
        s = self.runtime
        r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)
        continue_url = r._email_otp_send_url(
            s.reg_data,
            s.auth_base,
            s.resume_email_verification,
        )
        otp_send_started = int(time.time())
        password_fallback = bool(s.signup_state.get("password_fallback")) or r._is_signup_password_step(s.signup_state.get("url", ""))
        if s.registration_mode == "passwordless" and not password_fallback:
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
        r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_otp_send")
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
            self._abort("email_otp_poll_timeout")

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
        create_sentinel_token = r._create_account_sentinel_token(s.sentinel_data, proxy=s.proxy)
        response = r.request_with_retry(
            s.session, "post", f"{s.auth_base}/api/accounts/create_account", label="Create account",
            json={"name": s.full_name, "birthdate": s.birthdate},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/about-you",
                origin=s.auth_base,
                sentinel_token=create_sentinel_token,
                sentinel_so_token=s.sentinel_so_token,
            ),
            impersonate=r.auth_impersonate(),
        )
        try:
            s.create_data = response.json()
        except (ValueError, TypeError):
            s.create_data = {"_raw": response.text[:300]}
        print(f"  Status: {response.status_code}")
        print(f"  Response: {r._sanitize_text(json.dumps(s.create_data, ensure_ascii=False)[:300])}")
        s.create_ok = response.status_code == 200
        r.think_stage("post_create_account")
        s.existing_account = r._is_user_already_exists(s.create_data)
        c = s.context
        s.password_unknown = bool(
            s.resume_email_verification
            and c is not None
            and not (c.explicit_password or c.password_from_storage)
        ) or s.password_unknown
        if not s.create_ok and s.existing_account:
            print("  Account already exists, password may differ from generated one; clearing stored password.")
            s.create_ok = True
            s.password_unknown = True
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

    def fetch_auth_session(self) -> None:
        r = self.r
        s = self.runtime
        s.auth_session = r._fetch_auth_session(s.session, s.chat_base, s.base_headers)
        s.auth_body = s.auth_session.get("body") or {}
        s.access_token = r._auth_session_access_token(s.auth_body)
        self._persist_checkpoint("at_probe_pending")
        if not s.existing_account or s.access_token:
            return
        print("  Existing account has no ChatGPT session yet; retrying with passwordless email login...")
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
            )
        except Exception as exc:
            existing_login = {"ok": False, "error": f"existing_login_transport:{exc}"}
        if not existing_login.get("ok"):
            print(f"  Existing account login failed: {r._sanitize_text(existing_login.get('error') or 'unknown')}")
            return
        s.auth_session = r._fetch_auth_session(s.login_session, s.chat_base, s.base_headers)
        s.auth_body = s.auth_session.get("body") or {}
        s.access_token = r._auth_session_access_token(s.auth_body)
        self._persist_checkpoint("at_probe_pending")
        if s.access_token:
            old_session = s.session
            s.session = s.login_session
            s.login_session = old_session

    def collect_codex_oauth(self) -> None:
        r = self.r
        s = self.runtime
        if not s.create_ok or not self.codex_oauth:
            if s.create_ok:
                print("  Codex OAuth refresh token skipped (AT-only registration mode)")
            return
        try:
            from .codex_oauth import collect_codex_oauth_tokens

            oauth_seed = {
                "email": s.username,
                "password": "" if s.password_unknown else s.password,
                "device_id": s.device_id,
                "cookie_header": r._cookie_header(s.session),
                "auth_session": s.auth_body,
                "mailbox": r._mailbox_snapshot(s.mailbox),
                "registration_email_otp": s.email_code,
            }
            s.oauth_result = collect_codex_oauth_tokens(
                data=oauth_seed,
                session=s.session,
                proxy=s.proxy,
                timeout=int((self.config.get("codex_oauth") or {}).get("registration_timeout", 180)),
                force_email_otp_login=bool(s.resume_email_verification or s.password_unknown),
                phone_pool=self.phone_pool,
                browser_headless=self.browser_headless,
            )
        except Exception as exc:
            s.oauth_result = {"ok": False, "error": f"codex_oauth_transport:{exc}"}
        if s.oauth_result.get("ok"):
            s.oauth_tokens = s.oauth_result.get("tokens") or {}
            s.access_token = s.oauth_tokens.get("access_token") or s.access_token
            s.id_token = s.oauth_tokens.get("id_token", "")
            s.oauth_refresh_token = s.oauth_tokens.get("refresh_token", "")
            s.phone_result = s.oauth_result.get("phone_attempt") or {}
            if s.phone_result.get("ok"):
                print(
                    f"  Phone verified: {s.phone_result.get('phone', '')} "
                    f"(reuse {s.phone_result.get('reuse_count', 0)}/{s.phone_result.get('max_reuse_count', 0)})"
                )
            print("  OAuth refresh token captured" if s.oauth_refresh_token else "  Codex OAuth exchange returned no refresh token")
        else:
            s.phone_result = s.oauth_result.get("phone_attempt") or {}
            print(f"  Codex OAuth refresh token failed: {r._sanitize_text(s.oauth_result.get('error', 'unknown'))}")

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

    def _set_outcome(self) -> None:
        r = self.r
        s = self.runtime
        s.success, s.error, s.registration_warning = r._registration_outcome(
            s.create_ok,
            s.create_data,
            s.access_token,
            s.at_probe,
        )
        require_refresh_token = r._registration_requires_refresh_token(self.config) if self.codex_oauth else False
        require_phone = r._registration_requires_phone_verification(self.phone_pool, self.config) if self.codex_oauth else False
        s.post_registration_ready = (
            (not require_refresh_token or bool(s.oauth_refresh_token))
            and (not require_phone or bool(s.phone_result.get("ok")))
        )

    def enroll_totp(self) -> None:
        r = self.r
        s = self.runtime
        if not (s.success and s.access_token and s.mailbox):
            return
        if not self.enroll_2fa:
            print("  [2FA] Enrollment disabled (--no-2fa)")
            return
        def poll_reauth_otp(email: str, issued_after_unix: int = 0, timeout: int = 120, **kwargs: Any) -> str:
            return s.mailbox_service.poll_otp(
                s.mailbox,
                subject_keyword=r.REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
                timeout=int(timeout or 120),
                issued_after_unix=issued_after_unix,
                proxy=s.proxy,
                excluded_otps=kwargs.get("excluded_otps") or ({s.email_code} if s.email_code else set()),
            )

        def reauth_existing_account() -> str:
            login = r._login_existing_account_with_email_otp(
                session=s.session,
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
            )
            if not login.get("ok"):
                raise RuntimeError(str(login.get("error") or "existing_account_reauth_failed"))
            auth_session = r._fetch_auth_session(s.session, s.chat_base, s.base_headers)
            auth_body = auth_session.get("body") if isinstance(auth_session, dict) else {}
            refreshed_token = str(r._auth_session_access_token(auth_body or {}) or "").strip()
            if not refreshed_token:
                raise RuntimeError("existing_account_reauth_missing_access_token")
            return refreshed_token

        try:
            from .account_2fa import setup_totp_2fa

            s.twofa_result = setup_totp_2fa(
                session=s.session,
                email=s.username,
                access_token=s.access_token,
                did=s.device_id,
                base_headers=s.base_headers,
                poll_otp_fn=poll_reauth_otp,
                excluded_otps={s.email_code} if s.email_code else set(),
                reauth_login_fn=reauth_existing_account,
            )
            if s.twofa_result.get("ok"):
                s.totp_secret = s.twofa_result.get("totp_secret", "")
                s.access_token = s.twofa_result.get("access_token") or s.access_token
                print("  [2FA] TOTP enrolled")
            else:
                print(f"  [2FA] Enrollment skipped: {r._sanitize_text(s.twofa_result.get('error', 'unknown'))}")
        except ImportError as exc:
            s.twofa_result = {"ok": False, "error": f"pyotp_missing:{exc}"}
            print("  [2FA] pyotp not installed")
        except Exception as exc:
            s.twofa_result = {"ok": False, "error": str(exc)}
            print(f"  [2FA] Setup failed: {r._sanitize_text(exc)}")

    def finalize(self) -> dict[str, Any]:
        r = self.r
        s = self.runtime
        from .auth_headers import current_auth_fingerprint
        from .token_telemetry import access_token_telemetry
        from .paypal_proxy import infer_proxy_country
        from .sentinel_quickjs import sentinel_version

        fingerprint = current_auth_fingerprint()
        token_telemetry = access_token_telemetry(s.access_token)
        result = {
            "success": s.success,
            "error": r._sanitize_text(s.error),
            "email": s.username,
            "source": "register",
            "register_method": "email" if s.registration_mode != "phone" else "phone",
            "session_type": "at_only" if s.registration_mode == "at_only" else "web",
            "plan_type": "unknown",
            "phone": s.phone_result.get("phone", "") if s.phone_result.get("ok") else "",
            "password": "" if s.password_unknown else s.password,
            "name": s.full_name,
            "birthdate": s.birthdate,
            "response": {
                "register": s.reg_data,
                "email_otp": s.otp_data,
                "create_account": s.create_data,
                "auth_session": s.auth_body,
                "phone_verification": s.phone_result,
                "codex_oauth": r._oauth_result_summary(s.oauth_result),
                "access_token_probe": s.at_probe,
            },
            "auth_session": s.auth_body,
            "access_token": s.access_token or "",
            "id_token": s.id_token,
            "oauth_refresh_token": s.oauth_refresh_token,
            "refresh_token_status": "oauth_present" if s.oauth_refresh_token else "no_rt",
            "quota_status": s.at_probe.get("quota_status", ""),
            "quota": {
                "status": s.at_probe.get("quota_status", ""),
                "updated_at": int(time.time()),
                "last_result": s.at_probe,
            } if s.at_probe else {},
            "totp_secret": s.totp_secret or "",
            "totp_enrolled": bool(s.totp_secret) or bool(s.twofa_result.get("already_enrolled")),
            "twofa_enrolled_at": int(time.time()) if (s.totp_secret or s.twofa_result.get("already_enrolled")) else 0,
            "twofa_enrollment": s.twofa_result or {"ok": False, "reason": "skipped"},
            "registration_success_basis": "at_http_200" if s.success else "",
            "registration_state": "active" if s.success else ("terminal" if "account_deactivated" in s.error else "failed"),
            "access_token_telemetry": token_telemetry,
            "auth_fingerprint_profile": str(fingerprint.get("impersonate") or ""),
            "sentinel_version": sentinel_version(),
            "registration_country": infer_proxy_country(s.proxy),
            "registration_warning": r._sanitize_text(s.registration_warning),
            "post_registration_ready": s.post_registration_ready,
            "cookie_header": s.auth_session.get("cookie_header", ""),
            "registration_mode": s.registration_mode,
            "device_id": s.device_id,
            "auth_session_logging_id": s.session_logging_id,
            "timing": r._timing_summary(),
            "mailbox": r._mailbox_snapshot(s.mailbox),
        }
        self.machine.transition(RegistrationState.COMPLETED)
        if r._retain_registration_checkpoint(s.success, s.access_token, s.at_probe):
            print("  [Checkpoint] Retaining post-create state for AT probe retry")
        else:
            try:
                from .storage import clear_registration_checkpoint

                clear_registration_checkpoint(s.username, runtime_config=self.config)
            except Exception:
                pass
        result["registration_machine"] = self.machine.snapshot()
        return result

    def _close_sessions(self) -> None:
        seen: set[int] = set()
        for session in (self.runtime.session, self.runtime.login_session):
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
