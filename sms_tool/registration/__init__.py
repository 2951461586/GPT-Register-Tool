"""Public registration facade, split out of the package root on 2026-10-21.

The implementation lives in :mod:`sms_tool.registration.orchestrator.orchestrator` (the
former ``sms_tool/registration/orchestrator.py``); this package re-exports its whole
surface so ``from sms_tool.registration.orchestrator import X`` and ``sms_tool.registration.orchestrator.X``
keep working.

**The seam is not here.** ``_email_registration_operations()`` builds its
dependency mapping from *orchestrator module globals at call time*, which is
what makes ``patch.object(registration, "<name>")`` effective. Once that
function moved into the submodule, patching this package stopped reaching it,
so every test patch targets ``sms_tool.registration.orchestrator.orchestrator`` instead.
Adding a new seam name means patching the submodule, not this facade.
"""

from __future__ import annotations

from .orchestrator import *  # noqa: F401,F403
from .orchestrator import __all__ as __all__  # noqa: F401
from .orchestrator import (  # noqa: F401
    _absolute_url,
    _auth_request_headers,
    _auth_session_access_token,
    _build_session_file,
    _complete_existing_login_totp,
    _continue_signup_username,
    _cookie_header,
    _cookie_jar_header,
    _create_account_continue_url,
    _create_account_error,
    _create_account_sentinel_token,
    _email_otp_send_url,
    _email_registration_operations,
    _extract_sentinel,
    _extract_sentinel_http,
    _failure_result,
    _fetch_auth_session,
    _fetch_client_auth_session_dump,
    _follow_continue_url,
    _generate_password,
    _import_sentinel_cookies,
    _invalid_state_auth_response,
    _is_chatgpt_auth_login_landing,
    _is_email_verification_step,
    _is_existing_login_redirect,
    _is_mfa_challenge_payload,
    _is_signup_password_step,
    _is_user_already_exists,
    _is_wrong_email_otp_code,
    _json_or_raw,
    _login_existing_account_with_email_otp,
    _mailbox_snapshot,
    _minimal_chatgpt_cookie_header,
    _normalize_registration_mode,
    _oauth_result_summary,
    _openai_signin_url,
    _passwordless_signin_attempts,
    _poll_registration_email_otp,
    _prepare_signup_auth_state,
    _prime_create_account_password_page,
    _prime_create_account_password_page_enabled,
    _prime_email_verification_page,
    _print_timings,
    _probe_registration_access_token,
    _probe_registration_access_token_impl,
    _random_birthdate,
    _random_name,
    _registration_outcome,
    _registration_requires_phone_verification,
    _registration_requires_refresh_token,
    _resolve_proxy_scheme,
    _response_next_url,
    _response_next_url_from_data,
    _retain_registration_checkpoint,
    _retain_registration_checkpoint_impl,
    _run_phone_register_impl,
    _safe_tock,
    _sanitize,
    _sanitize_text,
    _send_existing_login_otp,
    _send_registration_email_otp,
    _sentinel_device_id,
    _sentinel_frame_version,
    _set_oai_did_cookie,
    _signup_signin_attempts,
    _stored_registration_password,
    _stored_registration_totp,
    _tick,
    _timing_summary,
    _tl,
    _tock,
    _totp_factor_id,
    _validate_email_otp,
    _with_query_param,
)
