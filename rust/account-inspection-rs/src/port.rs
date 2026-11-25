//! Rust port of `decode_jwt_exp` from `sms_tool/accounts/account_inspection.py`.

#![forbid(unsafe_code)]

use serde_json::Value;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn py_str_repr(s: &str, out: &mut String) {
    let use_double = s.contains('\'') && !s.contains('"');
    let quote = if use_double { '"' } else { '\'' };
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\'' if !use_double => out.push_str("\\'"),
            '"' if use_double => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                use std::fmt::Write;
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
}

fn py_repr_into(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("None"),
        Value::Bool(true) => out.push_str("True"),
        Value::Bool(false) => out.push_str("False"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => py_str_repr(s, out),
        Value::Array(arr) => {
            out.push('[');
            for (idx, item) in arr.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_repr_into(item, out);
            }
            out.push(']');
        }
        Value::Object(obj) => {
            out.push('{');
            for (idx, (k, v)) in obj.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_str_repr(k, out);
                out.push_str(": ");
                py_repr_into(v, out);
            }
            out.push('}');
        }
    }
}

fn py_str_or_empty(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Number(n) => {
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(a) if a.is_empty() => String::new(),
        Value::Object(o) if o.is_empty() => String::new(),
        other => {
            let mut out = String::new();
            py_repr_into(other, &mut out);
            out
        }
    }
}

#[inline]
fn b64url_val(b: u8) -> Option<u8> {
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
    let bytes = segment.as_bytes();
    let mut vals: Vec<u8> = Vec::with_capacity(bytes.len());
    for &b in bytes {
        if b == b'=' {
            break;
        }
        if let Some(v) = b64url_val(b) {
            vals.push(v);
        }
    }
    if vals.len() % 4 == 1 {
        return None;
    }
    let mut out = Vec::with_capacity((vals.len() * 3) / 4);
    let mut i = 0;
    while i + 4 <= vals.len() {
        let n = ((vals[i] as u32) << 18)
            | ((vals[i + 1] as u32) << 12)
            | ((vals[i + 2] as u32) << 6)
            | (vals[i + 3] as u32);
        out.push(((n >> 16) & 0xff) as u8);
        out.push(((n >> 8) & 0xff) as u8);
        out.push((n & 0xff) as u8);
        i += 4;
    }
    let rem = vals.len() - i;
    if rem == 2 {
        let n = ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12);
        out.push(((n >> 16) & 0xff) as u8);
    } else if rem == 3 {
        let n =
            ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12) | ((vals[i + 2] as u32) << 6);
        out.push(((n >> 16) & 0xff) as u8);
        out.push(((n >> 8) & 0xff) as u8);
    }
    Some(out)
}

pub fn decode_jwt_exp(token: serde_json::Value) -> Result<Option<f64>, String> {
    let owned = py_str_or_empty(&token);
    let mut raw = py_trim(&owned);
    if raw.len() >= 7 && raw.as_bytes()[..7].eq_ignore_ascii_case(b"bearer ") {
        raw = py_trim(&raw[7..]);
    }
    if raw.is_empty() {
        return Ok(None);
    }
    let mut parts = raw.split('.');
    let Some(_header) = parts.next() else {
        return Ok(None);
    };
    let Some(segment) = parts.next() else {
        return Ok(None);
    };
    let Some(_sig) = parts.next() else {
        return Ok(None);
    };
    if parts.next().is_some() || segment.is_empty() {
        return Ok(None);
    }
    let Some(decoded) = py_urlsafe_b64decode_ascii(segment) else {
        return Ok(None);
    };
    let Ok(json_str) = std::str::from_utf8(&decoded) else {
        return Ok(None);
    };
    let Ok(Value::Object(payload)) = serde_json::from_str::<Value>(json_str) else {
        return Ok(None);
    };
    let Some(Value::Number(exp_num)) = payload.get("exp") else {
        return Ok(None);
    };
    let Some(exp_float) = exp_num.as_f64() else {
        return Ok(None);
    };
    if !exp_float.is_finite() {
        return Ok(None);
    }
    Ok(Some(exp_float))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decodes_valid_jwt_exp_and_rejects_malformed() {
        // {"sub":"usr_1","exp":1893456000}
        let tok =
            "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c3JfMSIsImV4cCI6MTg5MzQ1NjAwMH0.sig_part";
        assert_eq!(decode_jwt_exp(json!(tok)).unwrap(), Some(1_893_456_000.0));
        // {"exp":true} -> must reject bool!
        let bool_tok = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOnRydWV9.sig";
        assert_eq!(decode_jwt_exp(json!(bool_tok)).unwrap(), None);
        assert_eq!(decode_jwt_exp(json!("")).unwrap(), None);
        assert_eq!(decode_jwt_exp(json!("a.b")).unwrap(), None);
    }
}
