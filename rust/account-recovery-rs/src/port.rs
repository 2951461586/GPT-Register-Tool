//! Rust port of `_redact_recovery_error` and `_relogin_failure_quota_status`
//! from `sms_tool/accounts/account_recovery.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

const NO_CODE_SPENT_MARKERS: &[&str] = &[
    "existing_login_password_verify_failed",
    "existing_login_password_required",
    "existing_login_password_step_unknown",
];

const CODE_PHASE_MARKERS: &[&str] = &["otp", "email_code", "verification_code", "mailbox"];

struct RecoveryRegexes {
    proxy_creds: Regex,
    refresh_token: Regex,
    jwt_token: Regex,
}

fn rx() -> &'static RecoveryRegexes {
    static CACHE: OnceLock<RecoveryRegexes> = OnceLock::new();
    CACHE.get_or_init(|| RecoveryRegexes {
        proxy_creds: Regex::new(r"(?i)((?:https?|socks5h?)://)[^@\s/]+@").expect("valid regex"),
        refresh_token: Regex::new(r"\brt_[A-Za-z0-9._~-]+").expect("valid regex"),
        jwt_token: Regex::new(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
            .expect("valid regex"),
    })
}

fn is_py_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => {
            n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0)
        }
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

fn py_str_repr(s: &str, out: &mut String) {
    let use_double = s.contains('\'') && !s.contains('"');
    let quote = if use_double { '"' } else { '\'' };
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\'' if !use_double => out.push_str("\\'"),
            '"' if use_double => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                use std::fmt::Write;
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
}

fn py_repr_into(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("None"),
        Value::Bool(true) => out.push_str("True"),
        Value::Bool(false) => out.push_str("False"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => py_str_repr(s, out),
        Value::Array(arr) => {
            out.push('[');
            for (idx, item) in arr.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_repr_into(item, out);
            }
            out.push(']');
        }
        Value::Object(obj) => {
            out.push('{');
            for (idx, (k, v)) in obj.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_str_repr(k, out);
                out.push_str(": ");
                py_repr_into(v, out);
            }
            out.push('}');
        }
    }
}

fn py_str_or_empty(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Number(n) => {
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(a) if a.is_empty() => String::new(),
        Value::Object(o) if o.is_empty() => String::new(),
        other => {
            let mut out = String::new();
            py_repr_into(other, &mut out);
            out
        }
    }
}

fn truncate_chars(mut s: String, max_chars: usize) -> String {
    if s.len() <= max_chars {
        return s;
    }
    if let Some((byte_idx, _)) = s.char_indices().nth(max_chars) {
        s.truncate(byte_idx);
    }
    s
}

pub fn _redact_recovery_error(value: serde_json::Value) -> Result<String, String> {
    let text = py_str_or_empty(&value);
    if text.is_empty() {
        return Ok(String::new());
    }
    let regs = rx();
    let step1 = regs.proxy_creds.replace_all(&text, "${1}[REDACTED]@");
    let step2 = regs.refresh_token.replace_all(&step1, "rt_[REDACTED]");
    let step3 = regs
        .jwt_token
        .replace_all(&step2, "[REDACTED_JWT]")
        .into_owned();
    Ok(truncate_chars(step3, 1000))
}

#[derive(Default)]
struct MarkerFlags {
    deactivated: bool,
    phone_verify: bool,
    mailbox_transport: bool,
    mailbox_auth_invalid: bool,
    cooldown: bool,
}

#[inline]
fn contains_ascii_ci(haystack: &str, needle: &[u8]) -> bool {
    let bytes = haystack.as_bytes();
    let n = needle.len();
    if bytes.len() < n {
        return false;
    }
    bytes.windows(n).any(|w| w.eq_ignore_ascii_case(needle))
}

fn scan_str_markers(s: &str, flags: &mut MarkerFlags) {
    if s.len() < 8 {
        return;
    }
    if !flags.deactivated
        && (contains_ascii_ci(s, b"account_deactivated")
            || contains_ascii_ci(s, b"deleted or deactivated"))
    {
        flags.deactivated = true;
    }
    if !flags.phone_verify
        && (contains_ascii_ci(s, b"add_phone") || contains_ascii_ci(s, b"phone_verification"))
    {
        flags.phone_verify = true;
    }
    if !flags.mailbox_transport && contains_ascii_ci(s, b"mailbox_transport") {
        flags.mailbox_transport = true;
    }
    if !flags.mailbox_auth_invalid && contains_ascii_ci(s, b"mailbox_auth_invalid") {
        flags.mailbox_auth_invalid = true;
    }
    if !flags.cooldown && contains_ascii_ci(s, b"cooldown") {
        flags.cooldown = true;
    }
}

fn scan_value_markers(value: &Value, flags: &mut MarkerFlags) {
    if flags.deactivated {
        return;
    }
    match value {
        Value::String(s) => scan_str_markers(s, flags),
        Value::Array(arr) => {
            for item in arr {
                scan_value_markers(item, flags);
                if flags.deactivated {
                    return;
                }
            }
        }
        Value::Object(map) => {
            for (k, v) in map {
                scan_str_markers(k, flags);
                if flags.deactivated {
                    return;
                }
                scan_value_markers(v, flags);
                if flags.deactivated {
                    return;
                }
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
}

fn relogin_spent_email_code(relogin: &Value) -> bool {
    let Value::Object(map) = relogin else {
        return false;
    };
    let mut errors: Vec<String> = Vec::new();
    if let Some(Value::Array(attempts)) = map.get("attempts")
        && !attempts.is_empty()
    {
        for item in attempts {
            if let Value::Object(att_map) = item {
                let err = att_map
                    .get("error")
                    .map(py_str_or_empty)
                    .unwrap_or_default()
                    .to_lowercase();
                errors.push(err);
            }
        }
    }
    if errors.is_empty() {
        let top_err = map
            .get("error")
            .map(py_str_or_empty)
            .unwrap_or_default()
            .to_lowercase();
        errors.push(top_err);
    }
    for error in &errors {
        if error.is_empty() {
            continue;
        }
        if NO_CODE_SPENT_MARKERS.iter().any(|m| error.contains(m)) {
            continue;
        }
        if CODE_PHASE_MARKERS.iter().any(|m| error.contains(m)) {
            return true;
        }
    }
    false
}

pub fn _relogin_failure_quota_status(relogin: serde_json::Value) -> Result<String, String> {
    let mut flags = MarkerFlags::default();
    if !is_py_falsy(&relogin) {
        scan_value_markers(&relogin, &mut flags);
    }
    if flags.deactivated {
        return Ok("account_deactivated".to_string());
    }
    if flags.phone_verify {
        return Ok("phone_verification_required".to_string());
    }
    if flags.mailbox_transport {
        return Ok("relogin_mailbox_transport_failed".to_string());
    }
    if flags.mailbox_auth_invalid {
        return Ok("mailbox_auth_invalid".to_string());
    }
    if relogin_spent_email_code(&relogin) {
        return Ok("relogin_otp_failed".to_string());
    }
    if flags.cooldown {
        return Ok("relogin_cooldown".to_string());
    }
    Ok("relogin_failed".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn redacts_proxy_refresh_token_and_jwt() {
        let raw = "err via http://u1:p1@proxy.example:8080 rt_abc123.def eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig_x";
        let out = _redact_recovery_error(json!(raw)).unwrap();
        assert_eq!(
            out,
            "err via http://[REDACTED]@proxy.example:8080 rt_[REDACTED] [REDACTED_JWT]"
        );
    }

    #[test]
    fn classifies_relogin_failures() {
        let payload = json!({
            "ok": false,
            "attempts": [
                {"mode": "web_session", "error": "session_401", "last_status": 401},
                {"mode": "chatgpt_email_otp", "error": "otp_verify_failed {\"status\": 409, \"code\": \"invalid_state\"}"}
            ]
        });
        assert_eq!(
            _relogin_failure_quota_status(payload).unwrap(),
            "relogin_otp_failed"
        );
    }
}
