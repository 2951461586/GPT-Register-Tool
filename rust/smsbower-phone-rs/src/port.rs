//! Rust port of `normalize_phone` in `sms_tool/phone/smsbower.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn non_nd_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"[^\p{Nd}\u{00B2}\u{00B3}\u{00B9}\u{2070}\u{2074}-\u{2079}\u{2080}-\u{2089}\u{2460}-\u{2468}\u{2474}-\u{247C}\u{2488}-\u{2490}\u{24EA}\u{24F5}-\u{24FD}\u{24FF}\u{2776}-\u{277E}\u{2780}-\u{2788}\u{278A}-\u{2792}]+")
            .expect("valid regex")
    })
}

fn extract_digits(s: &str) -> String {
    if s.is_ascii() {
        let mut out = String::with_capacity(s.len());
        for b in s.bytes() {
            if b.is_ascii_digit() {
                out.push(b as char);
            }
        }
        return out;
    }
    non_nd_re().replace_all(s, "").into_owned()
}

pub fn normalize_phone(phone: String) -> Result<String, String> {
    let value = py_trim(&phone);
    if value.is_empty() {
        return Ok(String::new());
    }
    if let Some(rest) = value.strip_prefix('+') {
        let mut out = String::with_capacity(rest.len() + 1);
        out.push('+');
        out.push_str(&extract_digits(rest));
        return Ok(out);
    }
    if let Some(rest) = value.strip_prefix("00") {
        let mut out = String::with_capacity(rest.len() + 1);
        out.push('+');
        out.push_str(&extract_digits(rest));
        return Ok(out);
    }
    let digits = extract_digits(value);
    if digits.is_empty() {
        Ok(String::new())
    } else {
        Ok(format!("+{digits}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_phone_with_plus_prefix() {
        assert_eq!(
            normalize_phone("  +233 55-123-4567 ".to_string()).unwrap(),
            "+233551234567"
        );
        assert_eq!(
            normalize_phone("0086-138-0013-8000".to_string()).unwrap(),
            "+8613800138000"
        );
        assert_eq!(
            normalize_phone("233 55 123 4567".to_string()).unwrap(),
            "+233551234567"
        );
        assert_eq!(normalize_phone("abc".to_string()).unwrap(), "");
        assert_eq!(normalize_phone("+".to_string()).unwrap(), "+");
    }
}
