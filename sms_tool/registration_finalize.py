"""Result-assembly and credential-enrollment stages of the registration workflow.

Extracted from ``registration_handlers.RegistrationEmailWorkflow``
(2026-09-19, P1 split): that module was the god-object holding HTTP calls,
the state machine, checkpointing, 2FA, result assembly and progress
printing in one 1700-line class.  The three methods here --
``obtain_oauth_refresh_token``, ``enroll_totp`` and ``finalize`` -- are
moved verbatim as module-level functions whose first parameter is the
workflow instance; the class keeps thin delegates so no caller changes.
Delayed imports inside the bodies are kept exactly as they were (they
guard against import cycles), so behaviour is unchanged.
"""

from __future__ import annotations

import time
from typing import Any

from . import registration_checkpoint
from .auth_headers import current_auth_fingerprint
from .codex_oauth import collect_codex_oauth_tokens
from .registration_outcome import needs_manual_session_recovery
from .registration_result import build_registration_result
from .registration_retry_guard import RegistrationRetryGuard
from .registration_state import RegistrationState


def obtain_oauth_refresh_token(self) -> None:
    """Exchange an OAuth refresh token while this run's session is still live.

    **Opt-in** (``registration.obtain_refresh_token``, default off) because
    the email lane is deliberately AT-only: OAuth recovery has its own entry
    points, and pulling it in here used to add hidden dependencies.

    Why it is worth having at all: ``oauth_refresh_token`` is declared
    (``registration_runtime``), persisted (``finalize``) and summarised
    (``_oauth_result_summary``) but has **no assignment anywhere in the
    package**, so every account this lane registers is stored
    ``refresh_token_status="no_rt"``.  The recovery chain then skips its only
    free strategy (``oauth_refresh_token``) for that account and pays an
    email code or a browser instead -- measured 0/1245 accounts hold an RT.

    Cost is near zero **when the session is live**, which is the whole point
    of doing it here: ``collect_codex_oauth_tokens`` follows the OAuth
    authorize URL first and, when the redirect already carries a callback
    ``code``, exchanges it immediately -- no login stage, no email code
    (pinned by ``tests/test_codex_oauth.py::LiveSessionShortCircuitTests``;
    before that test existed this was only a code read, and the one
    codex_oauth run in the logs took the ``email_otp`` path because it ran
    on an already-dead session).  A dead session is the expensive case, and
    that is precisely what this placement avoids.  Hence
    ``force_email_otp_login=False``: forcing it would pay the code this is
    meant to save.

    Whether it was actually free is recoverable from the log line below:
    ``collect_codex_oauth_tokens`` tags its result with ``login_stage``
    (``live_session`` = the short circuit fired and no code was paid;
    ``login_required`` = the login stage machine ran, so a code was -- and
    on a failure, was wasted).  Without that tag ``mode`` is the same on
    every branch, so "the switch is on and every account still burned a
    code" would be indistinguishable from "the switch is on and it worked".

    Never raises and never touches ``s.success``/``s.error`` -- a failure is
    only recorded on ``s.oauth_result``.
    """
    r = self.r
    s = self.runtime
    if not self._obtain_refresh_token_enabled():
        return
    if not (s.success and s.access_token):
        return

    try:
        s.oauth_result = collect_codex_oauth_tokens(
            data={
                "email": s.username,
                "cookie_header": s.auth_session.get("cookie_header", ""),
                "access_token": s.access_token,
                "device_id": s.device_id,
            },
            session=s.session,
            proxy=s.proxy or None,
            force_email_otp_login=False,
        ) or {}
    except Exception as exc:
        s.oauth_result = {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": r._sanitize_text(exc),
        }
    if s.oauth_result.get("ok"):
        tokens = s.oauth_result.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        # Only the refresh token is taken.  The Codex OAuth access token is
        # scoped to the Codex client, so it is *not* a substitute for this
        # run's ChatGPT web AT -- the one just probed, and the one the
        # persisted ``access_token`` has to stay.
        s.oauth_refresh_token = str(
            tokens.get("refresh_token") or s.oauth_refresh_token or ""
        ).strip()
    print(
        f"  OAuth refresh token: {r._oauth_result_summary(s.oauth_result)} "
        f"refresh_token_status={'oauth_present' if s.oauth_refresh_token else 'no_rt'}"
    )


def enroll_totp(self) -> None:
    # Delayed import: registration_handlers imports this module, so the
    # helper it still owns has to be fetched here, not at module top.
    from .registration_handlers import _login_probe_password

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
        # Same lane as ``fetch_auth_session``, so it needs the same two
        # inputs.  Without the password the probe's positive verdict has
        # nothing to submit and the re-auth can only answer
        # ``password_required``; without the TOTP secret a password login
        # cannot clear the account's own MFA challenge.  The secret is read
        # from storage because enrollment is exactly what has not happened
        # yet in this run.
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
            password=_login_probe_password(s),
            totp_secret=str(s.totp_secret or "") or r._stored_registration_totp(s.username),
            allow_passwordless=not s.existing_account,
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
        from .accounts.account_2fa import setup_totp_2fa

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
    from .token_telemetry import access_token_telemetry
    from .paypal_proxy import infer_proxy_country
    from .sentinel.bundle import sentinel_version

    fingerprint = current_auth_fingerprint()
    token_telemetry = access_token_telemetry(s.access_token)
    from .accounts.account_identity import create_registration_identity, complete_registration_identity
    identity_context = complete_registration_identity(
        create_registration_identity(
            s.proxy,
            pool_index=int(self.proxy_metadata.get("pool_index", -1) or -1),
            fingerprint_key=str(fingerprint.get("impersonate") or ""),
            device_id=s.device_id,
            auth_session_logging_id=s.session_logging_id,
            config=self.config,
        ),
        device_id=s.device_id,
        auth_session_logging_id=s.session_logging_id,
    )
    result = build_registration_result(
        success=s.success,
        registration_mode=s.registration_mode,
        registration_state="active" if s.success else (
            "partial_registered" if s.existing_account else
            ("terminal" if "account_deactivated" in s.error else "failed")
        ),
        email=s.username,
        error=s.error,
        register_method="email" if s.registration_mode != "phone" else "phone",
        session_type="at_only" if s.registration_mode == "at_only" else "web",
        password="" if s.password_unknown else s.password,
        name=s.full_name,
        birthdate=s.birthdate,
        access_token=s.access_token or "",
        id_token=s.id_token,
        auth_session=s.auth_body,
        cookie_header=s.auth_session.get("cookie_header", ""),
        device_id=s.device_id,
        identity_context=identity_context,
        auth_fingerprint_profile=str(fingerprint.get("impersonate") or ""),
        response={
            "register": s.reg_data,
            "email_otp": s.otp_data,
            "create_account": s.create_data,
            "auth_session": s.auth_body,
            "phone_verification": s.phone_result,
            "codex_oauth": r._oauth_result_summary(s.oauth_result),
            "access_token_probe": s.at_probe,
        },
        quota_status=s.at_probe.get("quota_status", ""),
        totp_secret=s.totp_secret or "",
        twofa_enrollment=s.twofa_result or {"ok": False, "reason": "skipped"},
        registration_success_basis="at_http_200" if s.success else "",
        registration_country=infer_proxy_country(s.proxy),
        registration_warning=s.registration_warning,
        post_registration_ready=s.post_registration_ready,
        mailbox_snapshot=r._mailbox_snapshot(s.mailbox),
        proxy_audit=self.proxy_metadata,
        extra={
            "phone": s.phone_result.get("phone", "") if s.phone_result.get("ok") else "",
            "oauth_refresh_token": s.oauth_refresh_token,
            "refresh_token_status": "oauth_present" if s.oauth_refresh_token else "no_rt",
            "quota": {
                "status": s.at_probe.get("quota_status", ""),
                "updated_at": int(time.time()),
                "last_result": s.at_probe,
            } if s.at_probe else {},
            "totp_enrolled": bool(s.totp_secret) or bool(s.twofa_result.get("already_enrolled")),
            "twofa_enrolled_at": int(time.time()) if (s.totp_secret or s.twofa_result.get("already_enrolled")) else 0,
            "access_token_telemetry": token_telemetry,
            "sentinel_version": sentinel_version(),
            "auth_session_logging_id": s.session_logging_id,
            "timing": r._timing_summary(),
            # 方案 B（2026-09-16）：把「已创建但无 session 且无密码」的账号从普通
            # 失败里分出来。判据只有 ``needs_manual_session_recovery`` 一个 owner，
            # ``_abort_result`` 那条装配路径走的是同一个函数。
            # ⚠️ 这个键必须同时在 ``store/accounts.record_registration_audit`` 的
            # ``detail`` **封闭白名单**里，否则审计表写不进去 —— 09-15 前
            # ``existing_login`` 在 4377 行里出现 0 次就是这个原因。
            "needs_manual_session_recovery": needs_manual_session_recovery(
                success=s.success,
                access_token=s.access_token,
                existing_account=s.existing_account,
                existing_account_password_known=s.existing_account_password_known,
            ),
        },
    )
    if self.post_process_result is not None:
        result = self.post_process_result(result)
    if s.success and s.existing_account:
        RegistrationRetryGuard(self.config).record(s.username, success=True)
    self.machine.transition(RegistrationState.COMPLETED)
    if s.create_ok and not s.existing_account and not s.access_token:
        self._persist_checkpoint(registration_checkpoint.SESSION_PENDING_STATE)
        print("  [Checkpoint] Retaining created-account session; retry will not repeat signup or OTP")
    elif r._retain_registration_checkpoint(s.success, s.access_token, s.at_probe):
        print("  [Checkpoint] Retaining post-create state for AT probe retry")
    else:
        try:
            self.persistence.clear_checkpoint(s.username, runtime_config=self.config)
        except Exception:
            pass
    result["registration_machine"] = self.machine.snapshot()
    return result
