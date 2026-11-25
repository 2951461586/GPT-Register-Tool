//! Rust port of `_normalized_contract_value` and `_canonical_terminal_state`
//! from `sms_tool/pay_link/normalize.py`.

#![forbid(unsafe_code)]

use serde_json::Value;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn normalize_contract_str(s: &str) -> String {
    let trimmed = py_trim(s);
    if trimmed.is_empty() {
        return String::new();
    }
    let mut out = String::with_capacity(trimmed.len());
    let mut pending_sep = false;
    let mut has_output = false;
    if trimmed.is_ascii() {
        for &b in trimmed.as_bytes() {
            let lb = b.to_ascii_lowercase();
            if lb.is_ascii_lowercase() || lb.is_ascii_digit() {
                if pending_sep {
                    out.push('_');
                    pending_sep = false;
                }
                out.push(lb as char);
                has_output = true;
            } else if has_output {
                pending_sep = true;
            }
        }
    } else {
        for ch in trimmed.chars() {
            for lc in ch.to_lowercase() {
                if matches!(lc, 'a'..='z' | '0'..='9') {
                    if pending_sep {
                        out.push('_');
                        pending_sep = false;
                    }
                    out.push(lc);
                    has_output = true;
                } else if has_output {
                    pending_sep = true;
                }
            }
        }
    }
    out
}

pub fn _normalized_contract_value(value: serde_json::Value) -> Result<String, String> {
    match value {
        Value::String(s) => Ok(normalize_contract_str(&s)),
        _ => Ok(String::new()),
    }
}

pub fn _canonical_terminal_state(value: serde_json::Value) -> Result<String, String> {
    let Value::String(s) = value else {
        return Ok(String::new());
    };
    let normalized = normalize_contract_str(&s);
    if matches!(
        normalized.as_str(),
        "cancelled"
            | "canceled"
            | "cancelled_by_user"
            | "canceled_by_user"
            | "interrupted"
            | "keyboard_interrupt"
            | "keyboardinterrupt"
    ) || normalized.ends_with("_cancelled")
        || normalized.ends_with("_canceled")
    {
        return Ok("cancelled".to_string());
    }
    if matches!(
        normalized.as_str(),
        "timed_out" | "timeout" | "timeout_expired" | "extractor_timeout"
    ) || normalized.ends_with("_timed_out")
        || normalized.ends_with("_timeout")
    {
        return Ok("timed_out".to_string());
    }
    if matches!(
        normalized.as_str(),
        "unknown"
            | "outcome_unknown"
            | "payment_outcome_unknown"
            | "indeterminate"
            | "inconclusive"
    ) || normalized.ends_with("_outcome_unknown")
    {
        return Ok("unknown".to_string());
    }
    Ok(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn normalizes_contract_values() {
        assert_eq!(
            _normalized_contract_value(json!("  HTTP 503 Service-Unavailable! ")).unwrap(),
            "http_503_service_unavailable"
        );
        assert_eq!(_normalized_contract_value(json!(123)).unwrap(), "");
    }

    #[test]
    fn canonicalizes_terminal_states() {
        assert_eq!(
            _canonical_terminal_state(json!("Cancelled-By-User")).unwrap(),
            "cancelled"
        );
        assert_eq!(
            _canonical_terminal_state(json!("http_read_timeout")).unwrap(),
            "timed_out"
        );
        assert_eq!(
            _canonical_terminal_state(json!("confirm_outcome_unknown")).unwrap(),
            "unknown"
        );
        assert_eq!(_canonical_terminal_state(json!("completed")).unwrap(), "");
    }
}
