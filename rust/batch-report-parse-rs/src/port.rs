//! Rust port of `safe_batch_id` and `account_ref` from
//! `sms_tool/payment/batch_report.py`.

#![forbid(unsafe_code)]

use sha2::{Digest, Sha256};

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

pub fn safe_batch_id(value: Option<String>) -> Result<String, String> {
    let raw = value.unwrap_or_default();
    let trimmed = py_trim(&raw);
    let mut out = String::with_capacity(trimmed.len().min(80));
    let mut in_bad = false;
    for ch in trimmed.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-') {
            out.push(ch);
            in_bad = false;
        } else if !in_bad {
            out.push('_');
            in_bad = true;
        }
        if out.len() >= 80 {
            break;
        }
    }
    Ok(out)
}

pub fn account_ref(email: Option<String>) -> Result<String, String> {
    let raw = email.unwrap_or_default();
    let trimmed = py_trim(&raw);
    let norm = if trimmed.is_ascii() {
        trimmed.to_ascii_lowercase()
    } else {
        trimmed.to_lowercase()
    };
    let digest = Sha256::digest(norm.as_bytes());
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(16);
    for &byte in &digest[..8] {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batch_report_helpers_match_python() {
        assert_eq!(
            safe_batch_id(Some("  batch 2026/11/28 #1  ".into())).unwrap(),
            "batch_2026_11_28_1"
        );
        assert_eq!(safe_batch_id(Some("a".repeat(120))).unwrap().len(), 80);
        assert_eq!(
            account_ref(Some("  USER@Example.com ".into())).unwrap(),
            account_ref(Some("user@example.com".into())).unwrap()
        );
        assert_eq!(account_ref(None).unwrap().len(), 16);
    }
}
