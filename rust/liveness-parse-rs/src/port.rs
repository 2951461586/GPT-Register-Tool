//! Rust port of invalid-token status/error classification in
//! `sms_tool/accounts/account_liveness.py`.

#[inline]
fn py_is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn has_word_401(s: &str) -> bool {
    let bytes = s.as_bytes();
    let mut pos = 0usize;
    while let Some(rel) = s[pos..].find("401") {
        let start = pos + rel;
        let end = start + 3;
        let left_ok = s[..start]
            .chars()
            .next_back()
            .is_none_or(|c| !py_is_word(c));
        let right_ok = s[end..].chars().next().is_none_or(|c| !py_is_word(c));
        if left_ok && right_ok {
            return true;
        }
        pos = start + 1;
        if pos >= bytes.len() {
            break;
        }
    }
    false
}

pub fn _is_token_invalid(status_code: f64, error_text: String) -> Result<bool, String> {
    if status_code.is_infinite() {
        return Err("OverflowError: cannot convert float infinity to integer".to_string());
    }
    if status_code.is_finite() && (401.0..402.0).contains(&status_code) {
        return Ok(true);
    }
    if error_text.is_empty() {
        return Ok(false);
    }
    let lowered = error_text.to_lowercase();
    if lowered.contains("unauthorized")
        || lowered.contains("token has been invalidated")
        || lowered.contains("invalid_grant")
        || lowered.contains("refresh_token")
    {
        return Ok(true);
    }
    Ok(has_word_401(&lowered))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_401_and_invalid_token_markers() {
        assert!(_is_token_invalid(401.0, String::new()).unwrap());
        assert!(_is_token_invalid(401.9, String::new()).unwrap());
        assert!(_is_token_invalid(200.0, "HTTP 401 failed".to_string()).unwrap());
        assert!(!_is_token_invalid(200.0, "err 14012 ok".to_string()).unwrap());
        assert!(
            _is_token_invalid(
                403.0,
                "Your authentication token has been invalidated".to_string()
            )
            .unwrap()
        );
        assert!(_is_token_invalid(400.0, "error: INVALID_GRANT".to_string()).unwrap());
        assert!(!_is_token_invalid(500.0, "gateway timeout".to_string()).unwrap());
        assert!(!_is_token_invalid(f64::NAN, "ok".to_string()).unwrap());
        assert!(_is_token_invalid(f64::INFINITY, String::new()).is_err());
    }
}
