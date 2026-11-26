//! Rust port of DOM field value matching in `sms_tool/paypal/dom_fields.py`.

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

fn non_digit_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\D+").expect("valid regex"))
}

fn strip_non_digits(s: &str) -> String {
    if s.is_ascii() {
        let mut out = String::with_capacity(s.len());
        for b in s.bytes() {
            if b.is_ascii_digit() {
                out.push(b as char);
            }
        }
        return out;
    }
    non_digit_re().replace_all(s, "").into_owned()
}

pub fn _value_matches(actual: String, expected: String) -> Result<bool, String> {
    let actual_s = py_trim(&actual);
    let expected_s = py_trim(&expected);
    if expected_s.is_empty() || actual_s == expected_s {
        return Ok(true);
    }
    let actual_digits = strip_non_digits(actual_s);
    let expected_digits = strip_non_digits(expected_s);
    if !expected_digits.is_empty() && actual_digits.ends_with(&expected_digits) {
        return Ok(true);
    }
    Ok(actual_s.to_lowercase().contains(&expected_s.to_lowercase()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_exact_digit_suffix_and_case_insensitive_substring() {
        assert!(_value_matches("+1 (415) 555-0199".to_string(), "4155550199".to_string()).unwrap());
        assert!(_value_matches("Alice Smith".to_string(), "alice".to_string()).unwrap());
        assert!(_value_matches("anything".to_string(), "  ".to_string()).unwrap());
        assert!(!_value_matches("1234".to_string(), "5678".to_string()).unwrap());
    }
}
