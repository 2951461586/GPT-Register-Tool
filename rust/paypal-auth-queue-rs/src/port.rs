//! Rust port of `_account_ref` from `sms_tool/paypal/authorization_queue.py`.

#![forbid(unsafe_code)]

use sha2::{Digest, Sha256};

const HEX: &[u8; 16] = b"0123456789abcdef";

pub fn _account_ref(email: Option<String>) -> Result<String, String> {
    let raw = email.unwrap_or_default();
    let digest = Sha256::digest(raw.as_bytes());
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
    fn hashes_first_16_hex_chars() {
        assert_eq!(_account_ref(None), Ok("e3b0c44298fc1c14".into()));
        assert_eq!(
            _account_ref(Some("user@example.com".into())),
            Ok("b4c9a289323b21a0".into())
        );
    }
}
