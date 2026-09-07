"""Explicit, immutable dependency interface for the email workflow.

The facade binds this once per invocation, inside the caller's patch/config
scope. A workflow never receives a live module or arbitrary module attributes.

Attribute access stays flat (``r._tick``, not ``r.timing._tick``). The workflow
in ``registration_handlers.py`` reads every dependency through a single ``r.``
prefix, and ``tests/test_registration_operations.py`` asserts the field set is
*exactly* the set of ``r.<name>`` reads -- so nested groups would break both.
The grouping below is therefore ordering + documentation only; see
``OPERATION_GROUPS`` for the machine-readable partition.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Callable, Mapping

# Machine-readable grouping. Documentation for humans and tooling -- it does not
# change attribute access. Held to an exact partition of the dataclass fields by
# tests/test_registration_operations.py::test_operation_groups_partition_fields,
# so adding a field without filing it into a group fails the suite.
OPERATION_GROUPS: dict[str, tuple[str, ...]] = {
    "config": (
        "REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS",
        "current_config_data",
        "runtime_config_scope",
        "validate_config",
    ),
    "http_transport": (
        "SyntheticResponse",
        "_auth_request_headers",
        "_json_or_raw",
        "_resolve_proxy_scheme",
        "chatgpt_headers",
        "nextauth_headers",
        "openai_auth_headers",
        "registration_network_preflight",
        "request_with_retry",
    ),
    "sentinel_fingerprint": (
        "_extract_sentinel",
        "_import_sentinel_cookies",
        "_sentinel_device_id",
        "_set_oai_did_cookie",
        "assert_sentinel_device_id",
        "auth_impersonate",
        "select_auth_fingerprint",
        "set_fingerprint_device",
        "set_fingerprint_geo",
    ),
    "auth_session": (
        "_auth_session_access_token",
        "_fetch_auth_session",
        "_fetch_client_auth_session_dump",
        "_oauth_result_summary",
        "_prepare_signup_auth_state",
        "_probe_registration_access_token",
    ),
    "signup_navigation": (
        "_create_account_continue_url",
        "_follow_continue_url",
        "_is_chatgpt_auth_login_landing",
        "_is_signup_password_step",
        "_is_user_already_exists",
        "_login_existing_account_with_email_otp",
        "_passwordless_signin_attempts",
        "_signup_signin_attempts",
    ),
    "generated_identity": (
        "_generate_password",
        "_normalize_registration_mode",
        "_random_birthdate",
        "_random_name",
        "_stored_registration_password",
    ),
    "email_otp_mailbox": (
        "_email_otp_send_url",
        "_ensure_mailbox_account",
        "_is_wrong_email_otp_code",
        "_mailbox_snapshot",
        "_poll_registration_email_otp",
        "_send_registration_email_otp",
        "_snapshot_mailbox_message",
        "_validate_email_otp",
    ),
    "checkpoint_outcome_telemetry": (
        "_failure_result",
        "_print_timings",
        "_registration_outcome",
        "_retain_registration_checkpoint",
        "_safe_tock",
        "_sanitize_text",
        "_tick",
        "_timing_summary",
        "_tl",
        "think_stage",
    ),
}


@dataclass(frozen=True)
class RegistrationOperations:
    # --- config & runtime scope -------------------------------------------
    REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS: str
    current_config_data: Callable[..., Any]
    runtime_config_scope: Callable[..., Any]
    validate_config: Callable[..., Any]

    # --- http transport ----------------------------------------------------
    SyntheticResponse: Callable[..., Any]
    _auth_request_headers: Callable[..., Any]
    _json_or_raw: Callable[..., Any]
    _resolve_proxy_scheme: Callable[..., Any]
    chatgpt_headers: Callable[..., Any]
    nextauth_headers: Callable[..., Any]
    openai_auth_headers: Callable[..., Any]
    registration_network_preflight: Callable[..., Any]
    request_with_retry: Callable[..., Any]

    # --- sentinel / device fingerprint ------------------------------------
    _extract_sentinel: Callable[..., Any]
    _import_sentinel_cookies: Callable[..., Any]
    _sentinel_device_id: Callable[..., Any]
    _set_oai_did_cookie: Callable[..., Any]
    assert_sentinel_device_id: Callable[..., Any]
    auth_impersonate: Callable[..., Any]
    select_auth_fingerprint: Callable[..., Any]
    set_fingerprint_device: Callable[..., Any]
    set_fingerprint_geo: Callable[..., Any]

    # --- auth session & token ---------------------------------------------
    _auth_session_access_token: Callable[..., Any]
    _fetch_auth_session: Callable[..., Any]
    _fetch_client_auth_session_dump: Callable[..., Any]
    _oauth_result_summary: Callable[..., Any]
    _prepare_signup_auth_state: Callable[..., Any]
    _probe_registration_access_token: Callable[..., Any]

    # --- signup navigation -------------------------------------------------
    _create_account_continue_url: Callable[..., Any]
    _follow_continue_url: Callable[..., Any]
    _is_chatgpt_auth_login_landing: Callable[..., Any]
    _is_signup_password_step: Callable[..., Any]
    _is_user_already_exists: Callable[..., Any]
    _login_existing_account_with_email_otp: Callable[..., Any]
    _passwordless_signin_attempts: Callable[..., Any]
    _signup_signin_attempts: Callable[..., Any]

    # --- generated identity ------------------------------------------------
    _generate_password: Callable[..., Any]
    _normalize_registration_mode: Callable[..., Any]
    _random_birthdate: Callable[..., Any]
    _random_name: Callable[..., Any]
    _stored_registration_password: Callable[..., Any]

    # --- email otp & mailbox -----------------------------------------------
    _email_otp_send_url: Callable[..., Any]
    _ensure_mailbox_account: Callable[..., Any]
    _is_wrong_email_otp_code: Callable[..., Any]
    _mailbox_snapshot: Callable[..., Any]
    _poll_registration_email_otp: Callable[..., Any]
    _send_registration_email_otp: Callable[..., Any]
    _snapshot_mailbox_message: Callable[..., Any]
    _validate_email_otp: Callable[..., Any]

    # --- checkpoint, outcome & telemetry -----------------------------------
    _failure_result: Callable[..., Any]
    _print_timings: Callable[..., Any]
    _registration_outcome: Callable[..., Any]
    _retain_registration_checkpoint: Callable[..., Any]
    _safe_tock: Callable[..., Any]
    _sanitize_text: Callable[..., Any]
    _tick: Callable[..., Any]
    _timing_summary: Callable[..., Any]
    _tl: Callable[..., Any]
    think_stage: Callable[..., Any]

    @classmethod
    def bind(cls, namespace: Mapping[str, Any]) -> "RegistrationOperations":
        """Bind every declared field from ``namespace``.

        A missing key used to surface as a bare ``KeyError`` carrying only the
        first missing name, which told you nothing about how much else was
        missing. Listing them all makes a broken facade a one-shot diagnosis
        instead of a fix-one-rerun loop.
        """
        required = tuple(item.name for item in fields(cls))
        missing = [name for name in required if name not in namespace]
        if missing:
            raise KeyError(
                f"RegistrationOperations.bind: namespace is missing "
                f"{len(missing)}/{len(required)} dependencies: {', '.join(missing)}"
            )
        return cls(**{name: namespace[name] for name in required})
