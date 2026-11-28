//! Rust port of `_parse_token_claims_and_hash` from `sms_tool/observability/token_telemetry.py`.

#![forbid(unsafe_code)]

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use serde_json::Value;
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

fn py_urlsafe_b64decode(seg: &str) -> Option<Vec<u8>> {
    if !seg.is_ascii() {
        return None;
    }
    let pad = (4 - (seg.len() % 4)) % 4;
    let mut cleaned = Vec::with_capacity(seg.len() + pad);
    let mut saw_pad = false;
    let mut pad_count = 0usize;
    for b in seg.bytes().chain(std::iter::repeat_n(b'=', pad)) {
        let mapped = match b {
            b'-' => b'+',
            b'_' => b'/',
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'+' | b'/' => b,
            b'=' => {
                saw_pad = true;
                pad_count += 1;
                cleaned.push(b'=');
                if cleaned.len() % 4 == 0 && pad_count >= 1 {
                    break;
                }
                continue;
            }
            _ => continue,
        };
        if saw_pad {
            continue;
        }
        cleaned.push(mapped);
    }
    B64.decode(&cleaned).ok()
}

fn py_parse_int_str(raw: &str) -> i64 {
    let trimmed = py_trim(raw);
    if trimmed.is_empty() {
        return 0;
    }
    let (neg, rest) = if let Some(r) = trimmed.strip_prefix('-') {
        (true, r)
    } else if let Some(r) = trimmed.strip_prefix('+') {
        (false, r)
    } else {
        (false, trimmed)
    };
    if rest.is_empty() || rest.starts_with('_') || rest.ends_with('_') {
        return 0;
    }
    let mut digits = String::with_capacity(rest.len() + 1);
    if neg {
        digits.push('-');
    }
    let mut prev_underscore = false;
    for b in rest.bytes() {
        if b.is_ascii_digit() {
            digits.push(b as char);
            prev_underscore = false;
        } else if b == b'_' && !prev_underscore {
            prev_underscore = true;
        } else {
            return 0;
        }
    }
    digits.parse::<i64>().unwrap_or(0)
}

fn json_as_int(value: Option<&Value>) -> i64 {
    let Some(val) = value else {
        return 0;
    };
    match val {
        Value::Bool(b) => i64::from(*b),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(f) = n.as_f64() {
                if f.is_finite() { f.trunc() as i64 } else { 0 }
            } else {
                0
            }
        }
        Value::String(s) => py_parse_int_str(s),
        _ => 0,
    }
}

pub fn _parse_token_claims_and_hash(token: Option<String>) -> Result<(String, i64, i64), String> {
    let owned = token.unwrap_or_default();
    let text = py_trim(&owned);
    if text.is_empty() {
        return Ok((String::new(), 0, 0));
    }
    let digest = Sha256::digest(text.as_bytes());
    let mut token_hash = String::with_capacity(16);
    for &byte in &digest[..8] {
        token_hash.push(HEX[(byte >> 4) as usize] as char);
        token_hash.push(HEX[(byte & 0x0f) as usize] as char);
    }

    let mut iat = 0i64;
    let mut exp = 0i64;
    if let Some((_, after_first)) = text.split_once('.') {
        let payload_seg = after_first.split('.').next().unwrap_or("");
        if let Some(decoded) = py_urlsafe_b64decode(payload_seg)
            && let Ok(Value::Object(map)) = serde_json::from_slice::<Value>(&decoded)
        {
            iat = json_as_int(map.get("iat"));
            exp = json_as_int(map.get("exp"));
        }
    }
    Ok((token_hash, iat, exp))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_jwt_claims_and_hashes_token() {
        // {"iat": 1700000000, "exp": 1700003600}
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3MDAwMDAwMDAsImV4cCI6MTcwMDAwMzYwMH0.sig";
        let (h, iat, exp) = _parse_token_claims_and_hash(Some(jwt.into())).unwrap();
        assert_eq!(h.len(), 16);
        assert_eq!(iat, 1_700_000_000);
        assert_eq!(exp, 1_700_003_600);
        assert_eq!(
            _parse_token_claims_and_hash(None).unwrap(),
            (String::new(), 0, 0)
        );
    }
}
