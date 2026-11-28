//! Rust port of `_detect_protocol_stage` from `sms_tool/codex/oauth_helpers.py`.

#![forbid(unsafe_code)]

use std::borrow::Cow;

pub fn _detect_protocol_stage(url: Option<String>) -> Result<String, String> {
    let value = url.unwrap_or_default();
    if value.contains("code=") && value.contains("state=") {
        return Ok("callback".into());
    }
    let lower: Cow<'_, str> = if value.is_ascii() {
        if value.bytes().any(|b| b.is_ascii_uppercase()) {
            Cow::Owned(value.to_ascii_lowercase())
        } else {
            Cow::Borrowed(&value)
        }
    } else {
        Cow::Owned(value.to_lowercase())
    };
    let s = lower.as_ref();
    let stage = if s.contains("/add-phone") || s.contains("phone-verification") {
        "add_phone"
    } else if s.contains("email-verification") || s.contains("email-otp") {
        "email_otp"
    } else if s.contains("/log-in/password")
        || s.contains("/login/password")
        || s.ends_with("/password")
    {
        "password"
    } else if s.ends_with("/consent") || s.ends_with("/workspace") {
        "consent"
    } else if s.contains("/authorize") || s.contains("/oauth/") {
        "email"
    } else {
        "unknown"
    };
    Ok(stage.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_oauth_protocol_stages() {
        assert_eq!(
            _detect_protocol_stage(Some(
                "http://localhost:1455/auth/callback?code=abc&state=xyz".into()
            )),
            Ok("callback".into())
        );
        assert_eq!(
            _detect_protocol_stage(Some("https://auth.openai.com/email-verification".into())),
            Ok("email_otp".into())
        );
        assert_eq!(
            _detect_protocol_stage(Some("https://auth.openai.com/log-in/password".into())),
            Ok("password".into())
        );
        assert_eq!(
            _detect_protocol_stage(Some("https://auth.openai.com/oauth/authorize".into())),
            Ok("email".into())
        );
        assert_eq!(_detect_protocol_stage(None), Ok("unknown".into()));
    }
}
