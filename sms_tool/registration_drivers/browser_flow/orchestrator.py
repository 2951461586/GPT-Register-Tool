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

from ... import storage
from ...registration import chatgpt_bootstrap
from ...browser import fingerprint_pool as browser_fingerprint_pool
from ...registration import outcome as registration_outcome
from ...registration import progress as registration_progress
from ...registration import result as registration_result
from ...mailbox import router as mailbox_pkg
from ...proxy import entry as proxy_entry
from ...accounts import account_identity
from ...mailbox.service import MailboxService
from ...proxy.entry import redact_proxy_text
from ...registration.state import RegistrationState, RegistrationStateMachine
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
    last_error: BaseException | None = None
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
    # The loop above either returns or raises, so this is unreachable; it exists
    # only to narrow ``last_error`` for the type checker.
    if last_error is None:  # pragma: no cover
        raise RuntimeError("goto retry loop exited without a navigation result")
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
    pre = _validate_browser_registration_inputs(driver_name=driver_name, mailbox=mailbox, config=config)
    if isinstance(pre, dict):
        return pre
    driver_name, mailbox, email, mailbox_service = pre
    password = str(password or "").strip() or _generate_password()
    full_name = " ".join(_random_name())
    birthdate = _random_birthdate()
    context = prepare_browser_context(config, driver_name, browser_headless)
    headless, timeout, otp_timeout = context.headless, context.timeout, context.otp_timeout
    machine = RegistrationStateMachine(registration_progress.registration_stage)
    machine.transition(RegistrationState.MAILBOX_READY)
    from ...storage import get_device_context

    device_context = get_device_context(email, runtime_config=config)
    device_id = str((device_context or {}).get("device_id") or uuid.uuid4())
    machine.transition(RegistrationState.IDENTITY_READY)
    chat_base, auth_base, start_url = context.chat_base, context.auth_base, context.start_url
    page = None
    diagnostics = {"driver": driver_name, "url_host": "", "title": ""}
    account_key = email
    browser_identity: dict[str, Any] = {
        "driver": driver_name,
        "profile_id": decisions.browser_profile_key(account_key, decisions.attempt_number(proxy_metadata)),
    }
    _browser_geo_enabled = bool(dom_fields._config_value(config, "browser_geo_alignment", True))
    _browser_geo, _browser_profile, locale, timezone_id, geo_audit, _browser_viewport = (
        _resolve_browser_geo_and_profile(
            driver_name=driver_name,
            proxy=proxy,
            proxy_metadata=proxy_metadata,
            device_id=device_id,
            locale=context.locale,
            timezone_id=context.timezone_id,
            browser_geo_enabled=_browser_geo_enabled,
            config=config,
        )
    )
    try:
        # Admission must happen before a browser process/context is allocated.
        # Acquiring at the later AUTH_FLOW transition let queued workers hold
        # idle Camoufox instances while the auth cap was saturated.
        registration_progress.admit_registration_stage("auth_flow")
        with flow_steps._browser_session_scope(
            driver_name=driver_name,
            config=config,
            proxy=proxy,
            headless=headless,
            timeout_ms=max(5_000, timeout * 1_000),
            locale=locale,
            timezone_id=timezone_id,
            browser_identity=browser_identity,
            viewport=_browser_viewport,
            session_factory=session_factory,
        ) as browser:
            _country_check = decisions.proxy_country_check_mode(driver_name, config)
            if _country_check != "off":
                proxy_metadata = _verify_browser_egress_country(
                    browser,
                    driver_name=driver_name,
                    country_check=_country_check,
                    timeout=timeout,
                    proxy_metadata=proxy_metadata,
                    geo_audit=geo_audit,
                )
            page = browser.page
            browser.add_device_cookie(device_id, chat_base, auth_base)
            machine.transition(RegistrationState.AUTH_FLOW)
            _goto_with_retry(page, start_url, timeout_ms=timeout * 1_000)
            form_steps._maybe_accept_cookies(page)
            chatgpt_bootstrap.run_anonymous_bootstrap(page, config)
            _ensure_signup_ready_with_reload(page, timeout=timeout, config=config)
            mailbox_pkg.snapshot_mailbox_message(mailbox, proxy=proxy)
            started = int(time.time())
            form_steps._fill_email(page, email, config=config)
            return _execute_post_email_browser_stages(
                page=page,
                browser=browser,
                machine=machine,
                mailbox_service=mailbox_service,
                mailbox=mailbox,
                driver_name=driver_name,
                start_url=start_url,
                email=email,
                password=password,
                full_name=full_name,
                birthdate=birthdate,
                device_id=device_id,
                account_key=account_key,
                browser_identity=browser_identity,
                browser_profile=_browser_profile,
                browser_geo=_browser_geo,
                browser_geo_enabled=_browser_geo_enabled,
                geo_audit=geo_audit,
                chat_base=chat_base,
                timeout=timeout,
                otp_timeout=otp_timeout,
                started=started,
                proxy=proxy,
                proxy_metadata=proxy_metadata,
                config=config,
                enroll_2fa=enroll_2fa,
                probe_fn=probe_fn,
            )
    except BrowserRegistrationError as exc:
        return _handle_browser_registration_error(
            exc,
            machine=machine,
            page=page,
            driver_name=driver_name,
            email=email,
            mailbox=mailbox,
            proxy_metadata=proxy_metadata,
            geo_audit=geo_audit,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        return _handle_unexpected_browser_crash(
            exc,
            machine=machine,
            page=page,
            driver_name=driver_name,
            email=email,
            mailbox=mailbox,
            proxy=proxy,
            proxy_metadata=proxy_metadata,
            geo_audit=geo_audit,
            diagnostics=diagnostics,
        )


def _execute_post_email_browser_stages(
    *,
    page: Any,
    browser: Any,
    machine: RegistrationStateMachine,
    mailbox_service: Any,
    mailbox: Any,
    driver_name: str,
    start_url: str,
    email: str,
    password: str | None,
    full_name: str,
    birthdate: str,
    device_id: str,
    account_key: str,
    browser_identity: dict[str, Any],
    browser_profile: Mapping[str, Any],
    browser_geo: Mapping[str, Any] | None,
    browser_geo_enabled: bool,
    geo_audit: dict[str, Any],
    chat_base: str,
    timeout: int,
    otp_timeout: int,
    started: int,
    proxy: str | None,
    proxy_metadata: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    enroll_2fa: bool,
    probe_fn: Any,
) -> dict[str, Any]:
    password_used = _advance_to_otp_or_post_password_state(
        page,
        browser=browser,
        machine=machine,
        password=password,
        timeout=timeout,
        config=config,
    )
    page = _poll_and_submit_browser_otp(
        page,
        browser=browser,
        machine=machine,
        mailbox_service=mailbox_service,
        mailbox=mailbox,
        driver_name=driver_name,
        start_url=start_url,
        email=email,
        password=password,
        timeout=timeout,
        otp_timeout=otp_timeout,
        started=started,
        proxy=proxy,
        config=config,
    )
    page = _complete_post_otp_profile_and_navigate(
        page,
        browser=browser,
        machine=machine,
        full_name=full_name,
        birthdate=birthdate,
        chat_base=chat_base,
        timeout=timeout,
        config=config,
    )
    return _finalize_browser_registration(
        page=page,
        browser=browser,
        machine=machine,
        driver_name=driver_name,
        email=email,
        password=password,
        password_used=password_used,
        full_name=full_name,
        birthdate=birthdate,
        device_id=device_id,
        account_key=account_key,
        browser_identity=browser_identity,
        browser_profile=browser_profile,
        browser_geo=browser_geo,
        browser_geo_enabled=browser_geo_enabled,
        geo_audit=geo_audit,
        chat_base=chat_base,
        timeout=timeout,
        proxy=proxy,
        proxy_metadata=proxy_metadata,
        config=config,
        enroll_2fa=enroll_2fa,
        probe_fn=probe_fn,
        mailbox=mailbox,
    )


def _validate_browser_registration_inputs(
    *,
    driver_name: str,
    mailbox: Any,
    config: Mapping[str, Any],
) -> tuple[str, Any, str, Any] | dict[str, Any]:
    try:
        driver_name = normalize_registration_driver(driver_name)
    except ValueError as exc:
        return registration_result.build_registration_failure_result(
            error=dom_fields._safe_text(str(exc)),
            failure_class="configuration",
            extra={"registration_driver": str(driver_name or "")},
        )
    if driver_name == "protocol":
        return registration_result.build_registration_failure_result(
            error="unsupported_registration_driver:protocol",
            failure_class="configuration",
            extra={"registration_driver": driver_name},
        )
    try:
        mailbox = mailbox_pkg.ensure_mailbox_account(mailbox)
    except Exception:
        return registration_result.build_registration_failure_result(
            error="browser_mailbox_setup_failed",
            failure_class="mailbox",
            extra={
                "registration_driver": driver_name,
                "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            },
        )
    email = str(getattr(mailbox, "email", "") or "").strip()
    if not email:
        return registration_result.build_registration_failure_result(
            error="mailbox_required",
            extra={"registration_driver": driver_name},
        )
    try:
        mailbox_service = MailboxService.create(config)
    except Exception:
        return registration_result.build_registration_failure_result(
            error="browser_mailbox_service_unavailable",
            failure_class="network",
            email=email,
            extra={
                "registration_driver": driver_name,
                "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            },
        )
    return driver_name, mailbox, email, mailbox_service


def _resolve_browser_geo_and_profile(
    *,
    driver_name: str,
    proxy: str | None,
    proxy_metadata: Mapping[str, Any] | None,
    device_id: str,
    locale: str,
    timezone_id: str,
    browser_geo_enabled: bool,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str, str, dict[str, str], Any]:
    browser_geo = browser_fingerprint_pool.detect_proxy_exit_geo(proxy, enabled=browser_geo_enabled)
    browser_profile = browser_fingerprint_pool.select_browser_profile(browser_geo, seed=device_id, config=config)
    locale, timezone_id = decisions.aligned_locale_timezone(browser_profile, locale, timezone_id)
    locale_parts = str(locale or "").replace("_", "-").split("-")
    locale_country = locale_parts[-1] if len(locale_parts) > 1 else ""
    geo_audit = {
        "fingerprint_country": (locale_country if driver_name in decisions.SCREEN_MANAGED_DRIVERS else ""),
        "exit_country": str((browser_geo or {}).get("country") or (proxy_metadata or {}).get("actual_country") or ""),
        "source": ("geo_probe" if (browser_geo or {}).get("country") else "preflight"),
    }
    viewport = decisions.browser_screen_size(browser_profile, driver_name)
    return browser_geo, browser_profile, locale, timezone_id, geo_audit, viewport


def _verify_browser_egress_country(
    browser: Any,
    *,
    driver_name: str,
    country_check: str,
    timeout: int,
    proxy_metadata: Mapping[str, Any] | None,
    geo_audit: dict[str, Any],
) -> Mapping[str, Any] | None:
    verification = external_sessions.verify_browser_proxy_country(
        browser,
        expected_country=str((proxy_metadata or {}).get("expected_country") or ""),
        timeout_seconds=min(20, timeout),
    )
    if proxy_metadata is not None:
        updated = dict(proxy_metadata)
        updated["actual_country"] = verification.get("actual_country", "")
        proxy_metadata = updated
    geo_audit["exit_country"] = str(verification.get("actual_country") or "")
    geo_audit["source"] = "browser_probe"
    if not verification.get("ok"):
        country_error = str(verification.get("error") or "unknown")
        if country_check == "blocking":
            raise BrowserRegistrationError(f"{driver_name}_proxy_country_mismatch", country_error)
        logger.warning(
            "egress country probe (%s, non-fatal): %s (expected=%s actual=%s)",
            driver_name,
            country_error,
            str((proxy_metadata or {}).get("expected_country") or ""),
            str(verification.get("actual_country") or ""),
        )
    return proxy_metadata


def _ensure_signup_ready_with_reload(page: Any, *, timeout: int, config: Mapping[str, Any]) -> None:
    try:
        page_state._ensure_signup_page_ready(
            page,
            timeout_seconds=recovery.stage_timeout(config, "auth_flow", min(45, timeout), maximum=120),
            config=config,
        )
    except BrowserRegistrationError as exc:
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


def _wait_and_validate_user_register_state(
    page: Any,
    *,
    browser: Any,
    timeout: int,
    config: Mapping[str, Any],
) -> str:
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
    return state


def _advance_to_otp_or_post_password_state(
    page: Any,
    *,
    browser: Any,
    machine: RegistrationStateMachine,
    password: str | None,
    timeout: int,
    config: Mapping[str, Any],
) -> bool:
    machine.transition(RegistrationState.USER_REGISTER)
    state = _wait_and_validate_user_register_state(page, browser=browser, timeout=timeout, config=config)
    if state == "password":
        password_used = form_steps._fill_password_if_present(page, password or "", config=config)
    else:
        password_used = False
    machine.transition(RegistrationState.EMAIL_OTP_SEND)
    if password_used:
        _wait_and_validate_user_register_state(page, browser=browser, timeout=timeout, config=config)
    return password_used


def _try_restart_or_resend_otp(
    browser: Any,
    page: Any,
    *,
    start_url: str,
    email: str,
    password: str | None,
    timeout: int,
    config: Mapping[str, Any],
) -> tuple[Any, bool]:
    restarted = False
    if not dom_fields._page_is_alive(page):
        try:
            page, _ = flow_steps._restart_email_otp_flow(
                browser,
                page,
                start_url=start_url,
                email=email,
                password=password or "",
                timeout_seconds=timeout,
                config=config,
            )
            restarted = True
        except Exception:
            restarted = False
    return page, (restarted or bool(dom_fields._click_resend(page)))


def _poll_and_submit_browser_otp(
    page: Any,
    *,
    browser: Any,
    machine: RegistrationStateMachine,
    mailbox_service: Any,
    mailbox: Any,
    driver_name: str,
    start_url: str,
    email: str,
    password: str | None,
    timeout: int,
    otp_timeout: int,
    started: int,
    proxy: str | None,
    config: Mapping[str, Any],
) -> Any:
    if dom_fields._otp_fields(page) is None:
        return page
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
        if not otp and otp_attempt > 0:
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
                page, resent = _try_restart_or_resend_otp(
                    browser,
                    page,
                    start_url=start_url,
                    email=email,
                    password=password,
                    timeout=timeout,
                    config=config,
                )
                if resent:
                    started = int(time.time())
                    continue
            raise BrowserRegistrationError("browser_email_otp_timeout")
        page = getattr(browser, "page", None) or page
        excluded_otps.add(str(otp))
        machine.transition(RegistrationState.EMAIL_OTP_VALIDATE)
        form_steps._fill_otp(page, str(otp))
        outcome = page_state._wait_after_otp_submit(
            page,
            timeout_seconds=recovery.stage_timeout(config, "email_otp_validate", min(30, timeout), maximum=120),
            require_transition=True,
        )
        if outcome in {"accepted", "pending"}:
            return page
        if outcome == "invalid" and otp_attempt < 2:
            page, resent = _try_restart_or_resend_otp(
                browser,
                page,
                start_url=start_url,
                email=email,
                password=password,
                timeout=timeout,
                config=config,
            )
            if resent:
                started = int(time.time())
    raise BrowserRegistrationError("browser_email_otp_rejected")


def _complete_post_otp_profile_and_navigate(
    page: Any,
    *,
    browser: Any,
    machine: RegistrationStateMachine,
    full_name: str,
    birthdate: str,
    chat_base: str,
    timeout: int,
    config: Mapping[str, Any],
) -> Any:
    state = page_state._post_otp_registration_state(
        page,
        browser=browser,
        timeout_seconds=recovery.stage_timeout(config, "create_account", min(30, max(5, timeout)), maximum=180),
        config=config,
    )
    for retry_round in range(2):
        if state not in {"unknown", "email_verification_stuck"}:
            break
        registration_progress.registration_stage(
            "create_account_retry",
            detail=("email_verification_reload" if state == "email_verification_stuck" else "state_probe_retry")
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
            timeout_seconds=recovery.stage_timeout(config, "create_account", min(45, max(10, timeout)), maximum=180),
            config=config,
        )
    page = getattr(browser, "page", None) or page
    machine.transition(RegistrationState.CREATE_ACCOUNT)
    if page_state._profile_completion_required(state):
        form_steps._complete_profile(page, full_name, birthdate)
        profile_done = page_state._wait_for_profile_completion(
            page,
            timeout_seconds=recovery.stage_timeout(config, "create_account", min(30, max(5, timeout)), maximum=120),
            config=config,
        )
        if not profile_done:
            registration_progress.registration_stage("create_account_retry", detail="profile_completion_retry")
            profile_done = page_state._wait_for_profile_completion(
                page,
                timeout_seconds=recovery.stage_timeout(config, "create_account", min(20, max(5, timeout)), maximum=120),
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
    return page


def _enroll_browser_totp_and_warmup(
    page: Any,
    *,
    access_token: str,
    device_id: str,
    chat_base: str,
    success: bool,
    enroll_2fa: bool,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    twofa_result: dict[str, Any] = {"ok": False, "reason": "disabled"}
    totp_secret = ""
    if success and enroll_2fa:
        try:
            twofa_result = session._bind_totp_in_browser(page, access_token, device_id, chat_base=chat_base)
            if twofa_result.get("ok"):
                totp_secret = str(twofa_result.get("totp_secret") or "")
            elif not twofa_result.get("error"):
                twofa_result = {"ok": False, "reason": "browser_driver_deferred"}
        except Exception as exc:
            twofa_result = {"ok": False, "reason": f"browser_totp_exception: {type(exc).__name__}"}
    elif enroll_2fa:
        twofa_result = {"ok": False, "reason": "registration_not_successful"}
    if success:
        chatgpt_bootstrap.run_authenticated_bootstrap(page, access_token, device_id=device_id, config=config)
    return twofa_result, totp_secret


def _finalize_browser_registration(
    *,
    page: Any,
    browser: Any,
    machine: RegistrationStateMachine,
    driver_name: str,
    email: str,
    password: str | None,
    password_used: bool,
    full_name: str,
    birthdate: str,
    device_id: str,
    account_key: str,
    browser_identity: dict[str, Any],
    browser_profile: Mapping[str, Any],
    browser_geo: Mapping[str, Any] | None,
    browser_geo_enabled: bool,
    geo_audit: dict[str, Any],
    chat_base: str,
    timeout: int,
    proxy: str | None,
    proxy_metadata: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    enroll_2fa: bool,
    probe_fn: Any,
    mailbox: Any,
) -> dict[str, Any]:
    machine.transition(RegistrationState.AUTH_SESSION)
    session_info = session._session_payload(
        browser,
        chat_base,
        email,
        timeout_seconds=recovery.stage_timeout(config, "auth_session", timeout, minimum=15, maximum=120),
    )
    auth_body = session_info["body"]
    access_token = session_info["access_token"]
    machine.transition(RegistrationState.ACCESS_TOKEN_PROBE)
    effective_probe_fn = probe_fn
    if effective_probe_fn is None:
        effective_probe_fn = lambda account, **kwargs: session._browser_access_token_probe(
            browser,
            account,
            timeout=int(kwargs.get("timeout") or timeout),
        )
    probe = registration_outcome._probe_registration_access_token(
        access_token,
        auth_body,
        proxy=(None if effective_probe_fn is not None else proxy),
        cfg=config,
        probe_fn=effective_probe_fn,
        stage_fn=registration_progress.registration_stage,
        sleep_fn=time.sleep,
    )
    success, error, warning = registration_outcome._registration_outcome(True, {}, access_token, probe)
    at_probe_pending = recovery.probe_pending(access_token, probe, success)
    if success:
        session._post_registration_dwell(config)
    machine.transition(RegistrationState.TOTP_ENROLL)
    twofa_result, totp_secret = _enroll_browser_totp_and_warmup(
        page,
        access_token=access_token,
        device_id=device_id,
        chat_base=chat_base,
        success=success,
        enroll_2fa=enroll_2fa,
        config=config,
    )
    machine.transition(RegistrationState.FINALIZE)
    identity_context = dict(
        account_identity.create_registration_identity(
            proxy,
            pool_index=int((proxy_metadata or {}).get("pool_index", -1) or -1),
            device_id=str(device_id),
            account_key=account_key,
            browser_identity=browser_identity,
            config=config,
        )
    )
    identity_context["browser_fingerprint_profile"] = str(browser_profile.get("browser_fingerprint_profile") or "")
    identity_context["browser_profile_index"] = int(browser_profile.get("browser_profile_index", 0))
    identity_context["fingerprint_seed"] = str(browser_profile.get("fingerprint_seed") or device_id)
    identity_context["geo_country"] = str((browser_geo or {}).get("country") or "")
    identity_context["geo_timezone"] = str((browser_geo or {}).get("timezone") or "")
    identity_context["geo_ip"] = str((browser_geo or {}).get("ip") or "")
    geo_cc = decisions.geo_affinity_country(browser_geo, browser_geo_enabled)
    if geo_cc:
        affinity = dict(identity_context.get("proxy_affinity") or {})
        affinity["country"] = geo_cc
        identity_context["proxy_affinity"] = affinity
    auth_fingerprint_profile = str(
        identity_context.get("browser_fingerprint_profile") or identity_context.get("fingerprint_key") or ""
    )
    registration_state, registration_success_basis = decisions.registration_state_and_basis(success, at_probe_pending)
    result = registration_result.build_registration_result(
        success=success,
        registration_mode="browser",
        registration_state=registration_state,
        email=email,
        error=dom_fields._safe_text(error),
        password=(password or "") if password_used else "",
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
        registration_country=proxy_entry.infer_proxy_country(proxy or ""),
        registration_warning=dom_fields._safe_text(warning),
        post_registration_ready=success,
        mailbox_snapshot=registration_outcome._mailbox_snapshot(mailbox),
        fingerprint_geo_audit=geo_audit,
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


def _handle_browser_registration_error(
    exc: BrowserRegistrationError,
    *,
    machine: RegistrationStateMachine,
    page: Any,
    driver_name: str,
    email: str,
    mailbox: Any,
    proxy_metadata: Mapping[str, Any] | None,
    geo_audit: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    if machine.state is not RegistrationState.FAILED:
        machine.fail(exc.code)
    if page is not None:
        diagnostics = session._browser_diagnostics(page, driver_name)
    cancelled = exc.code == "registration_cancelled"
    logger.warning(
        "browser registration failed driver=%s code=%s cancelled=%s",
        driver_name,
        exc.code,
        cancelled,
    )
    return registration_result.build_registration_failure_result(
        error=dom_fields._safe_text(exc),
        failure_class="cancelled" if cancelled else session._browser_failure_class(exc.code),
        registration_state="cancelled" if cancelled else "failed",
        email=email,
        extra={
            "registration_driver": driver_name,
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": session._safe_proxy_audit(proxy_metadata),
            "fingerprint_geo_audit": geo_audit,
            "driver_capabilities": driver_capabilities(driver_name),
        },
    )


def _handle_unexpected_browser_crash(
    exc: Exception,
    *,
    machine: RegistrationStateMachine,
    page: Any,
    driver_name: str,
    email: str,
    mailbox: Any,
    proxy: str | None,
    proxy_metadata: Mapping[str, Any] | None,
    geo_audit: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    if machine.state is not RegistrationState.FAILED:
        machine.fail(type(exc).__name__)
    if page is not None:
        diagnostics = session._browser_diagnostics(page, driver_name)
    error = redact_proxy_text(f"{type(exc).__name__}: {exc}", proxy)
    logger.error(
        "browser registration crashed driver=%s: %s",
        driver_name,
        error,
        exc_info=exc,
    )
    return registration_result.build_registration_failure_result(
        error=dom_fields._safe_text(error),
        failure_class=session._browser_failure_class(str(exc)),
        email=email,
        extra={
            "registration_driver": driver_name,
            "mailbox": registration_outcome._browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": session._safe_proxy_audit(proxy_metadata),
            "fingerprint_geo_audit": geo_audit,
            "driver_capabilities": driver_capabilities(driver_name),
        },
    )


def run_playwright_registration(**kwargs: Any) -> dict[str, Any]:
    return run_browser_registration(driver_name="playwright", **kwargs)


def build_browser_session_file(result: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a browser result through the same canonical session builder."""
    from ...session.builder import build_session_file

    return build_session_file(dict(result or {}))
