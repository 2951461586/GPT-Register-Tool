//! Rust port of `_extract_sms_code` from `sms_tool/phone/sms_utils.py`.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

fn keyword_re_1() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)(?:code|otp|verification|verify)[:\s]+(\d{4,6})").expect("valid regex")
    })
}

fn keyword_re_2() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // In Python `re`, `$` without MULTILINE matches `\z` or before a final `\n`.
        Regex::new(r"(?i)(?:is|:)\s*(\d{4,6})\s*(?:for|to|\.|\n?\z)").expect("valid regex")
    })
}

fn unicode_digit_run_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\d+").expect("valid regex"))
}

#[inline]
fn is_ascii_digit_or_hyphen(b: u8) -> bool {
    b.is_ascii_digit() || b == b'-'
}

fn parse_unicode_decimal_int(s: &str) -> Option<u32> {
    let mut acc: u32 = 0;
    for ch in s.chars() {
        let d = if ch.is_ascii_digit() {
            (ch as u32) - ('0' as u32)
        } else {
            // Handle non-ASCII decimal digits if any appear (rare).
            let cp = ch as u32;
            match cp {
                0x0660..=0x0669 => cp - 0x0660,
                0x06F0..=0x06F9 => cp - 0x06F0,
                0xFF10..=0xFF19 => cp - 0xFF10,
                _ => return None,
            }
        };
        acc = acc.checked_mul(10)?.checked_add(d)?;
    }
    Some(acc)
}

/// Exact equivalent of `re.compile(r"(?<![0-9-])(?<!20[0-9]{2})(\d{4,6})(?![0-9-])").search(text)`.
fn search_standalone(text: &str) -> Option<&str> {
    let bytes = text.as_bytes();
    for m in unicode_digit_run_re().find_iter(text) {
        let run = m.as_str();
        let run_start = m.start();
        let char_offsets: Vec<(usize, usize)> = run
            .char_indices()
            .map(|(off, ch)| (run_start + off, run_start + off + ch.len_utf8()))
            .collect();
        let n = char_offsets.len();
        if n < 4 {
            continue;
        }
        // Try leftmost starting character within the digit run.
        for start_i in 0..=n - 4 {
            let start_byte = char_offsets[start_i].0;
            // `(?<![0-9-])`
            if start_byte > 0 && is_ascii_digit_or_hyphen(bytes[start_byte - 1]) {
                continue;
            }
            // `(?<!20[0-9]{2})`
            if start_byte >= 4
                && bytes[start_byte - 4] == b'2'
                && bytes[start_byte - 3] == b'0'
                && bytes[start_byte - 2].is_ascii_digit()
                && bytes[start_byte - 1].is_ascii_digit()
            {
                continue;
            }
            // Greedy `\d{4,6}` tries length 6, then 5, then 4.
            let max_len = (n - start_i).min(6);
            for len in (4..=max_len).rev() {
                let end_byte = char_offsets[start_i + len - 1].1;
                // `(?![0-9-])`
                if end_byte < bytes.len() && is_ascii_digit_or_hyphen(bytes[end_byte]) {
                    continue;
                }
                return Some(&text[start_byte..end_byte]);
            }
        }
    }
    None
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

pub fn _extract_sms_code(text: serde_json::Value) -> Result<Option<String>, String> {
    if is_py_falsy(&text) {
        return Ok(None);
    }
    let Value::String(s) = text else {
        return Err("TypeError: expected string or bytes-like object".to_string());
    };
    if let Some(caps) = keyword_re_1().captures(&s)
        && let Some(m) = caps.get(1)
    {
        return Ok(Some(m.as_str().to_string()));
    }
    if let Some(caps) = keyword_re_2().captures(&s)
        && let Some(m) = caps.get(1)
    {
        return Ok(Some(m.as_str().to_string()));
    }
    if let Some(code) = search_standalone(&s) {
        if code.chars().count() == 4
            && let Some(num) = parse_unicode_decimal_int(code)
            && (2000..=2099).contains(&num)
        {
            return Ok(None);
        }
        return Ok(Some(code.to_string()));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_keyword_and_standalone_codes() {
        assert_eq!(
            _extract_sms_code(json!("Your OpenAI verification code is: 482910.")).unwrap(),
            Some("482910".to_string())
        );
        assert_eq!(
            _extract_sms_code(json!("Code: 123456\n")).unwrap(),
            Some("123456".to_string())
        );
        assert_eq!(
            _extract_sms_code(json!("Event in 2026 only.")).unwrap(),
            None
        );
        assert_eq!(
            _extract_sms_code(json!("Date 2026-11-24 ref 551903 ready")).unwrap(),
            Some("551903".to_string())
        );
        assert_eq!(_extract_sms_code(json!("1234567")).unwrap(), None);
        assert_eq!(_extract_sms_code(json!(null)).unwrap(), None);
    }
}
