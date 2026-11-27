//! Rust port of `_browser_failure_class` from
//! `sms_tool/registration_drivers/browser_flow/session.py`.

#![forbid(unsafe_code)]

const RATE_LIMIT_MARKERS: &[&str] = &["rate_limited", "rate_limit"];
const ACCOUNT_MARKERS: &[&str] = &["existing_account", "session_account_not_linked"];
const MAILBOX_MARKERS: &[&str] = &["otp_timeout", "otp_rejected", "mailbox", "email_otp"];
const AUTH_STATE_MARKERS: &[&str] = &[
    "identity_provider",
    "manual_challenge",
    "session_email_mismatch",
    "session_access_token_missing",
    "session_unauthorized",
    "session_forbidden",
    "session_access_denied",
    "session_oauth_callback_failed",
    "session_token_refresh_failed",
    "chatgpt_context_unavailable",
    "profile_",
    "passwordless_otp",
    "otp_restart_state",
    "registration_state_unknown",
    "email_verification",
    "email_field",
    "auth_state",
    "email_value_mismatch",
];
const CONFIG_MARKERS: &[&str] = &[
    "dependency_missing",
    "api_key_missing",
    "workspace_id_missing",
    "profile_create_failed",
    "debug_address_missing",
    "session_endpoint_unavailable",
    "config",
    "unsupported",
];

pub fn _browser_failure_class(code: Option<String>) -> Result<String, String> {
    let raw = code.unwrap_or_default();
    let value = if raw.is_ascii() {
        raw.to_ascii_lowercase()
    } else {
        raw.to_lowercase()
    };
    if value.contains("registration_cancelled") {
        return Ok("cancelled".to_string());
    }
    if RATE_LIMIT_MARKERS.iter().any(|m| value.contains(m)) {
        return Ok("rate_limit".to_string());
    }
    if ACCOUNT_MARKERS.iter().any(|m| value.contains(m)) {
        return Ok("account".to_string());
    }
    if MAILBOX_MARKERS.iter().any(|m| value.contains(m)) {
        return Ok("mailbox".to_string());
    }
    if AUTH_STATE_MARKERS.iter().any(|m| value.contains(m)) {
        return Ok("auth_state".to_string());
    }
    if value.contains("proxy_blocked") || value.contains("proxy_country_mismatch") {
        return Ok("network".to_string());
    }
    if CONFIG_MARKERS.iter().any(|m| value.contains(m)) {
        return Ok("configuration".to_string());
    }
    Ok("network".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_all_browser_failure_branches() {
        assert_eq!(_browser_failure_class(None).unwrap(), "network");
        assert_eq!(
            _browser_failure_class(Some("REGISTRATION_CANCELLED".into())).unwrap(),
            "cancelled"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_session_rate_limited".into())).unwrap(),
            "rate_limit"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_existing_account".into())).unwrap(),
            "account"
        );
        assert_eq!(
            _browser_failure_class(Some("email_otp_timeout".into())).unwrap(),
            "mailbox"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_email_value_mismatch".into())).unwrap(),
            "auth_state"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_profile_create_failed".into())).unwrap(),
            "auth_state"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_proxy_blocked".into())).unwrap(),
            "network"
        );
        assert_eq!(
            _browser_failure_class(Some("browser_api_key_missing".into())).unwrap(),
            "configuration"
        );
    }
}
