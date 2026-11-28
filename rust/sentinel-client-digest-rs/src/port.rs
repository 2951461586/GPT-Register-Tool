//! Rust port of `_secret_digest` from `sms_tool/sentinel/client.py`.

#![forbid(unsafe_code)]

use sha2::{Digest, Sha256};

const HEX: &[u8; 16] = b"0123456789abcdef";

pub fn _secret_digest(value: Option<String>) -> Result<String, String> {
    let text = value.unwrap_or_default();
    if text.is_empty() {
        return Ok(String::new());
    }
    let digest = Sha256::digest(text.as_bytes());
    let mut out = String::with_capacity(64);
    for &byte in &digest[..] {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashes_non_empty_secrets_to_64_hex_chars() {
        assert_eq!(_secret_digest(None), Ok(String::new()));
        assert_eq!(_secret_digest(Some(String::new())), Ok(String::new()));
        assert_eq!(
            _secret_digest(Some("abc".into())),
            Ok("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".into())
        );
    }
}
