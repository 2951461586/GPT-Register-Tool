//! Rust port of `_needs_reauth` from `sms_tool/accounts/account_2fa.py`.

#![forbid(unsafe_code)]

const REAUTH_MARKERS: [&str; 5] = [
    "recent_auth_required",
    "reauth",
    "login_challenge",
    "mfa enroll http 401",
    "mfa enroll http 403",
];

#[inline]
fn ascii_needs_reauth(bytes: &[u8]) -> bool {
    let n = bytes.len();
    if n < 6 {
        return false;
    }
    let limit = n - 6;
    let mut i = 0;
    while i <= limit {
        let matched = match bytes[i] | 0x20 {
            b'r' => {
                bytes[i..i + 6].eq_ignore_ascii_case(b"reauth")
                    || (n - i >= 20
                        && bytes[i..i + 20].eq_ignore_ascii_case(b"recent_auth_required"))
            }
            b'l' => n - i >= 15 && bytes[i..i + 15].eq_ignore_ascii_case(b"login_challenge"),
            b'm' => {
                n - i >= 19
                    && bytes[i..i + 18].eq_ignore_ascii_case(b"mfa enroll http 40")
                    && matches!(bytes[i + 18], b'1' | b'3')
            }
            _ => false,
        };
        if matched {
            return true;
        }
        i += 1;
    }
    false
}

pub fn _needs_reauth(err_text: Option<String>) -> Result<bool, String> {
    let raw = err_text.as_deref().unwrap_or("None");
    if raw.len() < 6 {
        return Ok(false);
    }
    if raw.is_ascii() {
        return Ok(ascii_needs_reauth(raw.as_bytes()));
    }
    let lowered = raw.to_lowercase();
    Ok(REAUTH_MARKERS.iter().any(|m| lowered.contains(m)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_reauth_markers_case_insensitively() {
        assert!(!_needs_reauth(None).unwrap());
        assert!(!_needs_reauth(Some("ok".into())).unwrap());
        assert!(_needs_reauth(Some("Error: RECENT_AUTH_REQUIRED".into())).unwrap());
        assert!(_needs_reauth(Some("Please reauth to continue".into())).unwrap());
        assert!(_needs_reauth(Some("step=Login_Challenge".into())).unwrap());
        assert!(_needs_reauth(Some("MFA enroll HTTP 401 unauthorized".into())).unwrap());
        assert!(_needs_reauth(Some("MFA enroll HTTP 403 forbidden".into())).unwrap());
        assert!(!_needs_reauth(Some("MFA enroll HTTP 500 server error".into())).unwrap());
    }
}
