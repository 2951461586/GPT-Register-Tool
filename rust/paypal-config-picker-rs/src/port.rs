//! Rust port of `_safe_paypal_session_stem` from `sms_tool/paypal/config_picker.py`.

#![forbid(unsafe_code)]

pub fn _safe_paypal_session_stem(email: Option<String>) -> Result<String, String> {
    let owned = email.unwrap_or_default();
    let source = if owned.is_empty() {
        "unknown"
    } else {
        owned.as_str()
    };
    let mut out = String::with_capacity(source.len());
    let mut in_bad_run = false;
    for ch in source.chars() {
        if ch == '+' {
            continue;
        }
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '@' | '-') {
            out.push(ch);
            in_bad_run = false;
        } else if !in_bad_run {
            out.push('_');
            in_bad_run = true;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizes_session_stem() {
        assert_eq!(
            _safe_paypal_session_stem(Some("alice+tag/bad space@example.com".into())),
            Ok("alicetag_bad_space@example.com".into())
        );
        assert_eq!(
            _safe_paypal_session_stem(Some("".into())),
            Ok("unknown".into())
        );
    }
}
