//! Rust port of `coupon_url_for_campaign` from
//! `sms_tool/accounts/account_promotion.py`.

#![forbid(unsafe_code)]

const URL_PREFIX: &str = "https://chatgpt.com/backend-api/promo_campaign/check_coupon?coupon=";
const URL_SUFFIX: &str = "&is_coupon_from_query_param=true";
const DEFAULT_CAMPAIGN_ID: &str = "plus-1-month-free";
const HEX: &[u8; 16] = b"0123456789ABCDEF";

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

#[inline]
fn is_rfc3986_unreserved(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'~')
}

pub fn coupon_url_for_campaign(campaign_id: Option<String>) -> Result<String, String> {
    let raw = campaign_id.unwrap_or_default();
    let trimmed = py_trim(&raw);
    let resolved = if trimmed.is_empty() {
        DEFAULT_CAMPAIGN_ID
    } else {
        trimmed
    };
    let mut out = String::with_capacity(URL_PREFIX.len() + resolved.len() * 3 + URL_SUFFIX.len());
    out.push_str(URL_PREFIX);
    for &b in resolved.as_bytes() {
        if is_rfc3986_unreserved(b) {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(HEX[(b >> 4) as usize] as char);
            out.push(HEX[(b & 0x0f) as usize] as char);
        }
    }
    out.push_str(URL_SUFFIX);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_encoded_coupon_url() {
        assert_eq!(
            coupon_url_for_campaign(None).unwrap(),
            "https://chatgpt.com/backend-api/promo_campaign/check_coupon?coupon=plus-1-month-free&is_coupon_from_query_param=true"
        );
        assert_eq!(
            coupon_url_for_campaign(Some("  team-1-month-free  ".into())).unwrap(),
            "https://chatgpt.com/backend-api/promo_campaign/check_coupon?coupon=team-1-month-free&is_coupon_from_query_param=true"
        );
        assert_eq!(
            coupon_url_for_campaign(Some("a/b c+d%e".into())).unwrap(),
            "https://chatgpt.com/backend-api/promo_campaign/check_coupon?coupon=a%2Fb%20c%2Bd%25e&is_coupon_from_query_param=true"
        );
    }
}
