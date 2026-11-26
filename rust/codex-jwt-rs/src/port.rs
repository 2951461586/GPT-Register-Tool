//! Rust port of JWT payload decoding in `sms_tool/codex/export.py`.

use serde_json::{Map, Value};

#[inline]
fn b64_val(b: u8) -> Option<u8> {
    match b {
        b'A'..=b'Z' => Some(b - b'A'),
        b'a'..=b'z' => Some(b - b'a' + 26),
        b'0'..=b'9' => Some(b - b'0' + 52),
        b'+' | b'-' => Some(62),
        b'/' | b'_' => Some(63),
        _ => None,
    }
}

fn py_urlsafe_b64decode_ascii(segment: &str) -> Option<Vec<u8>> {
    if !segment.is_ascii() {
        return None;
    }
    let pad_len = (4 - (segment.len() % 4)) % 4;
    let mut buf: Vec<u8> = Vec::with_capacity(segment.len() + pad_len);
    buf.extend_from_slice(segment.as_bytes());
    buf.resize(segment.len() + pad_len, b'=');

    let mut out = Vec::with_capacity(buf.len() * 3 / 4);
    let mut quad = [0u8; 4];
    let mut qlen = 0usize;
    let mut eq_seen = 0usize;

    for &b in &buf {
        if b == b'=' {
            if qlen >= 2 {
                eq_seen += 1;
                if qlen + eq_seen >= 4 {
                    break;
                }
            }
            continue;
        }
        if eq_seen > 0 {
            continue;
        }
        if let Some(v) = b64_val(b) {
            quad[qlen] = v;
            qlen += 1;
            if qlen == 4 {
                out.push((quad[0] << 2) | (quad[1] >> 4));
                out.push((quad[1] << 4) | (quad[2] >> 2));
                out.push((quad[2] << 6) | quad[3]);
                qlen = 0;
            }
        }
    }

    match (qlen, eq_seen) {
        (0, _) => Some(out),
        (2, e) if e >= 2 => {
            out.push((quad[0] << 2) | (quad[1] >> 4));
            Some(out)
        }
        (3, e) if e >= 1 => {
            out.push((quad[0] << 2) | (quad[1] >> 4));
            out.push((quad[1] << 4) | (quad[2] >> 2));
            Some(out)
        }
        _ => None,
    }
}

pub fn _jwt_claims(token: String) -> Result<serde_json::Value, String> {
    let mut parts = token.split('.');
    let _header = parts.next();
    let Some(segment) = parts.next() else {
        return Ok(Value::Object(Map::new()));
    };
    let Some(decoded) = py_urlsafe_b64decode_ascii(segment) else {
        return Ok(Value::Object(Map::new()));
    };
    let Ok(text) = std::str::from_utf8(&decoded) else {
        return Ok(Value::Object(Map::new()));
    };
    match serde_json::from_str::<Value>(text) {
        Ok(val) => Ok(val),
        Err(_) => Ok(Value::Object(Map::new())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decodes_valid_jwt_claims_and_handles_invalid() {
        let tok = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1MSIsImV4cCI6MTkwMDAwMDAwMH0.sig";
        assert_eq!(
            _jwt_claims(tok.to_string()).unwrap(),
            json!({"sub": "u1", "exp": 1900000000})
        );
        assert_eq!(_jwt_claims("invalid".to_string()).unwrap(), json!({}));
    }
}
