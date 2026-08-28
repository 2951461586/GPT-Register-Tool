"""Public registration facade and compatibility exports."""

import sys
import time

from curl_cffi import requests as curl_requests

from .codex_sentinel import import_cookie_header, load_cached_sentinel, with_sentinel
from .auth_headers import (
    auth_fingerprint_capabilities,
    auth_impersonate,
    chatgpt_headers,
    curl_cffi_capabilities,
    current_auth_fingerprint,
    openai_auth_headers,
    nextauth_headers,
    set_fingerprint_device,
    set_fingerprint_geo,
    select_auth_fingerprint,
)
from .error_classification import classify_error
from .config import (
    CFG,
    current_config_data,
    resolve_runtime_config,
    runtime_config_scope,
    validate_config,
    validate_registration_driver_config,
)
from .http_client import request_with_retry
from .phone_proxy import normalize_proxy_url, refresh_proxy_sid
from .sentinel_tokens import (
    _cookie_jar_header,
    _extract_sentinel,
    _extract_sentinel_http,
    assert_sentinel_device_id,
    _import_sentinel_cookies,
    _sentinel_device_id,
    _sentinel_frame_version,
    _set_oai_did_cookie,
)
from .auth_flow import (
    LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
    _absolute_url,
    _auth_request_headers,
    _complete_existing_login_totp,
    _continue_signup_username,
    _invalid_state_auth_response,
    _is_chatgpt_auth_login_landing,
    _is_email_verification_step,
    _is_existing_login_redirect,
    _is_mfa_challenge_payload,
    _is_signup_password_step,
    _json_or_raw,
    _login_existing_account_with_email_otp,
    _openai_signin_url,
    _passwordless_signin_attempts,
    _prepare_signup_auth_state,
    _prime_email_verification_page,
    _response_next_url,
    _response_next_url_from_data,
    _send_existing_login_otp,
    _signup_signin_attempts,
    _totp_factor_id,
    _with_query_param,
)
from .account_creation import (
    _auth_session_access_token,
    _cookie_header,
    _create_account_continue_url,
    _create_account_sentinel_token,
    _email_otp_send_url,
    _fetch_auth_session,
    _is_user_already_exists,
    _is_wrong_email_otp_code,
    _minimal_chatgpt_cookie_header,
    _validate_email_otp,
)
from .http_utils import _follow_continue_url
from .auth_state import fetch_client_auth_session_dump as _fetch_client_auth_session_dump
from .otp_strategy import (
    SyntheticResponse,
    _poll_registration_email_otp,
    send_registration_email_otp as _send_registration_email_otp,
)
from .mailbox import _ensure_mailbox_account, _poll_email_otp, _snapshot_mailbox_message
from .paths import runtime_file
from .phone_registration import run_phone_register as _run_phone_register_impl
from .registration_outcome import (
    _create_account_error,
    _failure_result,
    _mailbox_snapshot,
    _oauth_result_summary,
    _probe_registration_access_token as _probe_registration_access_token_impl,
    _registration_outcome,
    _registration_requires_phone_verification,
    _registration_requires_refresh_token,
    _retain_registration_checkpoint as _retain_registration_checkpoint_impl,
)
from .registration_preflight import _resolve_proxy_scheme, registration_network_preflight
from .registration_progress import registration_stage, track_registration
from .registration_state import (
    RegistrationStage,
    RegistrationState,
    RegistrationStateMachine,
    _normalize_registration_mode,
    _stored_registration_password,
    prepare_registration_context,
)
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text
from .session_builder import build_session_file
from . import account_liveness
from .utils import (
    _generate_password,
    _print_timings,
    _random_birthdate,
    _random_name,
    _safe_tock,
    _tick,
    _timing_summary,
    _tock,
    _tl,
    think_stage,
)
from .registration_drivers.base import normalize_registration_driver

REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD = "verification code"
REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS = f"{REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD}|{LOGIN_EMAIL_OTP_SUBJECT_KEYWORD}"


def probe_account_liveness(*args, **kwargs):
    """Delegate dynamically so runtime instrumentation and tests can replace the canonical probe."""
    return account_liveness.probe_account_liveness(*args, **kwargs)


@track_registration
def run_email(
    proxy=None,
    password=None,
    sentinel_data=None,
    mailbox=None,
    phone_pool=None,
    codex_oauth=False,
    registration_mode=None,
    browser_headless: bool | None = None,
    enroll_2fa=True,
    runtime_config=None,
    registration_driver=None,
    proxy_metadata=None,
):
    """Run the staged email-registration workflow."""
    config = resolve_runtime_config(runtime_config, workflow="registration")
    selected_driver = normalize_registration_driver(registration_driver, config.data)
    # Validate selected browser credentials before the driver can claim a
    # disposable mailbox or create an external profile/session.
    validate_registration_driver_config(config.data, selected_driver, proxy=proxy)
    if selected_driver != "protocol":
        from .registration_drivers.playwright import run_browser_registration
        return run_browser_registration(
            driver_name=selected_driver,
            proxy=proxy, password=password, mailbox=mailbox, config=config.data,
            browser_headless=browser_headless, enroll_2fa=enroll_2fa,
            proxy_metadata=proxy_metadata,
            # Every browser registration validates the first AT inside the
            # same browser context.  Crossing to the protocol/health proxy at
            # this point creates fingerprint and egress drift on a new account.
            probe_fn=None,
        )
    from .registration_handlers import RegistrationEmailWorkflow

    flow = RegistrationStateMachine(registration_stage)
    return RegistrationEmailWorkflow(
        flow,
        proxy=proxy,
        password=password,
        sentinel_data=sentinel_data,
        mailbox=mailbox,
        phone_pool=phone_pool,
        codex_oauth=codex_oauth,
        registration_mode=registration_mode,
        browser_headless=browser_headless,
        enroll_2fa=enroll_2fa,
        config=config.data,
        operations=sys.modules[__name__],
    ).run()


def run_phone(*args, **kwargs):
    """Compatibility wrapper; SMS/phone registration has been removed from the active flow."""
    return run_email(
        proxy=kwargs.get("proxy"),
        password=kwargs.get("password"),
        sentinel_data=kwargs.get("sentinel_data"),
        mailbox=kwargs.get("mailbox"),
        phone_pool=kwargs.get("phone_pool"),
        codex_oauth=kwargs.get("codex_oauth", False),
        registration_mode=kwargs.get("registration_mode"),
        browser_headless=kwargs.get("browser_headless"),
        runtime_config=kwargs.get("runtime_config"),
    )


def run_phone_register(
    proxy=None,
    password=None,
    sentinel_data=None,
    codex_oauth=False,
    smsbower_country=None,
    smsbower_api_key=None,
    bind_email=None,
):
    """Register a ChatGPT account via phone number (SMS OTP), then optionally bind email."""
    return _run_phone_register_impl(
        proxy=proxy,
        password=password,
        sentinel_data=sentinel_data,
        codex_oauth=codex_oauth,
        smsbower_country=smsbower_country,
        smsbower_api_key=smsbower_api_key,
        bind_email=bind_email,
    )


def _probe_registration_access_token(access_token, auth_session, proxy=None, cfg=None):
    return _probe_registration_access_token_impl(
        access_token,
        auth_session,
        proxy=proxy,
        cfg=cfg or current_config_data(),
        probe_fn=probe_account_liveness,
        stage_fn=registration_stage,
        sleep_fn=time.sleep,
    )


def _retain_registration_checkpoint(success, access_token, at_probe):
    return _retain_registration_checkpoint_impl(success, access_token, at_probe)


def run_batch(
    count=1,
    proxy=None,
    proxy_pool=None,
    mailboxes=None,
    workers=4,
    phone_pool=None,
    codex_oauth=False,
    registration_mode=None,
    max_attempts=2,
    retry_delay_seconds=1.0,
    browser_headless=None,
    enroll_2fa=True,
    on_result=None,
    registration_driver=None,
):
    """Compatibility entry point for callers importing ``registration.run_batch``."""
    from .batch_runner import run_batch_impl

    return run_batch_impl(
        count=count,
        proxy=proxy,
        proxy_pool=proxy_pool,
        mailboxes=mailboxes,
        workers=workers,
        phone_pool=phone_pool,
        codex_oauth=codex_oauth,
        registration_mode=registration_mode,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
        on_result=on_result,
        browser_headless=browser_headless,
        enroll_2fa=enroll_2fa,
        registration_driver=registration_driver,
        run_email_func=run_email,
    )

# 保持向后兼容（cli.py 等通过 `_build_session_file` 引用）
_build_session_file = build_session_file
