"""Protocol-lane auth flow, split from the 1664-line ``auth_flow.py``.

Every submodule is reached module-qualified (``from . import steps`` then
``steps._is_about_you_step(...)``) so ``patch`` on the defining submodule is
seen by all callers; this package re-exports the same names for external
consumers (``registration.py`` and friends) that import from ``.auth_flow``."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Every name below lives in a submodule. Importing them eagerly made
# ``import sms_tool.auth_flow`` pull the whole package in, which is what closed the
# package facade cycle (this ``__init__`` -> submodules, submodules -> this
# package). PEP 562 resolves each name on first access instead, so the cycle
# exists only in the type checker's head -- the TYPE_CHECKING block below is how
# pyright still sees the symbols, and it never executes.
if TYPE_CHECKING:
    from .deps import (  # noqa: F401
        _absolute_url,
        _cookie_presence,
        _fetch_client_auth_session_dump,
        _follow_continue_url,
        _json_or_raw,
        _retry_after_seconds,
        _validate_email_otp,
        auth_impersonate,
        current_config_data,
        mark_registration_rate_limited,
        nextauth_headers,
        openai_auth_headers,
        poll_email_otp,
        redact_proxy_url,
        request_with_retry,
    )
    from .login import (  # noqa: F401
        _existing_login_continue,
        _existing_login_probe,
        _existing_login_signin,
        _fetch_session_csrf_token,
        _login_existing_account_with_email_otp,
    )
    from .otp import (  # noqa: F401
        _existing_login_otp,
        _otp_challenge_established,
        _send_existing_login_otp,
    )
    from .password_step import (  # noqa: F401
        LOGIN_PASSWORD_STEP_TYPES,
        SOURCE_SIBLING_FORM,
        SOURCE_TRANSACTION_STEP,
        _FORM_TAG_RE,
        _PASSWORD_INPUT_RE,
        _has_password_form,
        _is_login_password_step,
        _login_password_page_type,
        _password_login_existing_account,
        _probe_login_password_step,
    )
    from .sentinel_flow import (  # noqa: F401
        _authorize_continue_sentinel,
    )
    from .signup import (  # noqa: F401
        _continue_signup_username,
        _prepare_signup_auth_state,
        _prime_create_account_password_page,
        _prime_email_verification_page,
    )
    from .steps import (  # noqa: F401
        LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        _CC_CAPS,
        _PASSKEY_CLIENT_CAPABILITIES,
        _auth_request_headers,
        _ensure_authorize_context,
        _existing_login_continue_enabled,
        _invalid_state_auth_response,
        _is_about_you_step,
        _is_chatgpt_auth_login_landing,
        _is_email_verification_step,
        _is_existing_login_redirect,
        _is_signup_password_page,
        _is_signup_password_step,
        _openai_signin_url,
        _passwordless_signin_attempts,
        _prime_create_account_password_page_enabled,
        _print_protocol_diagnostic,
        _protocol_diagnostic,
        _response_next_url,
        _signup_continue_screen_hint_enabled,
        _signup_signin_attempts,
        _with_query_param,
    )
    from .totp import (  # noqa: F401
        _complete_existing_login_totp,
        _is_mfa_challenge_payload,
        _response_next_url_from_data,
        _totp_factor_id,
    )

_LAZY: dict[str, str] = {
    "LOGIN_EMAIL_OTP_SUBJECT_KEYWORD": "steps",
    "LOGIN_PASSWORD_STEP_TYPES": "password_step",
    "SOURCE_SIBLING_FORM": "password_step",
    "SOURCE_TRANSACTION_STEP": "password_step",
    "_CC_CAPS": "steps",
    "_FORM_TAG_RE": "password_step",
    "_PASSKEY_CLIENT_CAPABILITIES": "steps",
    "_PASSWORD_INPUT_RE": "password_step",
    "_absolute_url": "deps",
    "_auth_request_headers": "steps",
    "_authorize_continue_sentinel": "sentinel_flow",
    "_complete_existing_login_totp": "totp",
    "_continue_signup_username": "signup",
    "_cookie_presence": "deps",
    "_ensure_authorize_context": "steps",
    "_existing_login_continue": "login",
    "_existing_login_continue_enabled": "steps",
    "_existing_login_otp": "otp",
    "_existing_login_probe": "login",
    "_existing_login_signin": "login",
    "_fetch_client_auth_session_dump": "deps",
    "_fetch_session_csrf_token": "login",
    "_follow_continue_url": "deps",
    "_has_password_form": "password_step",
    "_invalid_state_auth_response": "steps",
    "_is_about_you_step": "steps",
    "_is_chatgpt_auth_login_landing": "steps",
    "_is_email_verification_step": "steps",
    "_is_existing_login_redirect": "steps",
    "_is_login_password_step": "password_step",
    "_is_mfa_challenge_payload": "totp",
    "_is_signup_password_page": "steps",
    "_is_signup_password_step": "steps",
    "_json_or_raw": "deps",
    "_login_existing_account_with_email_otp": "login",
    "_login_password_page_type": "password_step",
    "_openai_signin_url": "steps",
    "_otp_challenge_established": "otp",
    "_password_login_existing_account": "password_step",
    "_passwordless_signin_attempts": "steps",
    "_prepare_signup_auth_state": "signup",
    "_prime_create_account_password_page": "signup",
    "_prime_create_account_password_page_enabled": "steps",
    "_prime_email_verification_page": "signup",
    "_print_protocol_diagnostic": "steps",
    "_probe_login_password_step": "password_step",
    "_protocol_diagnostic": "steps",
    "_response_next_url": "steps",
    "_response_next_url_from_data": "totp",
    "_retry_after_seconds": "deps",
    "_send_existing_login_otp": "otp",
    "_signup_continue_screen_hint_enabled": "steps",
    "_signup_signin_attempts": "steps",
    "_totp_factor_id": "totp",
    "_validate_email_otp": "deps",
    "_with_query_param": "steps",
    "auth_impersonate": "deps",
    "current_config_data": "deps",
    "mark_registration_rate_limited": "deps",
    "nextauth_headers": "deps",
    "openai_auth_headers": "deps",
    "poll_email_otp": "deps",
    "redact_proxy_url": "deps",
    "request_with_retry": "deps",
}

__all__ = [
    "LOGIN_EMAIL_OTP_SUBJECT_KEYWORD",
    "LOGIN_PASSWORD_STEP_TYPES",
    "SOURCE_SIBLING_FORM",
    "SOURCE_TRANSACTION_STEP",
    "_CC_CAPS",
    "_FORM_TAG_RE",
    "_PASSKEY_CLIENT_CAPABILITIES",
    "_PASSWORD_INPUT_RE",
    "_absolute_url",
    "_cookie_presence",
    "_fetch_client_auth_session_dump",
    "_follow_continue_url",
    "poll_email_otp",
    "_retry_after_seconds",
    "_validate_email_otp",
    "auth_impersonate",
    "current_config_data",
    "mark_registration_rate_limited",
    "nextauth_headers",
    "openai_auth_headers",
    "redact_proxy_url",
    "request_with_retry",
    "_auth_request_headers",
    "_authorize_continue_sentinel",
    "_complete_existing_login_totp",
    "_continue_signup_username",
    "_ensure_authorize_context",
    "_existing_login_continue",
    "_existing_login_continue_enabled",
    "_prime_create_account_password_page_enabled",
    "_signup_continue_screen_hint_enabled",
    "_existing_login_otp",
    "_existing_login_probe",
    "_existing_login_signin",
    "_fetch_session_csrf_token",
    "_has_password_form",
    "_invalid_state_auth_response",
    "_is_about_you_step",
    "_is_chatgpt_auth_login_landing",
    "_is_email_verification_step",
    "_is_existing_login_redirect",
    "_is_login_password_step",
    "_is_mfa_challenge_payload",
    "_is_signup_password_page",
    "_is_signup_password_step",
    "_json_or_raw",
    "_login_existing_account_with_email_otp",
    "_login_password_page_type",
    "_openai_signin_url",
    "_otp_challenge_established",
    "_password_login_existing_account",
    "_passwordless_signin_attempts",
    "_prepare_signup_auth_state",
    "_prime_email_verification_page",
    "_prime_create_account_password_page",
    "_print_protocol_diagnostic",
    "_probe_login_password_step",
    "_protocol_diagnostic",
    "_response_next_url",
    "_response_next_url_from_data",
    "_send_existing_login_otp",
    "_signup_signin_attempts",
    "_totp_factor_id",
    "_with_query_param",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # `module_name` comes from the `_LAZY` whitelist above, never from caller input.
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
