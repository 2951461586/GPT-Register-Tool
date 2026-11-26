//! Rust port of `api_phone_number` in `sms_tool/phone/nexsms.py`.

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

pub fn api_phone_number(phone: String) -> Result<String, String> {
    let mut value = py_trim(&phone);
    if let Some(rest) = value.strip_prefix('+') {
        value = rest;
    }
    if let Some(rest) = value.strip_prefix("00") {
        value = rest;
    }
    if value.is_ascii() {
        let mut out = String::with_capacity(value.len());
        for b in value.bytes() {
            if b.is_ascii_digit() {
                out.push(b as char);
            }
        }
        return Ok(out);
    }
    Ok(non_nd_re().replace_all(value, "").into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_international_phone_numbers() {
        assert_eq!(
            api_phone_number("  +233 55-123-4567 ".to_string()).unwrap(),
            "233551234567"
        );
        assert_eq!(
            api_phone_number("0086-138-0013-8000".to_string()).unwrap(),
            "8613800138000"
        );
        assert_eq!(
            api_phone_number("+001 (415) 555-0199".to_string()).unwrap(),
            "14155550199"
        );
    }
}
