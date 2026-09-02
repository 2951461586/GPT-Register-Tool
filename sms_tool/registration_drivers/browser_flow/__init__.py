r"""Playwright 浏览器注册流程实现。

历史上全部实现都在 ``registration_drivers/playwright.py``（2035 行单文件）。
本包按职责分层拆开，原模块退化为 re-export 薄壳，保证外部 import 与测试
monkeypatch 面不变。分层（由低到高）：

    dom_fields -> page_state -> form_steps -> flow_steps -> orchestrator
                             \-> session
"""

from .dom_fields import (
    _body_text,
    _browser_heartbeat,
    _click_continue,
    _click_first_visible,
    _click_passwordless_otp,
    _click_resend,
    _config_value,
    _first_visible,
    _hard_proxy_block,
    _is_openai_auth_url,
    _otp_fields,
    _otp_page_state,
    _page_is_alive,
    _prepare_session_page,
    _safe_text,
    _session_context_closed,
    _session_error_marker,
    _terminal_session_error,
    _unexpected_identity_provider,
)
from .page_state import (
    _ensure_signup_page_ready,
    _manual_challenge,
    _post_otp_registration_state,
    _profile_completion_required,
    _quick_auth_state,
    _wait_after_otp_submit,
    _wait_for_challenge_clear,
    _wait_for_profile_completion,
    _wait_for_registration_state,
)
from .form_steps import (
    _complete_profile,
    _fill_email,
    _fill_otp,
    _fill_password_if_present,
    _maybe_accept_cookies,
    _maybe_dismiss_chatgpt_onboarding,
    _safe_submit_email_form,
    _submit_email_via_nextauth,
)
from .session import (
    _bind_totp_in_browser,
    _browser_access_token_probe,
    _browser_diagnostics,
    _browser_failure_class,
    _post_registration_dwell,
    _safe_proxy_audit,
    _session_payload,
)
from .flow_steps import (
    _BROWSER_POOL,
    _BROWSER_POOL_KEY,
    _BROWSER_POOL_LOCK,
    _browser_session_scope,
    _poll_browser_otp,
    _restart_email_otp_flow,
)
from .orchestrator import (
    build_browser_session_file,
    run_browser_registration,
    run_playwright_registration,
)

__all__ = [
    "_BROWSER_POOL",
    "_BROWSER_POOL_KEY",
    "_BROWSER_POOL_LOCK",
    "_bind_totp_in_browser",
    "_body_text",
    "_browser_access_token_probe",
    "_browser_diagnostics",
    "_browser_failure_class",
    "_browser_heartbeat",
    "_browser_session_scope",
    "_click_continue",
    "_click_first_visible",
    "_click_passwordless_otp",
    "_click_resend",
    "_complete_profile",
    "_config_value",
    "_ensure_signup_page_ready",
    "_fill_email",
    "_fill_otp",
    "_fill_password_if_present",
    "_first_visible",
    "_hard_proxy_block",
    "_is_openai_auth_url",
    "_manual_challenge",
    "_maybe_accept_cookies",
    "_maybe_dismiss_chatgpt_onboarding",
    "_otp_fields",
    "_otp_page_state",
    "_page_is_alive",
    "_poll_browser_otp",
    "_post_otp_registration_state",
    "_post_registration_dwell",
    "_prepare_session_page",
    "_profile_completion_required",
    "_quick_auth_state",
    "_restart_email_otp_flow",
    "_safe_proxy_audit",
    "_safe_submit_email_form",
    "_safe_text",
    "_session_context_closed",
    "_session_error_marker",
    "_session_payload",
    "_submit_email_via_nextauth",
    "_terminal_session_error",
    "_unexpected_identity_provider",
    "_wait_after_otp_submit",
    "_wait_for_challenge_clear",
    "_wait_for_profile_completion",
    "_wait_for_registration_state",
    "build_browser_session_file",
    "run_browser_registration",
    "run_playwright_registration",
]
