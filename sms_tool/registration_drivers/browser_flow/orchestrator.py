"""入口编排：run_browser_registration 把上面各层串成完整注册流程。依赖全部下层。"""

from __future__ import annotations

import time
import uuid

from .dom_fields import _click_resend, _config_value, _otp_fields, _page_is_alive, _prepare_session_page, _safe_text
from .flow_steps import _browser_session_scope, _poll_browser_otp, _restart_email_otp_flow
from .form_steps import _complete_profile, _fill_email, _fill_otp, _fill_password_if_present, _maybe_accept_cookies, _maybe_dismiss_chatgpt_onboarding
from .page_state import _ensure_signup_page_ready, _manual_challenge, _post_otp_registration_state, _profile_completion_required, _wait_after_otp_submit, _wait_for_challenge_clear, _wait_for_profile_completion, _wait_for_registration_state
from .session import _bind_totp_in_browser, _browser_access_token_probe, _browser_diagnostics, _browser_failure_class, _post_registration_dwell, _safe_proxy_audit, _session_payload
from .context import prepare_browser_context
from .recovery import is_navigation_retryable, probe_pending, stage_timeout

from ...mailbox import _ensure_mailbox_account
from ...mailbox_service import MailboxService
from ...phone_proxy import redact_proxy_text
from ...registration_outcome import _browser_mailbox_snapshot, _mailbox_snapshot, _registration_outcome
from ...registration_progress import registration_stage
from ...registration_state import RegistrationState, RegistrationStateMachine
from ...utils import _generate_password, _random_birthdate, _random_name
from ..base import BrowserRegistrationError, driver_capabilities, normalize_registration_driver
from ..external_sessions import _driver_config, create_browser_session
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def _goto_with_retry(page: Any, url: str, *, timeout_ms: int, attempts: int = 2) -> int:
    """Navigate with bounded recovery for transient proxy/edge failures.

    Camoufox can abort a ``domcontentloaded`` navigation after the document
    has already committed. A retry using ``commit`` lets the state classifier
    inspect that document instead of treating the abort as an account failure.
    """
    last_error = None
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        try:
            wait_until = "domcontentloaded" if attempt == 1 else "commit"
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return attempt
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not is_navigation_retryable(exc):
                raise
            registration_stage(
                "auth_flow_retry",
                detail=f"goto_retry={attempt};wait_until={wait_until};error={type(exc).__name__}",
            )
            try:
                page.wait_for_timeout(min(2_000, max(250, timeout_ms // 20)))
            except Exception:
                time.sleep(0.5)
    raise last_error


def run_browser_registration(
    *,
    driver_name: str,
    proxy: str | None,
    password: str | None,
    mailbox: Any,
    config: Mapping[str, Any],
    browser_headless: bool | None = None,
    enroll_2fa: bool = True,
    probe_fn=None,
    session_factory=create_browser_session,
    proxy_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one browser registration; result matches the protocol result contract."""
    try:
        driver_name = normalize_registration_driver(driver_name)
    except ValueError as exc:
        return {
            "success": False,
            "registration_driver": str(driver_name or ""),
            "error": _safe_text(str(exc)),
            "failure_class": "configuration",
        }
    if driver_name == "protocol":
        return {
            "success": False,
            "registration_driver": driver_name,
            "error": "unsupported_registration_driver:protocol",
            "failure_class": "configuration",
        }
    try:
        mailbox = _ensure_mailbox_account(mailbox)
    except Exception as exc:
        return {
            "success": False,
            "registration_driver": driver_name,
            "error": "browser_mailbox_setup_failed",
            "failure_class": "mailbox",
            "mailbox": _browser_mailbox_snapshot(mailbox),
        }
    email = str(getattr(mailbox, "email", "") or "").strip()
    if not email:
        return {"success": False, "error": "mailbox_required", "registration_driver": driver_name}
    password = str(password or "").strip()
    if not password:
        password = _generate_password()
    full_name = " ".join(_random_name())
    birthdate = _random_birthdate()
    context = prepare_browser_context(config, driver_name, browser_headless)
    selected_cfg = context.driver_config
    headless = context.headless
    timeout = context.timeout
    locale = context.locale
    timezone_id = context.timezone_id
    otp_timeout = context.otp_timeout
    try:
        mailbox_service = MailboxService.create(config)
    except Exception as exc:
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": "browser_mailbox_service_unavailable",
            "failure_class": "network",
            "mailbox": _browser_mailbox_snapshot(mailbox),
        }
    started = int(time.time())
    machine = RegistrationStateMachine(registration_stage)
    machine.transition(RegistrationState.MAILBOX_READY)
    from ...storage import get_device_context
    device_context = get_device_context(email, runtime_config=config)
    device_id = str((device_context or {}).get("device_id") or uuid.uuid4())
    machine.transition(RegistrationState.IDENTITY_READY)
    chat_base = context.chat_base
    auth_base = context.auth_base
    start_url = context.start_url
    page = None
    diagnostics = {"driver": driver_name, "url_host": "", "title": ""}
    account_key = email
    attempt_number = 1
    try:
        attempt_number = max(1, int((proxy_metadata or {}).get("attempt") or 1))
    except (TypeError, ValueError):
        attempt_number = 1
    profile_key = account_key if attempt_number == 1 else f"{account_key}__retry{attempt_number}"
    browser_identity: dict[str, Any] = {
        "driver": driver_name,
        # Failed attempts get an isolated on-disk profile so stale auth pages
        # cannot make the next retry miss the signup email field.
        "profile_id": profile_key,
    }
    # --- Browser fingerprint pool + exit-geo alignment -----------------------
    # Mirror turb-gpt-free-register's BROWSER_PROFILE_POOL + _detect_exit_geo:
    # draw a per-account browser profile (seed-stable by device_id) and align
    # its locale/timezone to the proxy's real egress.  Geo detection degrades
    # to {} on any failure, in which case we keep the configured locale/tz so
    # registration never blocks on the network probe.
    from ...browser_fingerprint_pool import (
        detect_proxy_exit_geo,
        select_browser_profile,
    )

    _browser_geo_enabled = bool(_config_value(config, "browser_geo_alignment", True))
    _browser_geo = detect_proxy_exit_geo(proxy, enabled=_browser_geo_enabled)
    _browser_profile = select_browser_profile(_browser_geo, seed=device_id, config=config)
    if _browser_profile.get("navigator_language"):
        locale = str(_browser_profile["navigator_language"])
    if _browser_profile.get("timezone_iana"):
        timezone_id = str(_browser_profile["timezone_iana"])
    # Viewport (screen) is only applied to the local Playwright driver; external
    # anti-detect browsers (Roxy/Cloak/Camoufox/cloud) manage their own screen.
    _browser_viewport = None
    if driver_name == "playwright":
        _browser_viewport = (
            int(_browser_profile.get("screen_width") or 1440),
            int(_browser_profile.get("screen_height") or 900),
        )
    try:
        with _browser_session_scope(
            driver_name=driver_name, config=config, proxy=proxy, headless=headless,
            timeout_ms=max(5_000, timeout * 1_000), locale=locale, timezone_id=timezone_id,
            browser_identity=browser_identity, viewport=_browser_viewport,
            session_factory=session_factory,
        ) as browser:
            if driver_name in {"roxy", "cloak"}:
                from ..external_sessions import verify_browser_proxy_country

                verification = verify_browser_proxy_country(browser, expected_country=str((proxy_metadata or {}).get("expected_country") or ""), timeout_seconds=min(20, timeout))
                if proxy_metadata is not None:
                    proxy_metadata = dict(proxy_metadata)
                    proxy_metadata["actual_country"] = verification.get("actual_country", "")
                if not verification.get("ok"):
                    raise BrowserRegistrationError(f"{driver_name}_proxy_country_mismatch", str(verification.get("error") or "unknown"))
            page = browser.page
            browser.add_device_cookie(device_id, chat_base, auth_base)
            machine.transition(RegistrationState.AUTH_FLOW)
            _goto_with_retry(page, start_url, timeout_ms=timeout * 1_000)
            _maybe_accept_cookies(page)
            # P1 risk-control: replay a real browser's ChatGPT first-screen
            # request sequence (mirrors turb-gpt-free-register's
            # core/chatgpt_bootstrap.py).  Config-gated (default off) and
            # non-fatal by construction, so it can never gate registration.
            from ...chatgpt_bootstrap import run_anonymous_bootstrap

            _anon_bootstrap = run_anonymous_bootstrap(page, config)
            try:
                _ensure_signup_page_ready(
                    page,
                    timeout_seconds=stage_timeout(config, "auth_flow", min(45, timeout), maximum=120),
                    config=config,
                )
            except BrowserRegistrationError as exc:
                # A committed SPA document can miss the email field while its
                # auth shell is still mounting. Reuse the same proxy once, but
                # reload the document before allowing the batch retry policy to
                # consume another mailbox attempt.
                if exc.code != "browser_email_field_missing":
                    raise
                registration_stage("auth_flow_retry", detail="signup_ready_reload")
                try:
                    page.reload(wait_until="commit", timeout=max(5_000, timeout * 1_000))
                except Exception:
                    pass
                _ensure_signup_page_ready(
                    page,
                    timeout_seconds=stage_timeout(config, "auth_flow", min(30, timeout), maximum=120),
                    config=config,
                )
            from ...mailbox import _snapshot_mailbox_message
            _snapshot_mailbox_message(mailbox, proxy=proxy)
            started = int(time.time())
            _fill_email(page, email, config=config)
            machine.transition(RegistrationState.USER_REGISTER)
            state = _wait_for_registration_state(
                page,
                stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                browser=browser,
                config=config,
            )
            if state == "challenge":
                if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
                state = _wait_for_registration_state(
                    page,
                    stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                    browser=browser,
                    config=config,
                )
                if state == "challenge":
                    raise BrowserRegistrationError("manual_challenge_required")
            if state == "identity_provider":
                raise BrowserRegistrationError("browser_unexpected_identity_provider")
            if state == "login_password":
                raise BrowserRegistrationError("browser_existing_account")
            if state == "unknown":
                raise BrowserRegistrationError("browser_registration_state_unknown")
            if state == "otp":
                password_used = False
            elif state == "password":
                password_used = _fill_password_if_present(page, password, config=config)
            else:
                password_used = False
            machine.transition(RegistrationState.EMAIL_OTP_SEND)
            if password_used:
                state = _wait_for_registration_state(
                    page,
                    stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                    browser=browser,
                    config=config,
                )
                if state == "challenge":
                    if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                        raise BrowserRegistrationError("manual_challenge_required")
                    state = _wait_for_registration_state(
                        page,
                        stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                        browser=browser,
                        config=config,
                    )
                    if state == "challenge":
                        raise BrowserRegistrationError("manual_challenge_required")
                if state == "login_password":
                    raise BrowserRegistrationError("browser_existing_account")
                if state == "identity_provider":
                    raise BrowserRegistrationError("browser_unexpected_identity_provider")
                if state == "unknown":
                    raise BrowserRegistrationError("browser_registration_state_unknown")
            fields = _otp_fields(page)
            if fields is not None:
                machine.transition(RegistrationState.EMAIL_OTP_WAIT)
                excluded_otps: set[str] = set()
                for otp_attempt in range(3):
                    otp = _poll_browser_otp(
                        mailbox_service,
                        mailbox,
                        browser=browser,
                        page=page,
                        driver_name=driver_name,
                        subject_keyword="verification code|login code",
                        timeout=otp_timeout,
                        issued_after_unix=started,
                        proxy=proxy,
                        excluded_otps=excluded_otps,
                    )
                    if not otp:
                        # Roxy reference flow retries the latest message without the
                        # send-time/code filters because OpenAI may update or reuse the
                        # same mailbox item after a resend.
                        if otp_attempt > 0:
                            otp = mailbox_service.poll_otp(
                                mailbox,
                                subject_keyword="verification code|login code",
                                timeout=min(15, otp_timeout),
                                issued_after_unix=0,
                                proxy=proxy,
                                excluded_otps=set(),
                            )
                        if not otp:
                            if otp_attempt < 2:
                                restarted = False
                                if not _page_is_alive(page):
                                    try:
                                        page, _ = _restart_email_otp_flow(
                                            browser, page, start_url=start_url, email=email,
                                            password=password, timeout_seconds=timeout,
                                            config=config,
                                        )
                                        restarted = True
                                    except Exception:
                                        restarted = False
                                if restarted or _click_resend(page):
                                    started = int(time.time())
                                    continue
                            raise BrowserRegistrationError("browser_email_otp_timeout")
                    page = getattr(browser, "page", None) or page
                    excluded_otps.add(str(otp))
                    machine.transition(RegistrationState.EMAIL_OTP_VALIDATE)
                    _fill_otp(page, str(otp))
                    outcome = _wait_after_otp_submit(
                        page,
                        timeout_seconds=stage_timeout(
                            config, "email_otp_validate", min(30, timeout), maximum=120
                        ),
                    )
                    # Match the reference Roxy state machine: absence of an explicit
                    # validation error is accepted even when the old OTP DOM remains
                    # mounted during a slow SPA navigation.
                    if outcome == "accepted":
                        break
                    if outcome == "invalid" and otp_attempt < 2:
                        restarted = False
                        if not _page_is_alive(page):
                            try:
                                page, _ = _restart_email_otp_flow(
                                    browser, page, start_url=start_url, email=email,
                                    password=password, timeout_seconds=timeout,
                                    config=config,
                                )
                                restarted = True
                            except Exception:
                                restarted = False
                        if restarted or _click_resend(page):
                            started = int(time.time())
                else:
                    raise BrowserRegistrationError("browser_email_otp_rejected")
            state = _post_otp_registration_state(
                page,
                browser=browser,
                timeout_seconds=stage_timeout(
                    config, "create_account", min(30, max(5, timeout)), maximum=120
                ),
                config=config,
            )
            if state == "unknown":
                registration_stage("create_account_retry", detail="state_probe_retry")
                state = _post_otp_registration_state(
                    page,
                    browser=browser,
                    timeout_seconds=stage_timeout(
                        config, "create_account", min(45, max(10, timeout)), maximum=120
                    ),
                    config=config,
                )
            page = getattr(browser, "page", None) or page
            machine.transition(RegistrationState.CREATE_ACCOUNT)
            profile_required = _profile_completion_required(state)
            if profile_required:
                _complete_profile(page, full_name, birthdate)
                profile_done = _wait_for_profile_completion(
                    page,
                    timeout_seconds=stage_timeout(
                        config, "create_account", min(30, max(5, timeout)), maximum=120
                    ),
                    config=config,
                )
                if not profile_done:
                    registration_stage("create_account_retry", detail="profile_completion_retry")
                    profile_done = _wait_for_profile_completion(
                        page,
                        timeout_seconds=stage_timeout(
                            config, "create_account", min(20, max(5, timeout)), maximum=120
                        ),
                        config=config,
                    )
                if not profile_done:
                    raise BrowserRegistrationError("browser_profile_submit_timeout")
                page.wait_for_timeout(2_000)
            if _manual_challenge(page):
                if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
            chat_host = str(urlsplit(chat_base).hostname or "").lower()
            if hasattr(browser, "ensure_chatgpt_context"):
                page = _prepare_session_page(browser, page, timeout)
            elif chat_host and chat_host not in str(page.url or "").lower():
                _goto_with_retry(page, chat_base, timeout_ms=timeout * 1_000)
            _maybe_dismiss_chatgpt_onboarding(page, config=config)
            machine.transition(RegistrationState.AUTH_SESSION)
            session_info = _session_payload(
                browser,
                chat_base,
                email,
                timeout_seconds=stage_timeout(
                    config, "auth_session", timeout, minimum=15, maximum=120
                ),
            )
            auth_body = session_info["body"]
            access_token = session_info["access_token"]
            machine.transition(RegistrationState.ACCESS_TOKEN_PROBE)
            from ...registration_outcome import _probe_registration_access_token
            effective_probe_fn = probe_fn
            if effective_probe_fn is None:
                # Route the post-registration AT probe through the browser
                # context for ALL browser drivers, not just cloud ones.  This
                # keeps the probe on the same fingerprint and cookies used
                # during registration, preventing the identity drift that
                # causes immediate token revocation when curl_cffi switches
                # to a generic fingerprint.
                effective_probe_fn = lambda account, **kwargs: _browser_access_token_probe(
                    browser,
                    account,
                    timeout=int(kwargs.get("timeout") or timeout),
                )
            probe = _probe_registration_access_token(
                access_token, auth_body,
                # The browser context owns the egress and fingerprint for the
                # probe.  Passing proxy=None would break local drivers that
                # need the proxy for the curl fallback, so only suppress it
                # when the browser probe is active.
                proxy=(None if effective_probe_fn is not None else proxy),
                cfg=config, probe_fn=effective_probe_fn,
                stage_fn=registration_stage, sleep_fn=time.sleep,
            )
            success, error, warning = _registration_outcome(True, {}, access_token, probe)
            # A token plus a transport-unknown probe means the account was
            # created but the final check could not reach the endpoint. Keep a
            # resumable persistence state instead of discarding the account.
            at_probe_pending = probe_pending(access_token, probe, success)
            if success:
                _post_registration_dwell(config)
            machine.transition(RegistrationState.TOTP_ENROLL)
            # Attempt browser-based TOTP 2FA enrollment when requested and
            # registration succeeded.  Routes MFA API calls through the
            # browser's fetch to carry real cookies and Cloudflare clearance.
            twofa_result: dict[str, Any] = {"ok": False, "reason": "disabled"}
            totp_secret = ""
            if success and enroll_2fa:
                try:
                    twofa_result = _bind_totp_in_browser(page, access_token, device_id)
                    if twofa_result.get("ok"):
                        totp_secret = str(twofa_result.get("totp_secret") or "")
                    elif not twofa_result.get("error"):
                        twofa_result = {"ok": False, "reason": "browser_driver_deferred"}
                except Exception as exc:
                    twofa_result = {"ok": False, "reason": f"browser_totp_exception: {type(exc).__name__}"}
            elif enroll_2fa:
                twofa_result = {"ok": False, "reason": "registration_not_successful"}
            # P1: logged-in first-screen warm-up.  Deliberately placed *after*
            # 2FA enrollment — that enrollment protects the account, so it must
            # never be delayed by decorative warm-up traffic.
            if success:
                from ...chatgpt_bootstrap import run_authenticated_bootstrap

                _auth_bootstrap = run_authenticated_bootstrap(
                    page, access_token, device_id=device_id, config=config
                )
            machine.transition(RegistrationState.FINALIZE)
            from ...account_identity import create_registration_identity
            identity_context = create_registration_identity(
                proxy,
                pool_index=int((proxy_metadata or {}).get("pool_index", -1) or -1),
                device_id=str(device_id),
                account_key=account_key,
                browser_identity=browser_identity,
                config=config,
            )
            # Augment the persisted identity with the browser fingerprint-pool
            # selection + exit-geo.  Recorded as free-form identity_context keys
            # (persisted as-is) rather than overriding ``fingerprint_key`` (that
            # key is canonicalized against the protocol pool and would drop a
            # browser label).  Updating proxy_affinity.country to the detected
            # egress keeps fingerprint geo consistent with the browser locale.
            identity_context = dict(identity_context)
            identity_context["browser_fingerprint_profile"] = str(
                _browser_profile.get("browser_fingerprint_profile") or ""
            )
            identity_context["browser_profile_index"] = int(_browser_profile.get("browser_profile_index", 0))
            identity_context["fingerprint_seed"] = str(_browser_profile.get("fingerprint_seed") or device_id)
            identity_context["geo_country"] = str((_browser_geo or {}).get("country") or "")
            identity_context["geo_timezone"] = str((_browser_geo or {}).get("timezone") or "")
            identity_context["geo_ip"] = str((_browser_geo or {}).get("ip") or "")
            if _browser_geo_enabled:
                _geo_cc = str((_browser_geo or {}).get("country") or "").strip().upper()
                if _geo_cc:
                    _affinity = dict(identity_context.get("proxy_affinity") or {})
                    _affinity["country"] = _geo_cc
                    identity_context["proxy_affinity"] = _affinity
            # The protocol path records the registration country from the exit
            # proxy credential (registration_handlers.finalize ->
            # infer_proxy_country(s.proxy)).  The browser path previously omitted
            # it, so headless-registered sessions stored registration_country=""
            # and were impossible to attribute to a region for payment-matrix
            # routing / liveness geo checks.  Mirror the protocol path here.
            from ...paypal_proxy import infer_proxy_country

            result = {
                "success": success,
                "error": _safe_text(error),
                "email": email,
                "source": "register",
                "register_method": "email",
                "session_type": "web",
                "plan_type": "unknown",
                "password": password if password_used else "",
                "name": full_name,
                "birthdate": birthdate,
                "registration_driver": driver_name,
                "registration_mode": "browser",
                "registration_country": infer_proxy_country(proxy),
                "registration_success_basis": (
                    "at_http_200" if success else "at_probe_pending" if at_probe_pending else ""
                ),
                "registration_state": "at_probe_pending" if at_probe_pending else ("active" if success else "failed"),
                "registration_warning": _safe_text(warning),
                "access_token": access_token,
                "id_token": session_info.get("id_token", ""),
                "auth_session": auth_body,
                "cookie_header": browser.cookie_header(),
                "device_id": device_id,
                "identity_context": identity_context,
                # Mirror the protocol path (registration_handlers.finalize): record the
                # fingerprint profile this account was bound to. Previously headless
                # browser registrations left this empty, making the fingerprint-pool
                # hypothesis impossible to isolate. The browser identity binds to the
                # shared fingerprint pool via create_registration_identity, so its
                # fingerprint_key is the canonical per-account fingerprint label.
                "auth_fingerprint_profile": str(
                    identity_context.get("browser_fingerprint_profile")
                    or identity_context.get("fingerprint_key")
                    or ""
                ),
                "response": {"auth_session": auth_body, "access_token_probe": probe},
                "access_token_probe": probe,
                "quota_status": probe.get("quota_status", "") if isinstance(probe, dict) else "",
                "post_registration_ready": success,
                "totp_secret": totp_secret,
                "twofa_enrollment": twofa_result,
                "mailbox": _mailbox_snapshot(mailbox),
                "browser_diagnostics": _browser_diagnostics(page, driver_name),
                "proxy_audit": _safe_proxy_audit(proxy_metadata),
                "driver_capabilities": driver_capabilities(driver_name),
            }
            machine.transition(RegistrationState.COMPLETED)
            result["registration_machine"] = machine.snapshot()
            return result
    except BrowserRegistrationError as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(exc.code)
        if page is not None:
            diagnostics = _browser_diagnostics(page, driver_name)
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": _safe_text(exc),
            "failure_class": _browser_failure_class(exc.code),
            "mailbox": _browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": _safe_proxy_audit(proxy_metadata),
            "driver_capabilities": driver_capabilities(driver_name),
        }
    except Exception as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(type(exc).__name__)
        if page is not None:
            diagnostics = _browser_diagnostics(page, driver_name)
        error = redact_proxy_text(f"{type(exc).__name__}: {exc}", proxy)
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": _safe_text(error),
            "failure_class": _browser_failure_class(str(exc)),
            "mailbox": _browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": _safe_proxy_audit(proxy_metadata),
            "driver_capabilities": driver_capabilities(driver_name),
        }


def run_playwright_registration(**kwargs: Any) -> dict[str, Any]:
    return run_browser_registration(driver_name="playwright", **kwargs)


def build_browser_session_file(result: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a browser result through the same canonical session builder."""
    from ...session_builder import build_session_file

    return build_session_file(dict(result or {}))
