//! Rust port of `text_has_account_deactivated` from
//! `sms_tool/accounts/account_terminal.py`.

#![forbid(unsafe_code)]

const DEACTIVATED_MARKERS: [&str; 5] = [
    "account_deactivated",
    "account_deatived",
    "deleted or deactivated",
    "account has been deleted",
    "account has been deactivated",
];

#[inline]
fn ascii_has_deactivated_marker(bytes: &[u8]) -> bool {
    let n = bytes.len();
    if n < 16 {
        return false;
    }
    let limit = n - 16;
    let mut i = 0;
    while i <= limit {
        let lead = bytes[i] | 0x20;
        if lead == b'a' {
            let rest = &bytes[i..];
            if (rest[..8].eq_ignore_ascii_case(b"account_")
                && ((rest.len() >= 19 && rest[8..19].eq_ignore_ascii_case(b"deactivated"))
                    || rest[8..16].eq_ignore_ascii_case(b"deatived")))
                || (rest.len() >= 24
                    && rest[..17].eq_ignore_ascii_case(b"account has been ")
                    && (rest[17..24].eq_ignore_ascii_case(b"deleted")
                        || (rest.len() >= 28 && rest[17..28].eq_ignore_ascii_case(b"deactivated"))))
            {
                return true;
            }
        } else if lead == b'd'
            && bytes[i..].len() >= 22
            && bytes[i..i + 22].eq_ignore_ascii_case(b"deleted or deactivated")
        {
            return true;
        }
        i += 1;
    }
    false
}

pub fn text_has_account_deactivated(text: Option<String>) -> Result<bool, String> {
    let Some(raw) = text else {
        return Ok(false);
    };
    if raw.len() < 16 {
        return Ok(false);
    }
    if raw.is_ascii() {
        return Ok(ascii_has_deactivated_marker(raw.as_bytes()));
    }
    let lowered = raw.to_lowercase();
    Ok(DEACTIVATED_MARKERS.iter().any(|m| lowered.contains(m)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deactivated_marker_scans_match_python() {
        assert!(!text_has_account_deactivated(None).unwrap());
        assert!(!text_has_account_deactivated(Some("short".into())).unwrap());
        assert!(text_has_account_deactivated(Some("err: ACCOUNT_DEACTIVATED now".into())).unwrap());
        assert!(text_has_account_deactivated(Some("legacy account_deatived row".into())).unwrap());
        assert!(
            text_has_account_deactivated(Some("Your Account Has Been Deleted.".into())).unwrap()
        );
        assert!(
            text_has_account_deactivated(Some("This account has been deactivated".into())).unwrap()
        );
        assert!(text_has_account_deactivated(Some("User deleted or deactivated".into())).unwrap());
        assert!(
            !text_has_account_deactivated(Some("temporary 429 rate_limit_exceeded".into()))
                .unwrap()
        );
    }
}
