"""入口编排：run_browser_registration 把上面各层串成完整注册流程。依赖全部下层。"""

from __future__ import annotations

import logging
import time
import uuid

logger = logging.getLogger(__name__)

# Cross-module calls go through the defining module's namespace (no
# from-import copies). A from-import binds the function object into THIS
# module's globals, so patching the source module would silently miss these
# call sites -- the exact "patch failure surface" the 2026-09-05 split had to
# patch around with tests/browser_flow_patch.py. Module attribute access makes
# the source module the only place a patch can land.
from . import decisions, dom_fields, flow_steps, form_steps, page_state, recovery, session
from .context import prepare_browser_context

from ... import (
    browser_fingerprint_pool,
    chatgpt_bootstrap,
    mailbox as mailbox_pkg,
    paypal_proxy,
    registration_outcome,
    registration_progress,
    registration_result,
    storage,
)
from ...accounts import account_identity
from ...mailbox_service import MailboxService
from ...phone_proxy import redact_proxy_text
from ...registration_state import RegistrationState, RegistrationStateMachine
from ...utils import _generate_password, _random_birthdate, _random_name
from .. import external_sessions
from ..base import BrowserRegistrationError, driver_capabilities, normalize_registration_driver
from collections.abc import Mapping
from typing import Any


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
            if attempt >= attempts or not recovery.is_navigation_retryable(exc):
                raise
            registration_progress.registration_stage(
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
    session_factory=None,
    proxy_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one browser registration; result matches the protocol result contract."""
    if session_factory is None:
        session_factory = external_sessions.create_browser_session
    try:
        driver_name = normalize_registration_driver(driver_name)
    except ValueError as exc:
        return {
            "success": False,
            "registration_driver": str(driver_name or ""),
            "error": dom_fields._safe_text(str(exc)),
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
        mailbox = mailbox_pkg._ensure_mailbox_account(mailbox)
    except Exception as exc:
        return {
            "success": False,
            "registration_driver": driver_name,
            "error": "browser_mailbox_setup_failed",
            "failure_class": "mailbox",
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
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
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
        }
    started = int(time.time())
    machine = RegistrationStateMachine(registration_progress.registration_stage)
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
    attempt_number = decisions.attempt_number(proxy_metadata)
    profile_key = decisions.browser_profile_key(account_key, attempt_number)
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
    _browser_geo_enabled = bool(dom_fields._config_value(config, "browser_geo_alignment", True))
    _browser_geo = browser_fingerprint_pool.detect_proxy_exit_geo(proxy, enabled=_browser_geo_enabled)
    _browser_profile = browser_fingerprint_pool.select_browser_profile(_browser_geo, seed=device_id, config=config)
    locale, timezone_id = decisions.aligned_locale_timezone(_browser_profile, locale, timezone_id)
    # P1-3: playwright consumes this as its viewport; camoufox consumes it as
    # Screen(max_width, max_height) (previously hardcoded 1280x900, so the pool
    # never reached it). Provider-owned drivers get None -- see decisions.py.
    _browser_viewport = decisions.browser_screen_size(_browser_profile, driver_name)
    try:
        # Admission must happen before a browser process/context is allocated.
        # Acquiring at the later AUTH_FLOW transition let queued workers hold
        # idle Camoufox instances while the auth cap was saturated.
        registration_progress.admit_registration_stage("auth_flow")
        with flow_steps._browser_session_scope(
            driver_name=driver_name, config=config, proxy=proxy, headless=headless,
            timeout_ms=max(5_000, timeout * 1_000), locale=locale, timezone_id=timezone_id,
            browser_identity=browser_identity, viewport=_browser_viewport,
            session_factory=session_factory,
        ) as browser:
            # P2-3: every browser driver probes its egress now. Provider
            # drivers keep failing on a mismatch; the local ones only record
            # and warn -- a wrong country is evidence, not a gate.
            _country_check = decisions.proxy_country_check_mode(driver_name, config)
            if _country_check != "off":
                verification = external_sessions.verify_browser_proxy_country(browser, expected_country=str((proxy_metadata or {}).get("expected_country") or ""), timeout_seconds=min(20, timeout))
                if proxy_metadata is not None:
                    proxy_metadata = dict(proxy_metadata)
                    proxy_metadata["actual_country"] = verification.get("actual_country", "")
                if not verification.get("ok"):
                    _country_error = str(verification.get("error") or "unknown")
                    if _country_check == "blocking":
                        raise BrowserRegistrationError(f"{driver_name}_proxy_country_mismatch", _country_error)
                    logger.warning(
                        "egress country probe (%s, non-fatal): %s (expected=%s actual=%s)",
                        driver_name, _country_error,
                        str((proxy_metadata or {}).get("expected_country") or ""),
                        str(verification.get("actual_country") or ""),
                    )
            page = browser.page
            browser.add_device_cookie(device_id, chat_base, auth_base)
            machine.transition(RegistrationState.AUTH_FLOW)
            _goto_with_retry(page, start_url, timeout_ms=timeout * 1_000)
            form_steps._maybe_accept_cookies(page)
            # P1 risk-control: replay a real browser's ChatGPT first-screen
            # request sequence (mirrors turb-gpt-free-register's
            # core/chatgpt_bootstrap.py).  Config-gated (default off) and
            # non-fatal by construction, so it can never gate registration.
            _anon_bootstrap = chatgpt_bootstrap.run_anonymous_bootstrap(page, config)
            try:
                page_state._ensure_signup_page_ready(
                    page,
                    timeout_seconds=recovery.stage_timeout(config, "auth_flow", min(45, timeout), maximum=120),
                    config=config,
                )
            except BrowserRegistrationError as exc:
                # A committed SPA document can miss the email field while its
                # auth shell is still mounting. Reuse the same proxy once, but
                # reload the document before allowing the batch retry policy to
                # consume another mailbox attempt.
                if exc.code != "browser_email_field_missing":
                    raise
                registration_progress.registration_stage("auth_flow_retry", detail="signup_ready_reload")
                try:
                    page.reload(wait_until="commit", timeout=max(5_000, timeout * 1_000))
                except Exception:
                    pass
                page_state._ensure_signup_page_ready(
                    page,
                    timeout_seconds=recovery.stage_timeout(config, "auth_flow", min(30, timeout), maximum=120),
                    config=config,
                )
            mailbox_pkg._snapshot_mailbox_message(mailbox, proxy=proxy)
            started = int(time.time())
            form_steps._fill_email(page, email, config=config)
            machine.transition(RegistrationState.USER_REGISTER)
            state = page_state._wait_for_registration_state(
                page,
                recovery.stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                browser=browser,
                config=config,
            )
            if state == "challenge":
                if not page_state._wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
                state = page_state._wait_for_registration_state(
                    page,
                    recovery.stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
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
                password_used = form_steps._fill_password_if_present(page, password, config=config)
            else:
                password_used = False
            machine.transition(RegistrationState.EMAIL_OTP_SEND)
            if password_used:
                state = page_state._wait_for_registration_state(
                    page,
                    recovery.stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
                    browser=browser,
                    config=config,
                )
                if state == "challenge":
                    if not page_state._wait_for_challenge_clear(page, max_wait_seconds=30):
                        raise BrowserRegistrationError("manual_challenge_required")
                    state = page_state._wait_for_registration_state(
                        page,
                        recovery.stage_timeout(config, "user_register", min(timeout, 30), maximum=120),
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
            fields = dom_fields._otp_fields(page)
            if fields is not None:
                machine.transition(RegistrationState.EMAIL_OTP_WAIT)
                excluded_otps: set[str] = set()
                for otp_attempt in range(3):
                    otp = flow_steps._poll_browser_otp(
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
                                if not dom_fields._page_is_alive(page):
                                    try:
                                        page, _ = flow_steps._restart_email_otp_flow(
                                            browser, page, start_url=start_url, email=email,
                                            password=password, timeout_seconds=timeout,
                                            config=config,
                                        )
                                        restarted = True
                                    except Exception:
                                        restarted = False
                                if restarted or dom_fields._click_resend(page):
                                    started = int(time.time())
                                    continue
                            raise BrowserRegistrationError("browser_email_otp_timeout")
                    page = getattr(browser, "page", None) or page
                    excluded_otps.add(str(otp))
                    machine.transition(RegistrationState.EMAIL_OTP_VALIDATE)
                    form_steps._fill_otp(page, str(otp))
                    outcome = page_state._wait_after_otp_submit(
                        page,
                        timeout_seconds=recovery.stage_timeout(
                            config, "email_otp_validate", min(30, timeout), maximum=120
                        ),
                        require_transition=True,
                    )
                    # Match the reference Roxy state machine: absence of an explicit
                    # validation error is accepted even when the old OTP DOM remains
                    # mounted during a slow SPA navigation.
                    if outcome in {"accepted", "pending"}:
                        break
                    if outcome == "invalid" and otp_attempt < 2:
                        restarted = False
                        if not dom_fields._page_is_alive(page):
                            try:
                                page, _ = flow_steps._restart_email_otp_flow(
                                    browser, page, start_url=start_url, email=email,
                                    password=password, timeout_seconds=timeout,
                                    config=config,
                                )
                                restarted = True
                            except Exception:
                                restarted = False
                        if restarted or dom_fields._click_resend(page):
                            started = int(time.time())
                else:
                    raise BrowserRegistrationError("browser_email_otp_rejected")
            state = page_state._post_otp_registration_state(
                page,
                browser=browser,
                timeout_seconds=recovery.stage_timeout(
                    config, "create_account", min(30, max(5, timeout)), maximum=180
                ),
                config=config,
            )
            # Slow signups can sit on the post-OTP email-verification route for
            # minutes before routing on (observed ~200s on healthy runs). Run
            # two reload/reprobe rounds instead of one before declaring the
            # run stuck.
            for retry_round in range(2):
                if state not in {"unknown", "email_verification_stuck"}:
                    break
                registration_progress.registration_stage(
                    "create_account_retry",
                    detail=(
                        "email_verification_reload"
                        if state == "email_verification_stuck"
                        else "state_probe_retry"
                    )
                    + (f"_round{retry_round + 1}" if retry_round else ""),
                )
                if state == "email_verification_stuck":
                    try:
                        page.reload(wait_until="commit", timeout=max(5_000, timeout * 1_000))
                    except Exception:
                        pass
                page = getattr(browser, "page", None) or page
                state = page_state._post_otp_registration_state(
                    page,
                    browser=browser,
                    timeout_seconds=recovery.stage_timeout(
                        config, "create_account", min(45, max(10, timeout)), maximum=180
                    ),
                    config=config,
                )
            page = getattr(browser, "page", None) or page
            machine.transition(RegistrationState.CREATE_ACCOUNT)
            profile_required = page_state._profile_completion_required(state)
            if profile_required:
                form_steps._complete_profile(page, full_name, birthdate)
                profile_done = page_state._wait_for_profile_completion(
                    page,
                    timeout_seconds=recovery.stage_timeout(
                        config, "create_account", min(30, max(5, timeout)), maximum=120
                    ),
                    config=config,
                )
                if not profile_done:
                    registration_progress.registration_stage("create_account_retry", detail="profile_completion_retry")
                    profile_done = page_state._wait_for_profile_completion(
                        page,
                        timeout_seconds=recovery.stage_timeout(
                            config, "create_account", min(20, max(5, timeout)), maximum=120
                        ),
                        config=config,
                    )
                if not profile_done:
                    raise BrowserRegistrationError("browser_profile_submit_timeout")
                page.wait_for_timeout(2_000)
            if page_state._manual_challenge(page):
                if not page_state._wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
            if hasattr(browser, "ensure_chatgpt_context"):
                page = dom_fields._prepare_session_page(browser, page, timeout)
            elif decisions.needs_chat_base_navigation(chat_base, str(page.url or "")):
                _goto_with_retry(page, chat_base, timeout_ms=timeout * 1_000)
            form_steps._maybe_dismiss_chatgpt_onboarding(page, config=config)
            machine.transition(RegistrationState.AUTH_SESSION)
            session_info = session._session_payload(
                browser,
                chat_base,
                email,
                timeout_seconds=recovery.stage_timeout(
                    config, "auth_session", timeout, minimum=15, maximum=120
                ),
            )
            auth_body = session_info["body"]
            access_token = session_info["access_token"]
            machine.transition(RegistrationState.ACCESS_TOKEN_PROBE)
            effective_probe_fn = probe_fn
            if effective_probe_fn is None:
                # Route the post-registration AT probe through the browser
                # context for ALL browser drivers, not just cloud ones.  This
                # keeps the probe on the same fingerprint and cookies used
                # during registration, preventing the identity drift that
                # causes immediate token revocation when curl_cffi switches
                # to a generic fingerprint.
                effective_probe_fn = lambda account, **kwargs: session._browser_access_token_probe(
                    browser,
                    account,
                    timeout=int(kwargs.get("timeout") or timeout),
                )
            probe = registration_outcome._probe_registration_access_token(
                access_token, auth_body,
                # The browser context owns the egress and fingerprint for the
                # probe.  Passing proxy=None would break local drivers that
                # need the proxy for the curl fallback, so only suppress it
                # when the browser probe is active.
                proxy=(None if effective_probe_fn is not None else proxy),
                cfg=config, probe_fn=effective_probe_fn,
                stage_fn=registration_progress.registration_stage, sleep_fn=time.sleep,
            )
            success, error, warning = registration_outcome._registration_outcome(True, {}, access_token, probe)
            # A token plus a transport-unknown probe means the account was
            # created but the final check could not reach the endpoint. Keep a
            # resumable persistence state instead of discarding the account.
            at_probe_pending = recovery.probe_pending(access_token, probe, success)
            if success:
                session._post_registration_dwell(config)
            machine.transition(RegistrationState.TOTP_ENROLL)
            # Attempt browser-based TOTP 2FA enrollment when requested and
            # registration succeeded.  Routes MFA API calls through the
            # browser's fetch to carry real cookies and Cloudflare clearance.
            twofa_result: dict[str, Any] = {"ok": False, "reason": "disabled"}
            totp_secret = ""
            if success and enroll_2fa:
                try:
                    twofa_result = session._bind_totp_in_browser(
                        page, access_token, device_id, chat_base=chat_base,
                    )
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
                _auth_bootstrap = chatgpt_bootstrap.run_authenticated_bootstrap(
                    page, access_token, device_id=device_id, config=config
                )
            machine.transition(RegistrationState.FINALIZE)
            identity_context = account_identity.create_registration_identity(
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
            _geo_cc = decisions.geo_affinity_country(_browser_geo, _browser_geo_enabled)
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
            # The browser identity binds to the shared fingerprint pool via
            # create_registration_identity, so its fingerprint_key is the
            # canonical per-account fingerprint label (the protocol path stores
            # the curl impersonate label under the same key).
            auth_fingerprint_profile = str(
                identity_context.get("browser_fingerprint_profile")
                or identity_context.get("fingerprint_key")
                or ""
            )
            registration_state, registration_success_basis = decisions.registration_state_and_basis(
                success, at_probe_pending
            )
            result = registration_result.build_registration_result(
                success=success,
                registration_mode="browser",
                registration_state=registration_state,
                email=email,
                error=dom_fields._safe_text(error),
                password=password if password_used else "",
                name=full_name,
                birthdate=birthdate,
                access_token=access_token,
                id_token=session_info.get("id_token", ""),
                auth_session=auth_body,
                cookie_header=browser.cookie_header(),
                device_id=device_id,
                identity_context=identity_context,
                auth_fingerprint_profile=auth_fingerprint_profile,
                response={"auth_session": auth_body, "access_token_probe": probe},
                quota_status=probe.get("quota_status", "") if isinstance(probe, dict) else "",
                totp_secret=totp_secret,
                twofa_enrollment=twofa_result,
                registration_success_basis=registration_success_basis,
                registration_country=paypal_proxy.infer_proxy_country(proxy),
                registration_warning=dom_fields._safe_text(warning),
                post_registration_ready=success,
                mailbox_snapshot=registration_outcome._mailbox_snapshot(mailbox),
                extra={
                    "registration_driver": driver_name,
                    "access_token_probe": probe,
                    "browser_diagnostics": session._browser_diagnostics(page, driver_name),
                    "proxy_audit": session._safe_proxy_audit(proxy_metadata),
                    "driver_capabilities": driver_capabilities(driver_name),
                },
            )
            machine.transition(RegistrationState.COMPLETED)
            result["registration_machine"] = machine.snapshot()
            return result
    except BrowserRegistrationError as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(exc.code)
        if page is not None:
            diagnostics = session._browser_diagnostics(page, driver_name)
        cancelled = exc.code == "registration_cancelled"
        # Expected business failure: log the classified code (not the raw
        # details) so batch audits can count outcomes without leaking page
        # content into logs.
        logger.warning(
            "browser registration failed driver=%s code=%s cancelled=%s",
            driver_name, exc.code, cancelled,
        )
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": dom_fields._safe_text(exc),
            "failure_class": "cancelled" if cancelled else session._browser_failure_class(exc.code),
            "registration_state": "cancelled" if cancelled else "failed",
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": session._safe_proxy_audit(proxy_metadata),
            "driver_capabilities": driver_capabilities(driver_name),
        }
    except Exception as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(type(exc).__name__)
        if page is not None:
            diagnostics = session._browser_diagnostics(page, driver_name)
        error = redact_proxy_text(f"{type(exc).__name__}: {exc}", proxy)
        # This catch-all used to swallow the traceback entirely, leaving only
        # "SomeError: ..." in the result dict. Keep the operator-facing text
        # redacted, but put the full traceback into the log for diagnosis.
        logger.error(
            "browser registration crashed driver=%s: %s",
            driver_name, error, exc_info=exc,
        )
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": dom_fields._safe_text(error),
            "failure_class": session._browser_failure_class(str(exc)),
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": session._safe_proxy_audit(proxy_metadata),
            "driver_capabilities": driver_capabilities(driver_name),
        }


def run_playwright_registration(**kwargs: Any) -> dict[str, Any]:
    return run_browser_registration(driver_name="playwright", **kwargs)


def build_browser_session_file(result: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a browser result through the same canonical session builder."""
    from ...session_builder import build_session_file

    return build_session_file(dict(result or {}))
