//! Rust port of `_hash_public_operation_id` from `sms_tool/operation_journal.py`.

#![forbid(unsafe_code)]

use sha2::{Digest, Sha256};

const HEX: &[u8; 16] = b"0123456789abcdef";

#[inline]
fn py_is_space(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{001c}'..='\u{001f}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

pub fn _hash_public_operation_id(raw: Option<String>) -> Result<String, String> {
    let owned = raw.unwrap_or_default();
    let text = py_trim(&owned);
    if text.len() == 32 && text.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Ok(text.to_ascii_lowercase());
    }
    let digest = Sha256::digest(text.as_bytes());
    let mut out = String::with_capacity(27);
    out.push_str("op_");
    for &byte in &digest[..12] {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_32_hex_and_hashes_opaque_inputs() {
        assert_eq!(
            _hash_public_operation_id(Some("  0123456789ABCDEF0123456789abcdef ".into())),
            Ok("0123456789abcdef0123456789abcdef".into())
        );
        let hashed = _hash_public_operation_id(Some("user@example.com".into())).unwrap();
        assert!(hashed.starts_with("op_"));
        assert_eq!(hashed.len(), 27);
    }
}
