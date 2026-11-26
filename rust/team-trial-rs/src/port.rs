//! Rust port of JWT account-id extraction in `sms_tool/pay_link/team_trial.py`.

use serde_json::Value;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
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
        Value::Null | Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if i == 0 { String::new() } else { i.to_string() }
            } else if let Some(u) = n.as_u64() {
                if u == 0 { String::new() } else { u.to_string() }
            } else if let Some(f) = n.as_f64() {
                if f == 0.0 {
                    String::new()
                } else {
                    n.to_string()
                }
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(arr) => {
            if arr.is_empty() {
                String::new()
            } else {
                let mut out = String::new();
                py_repr_into(value, &mut out);
                out
            }
        }
        Value::Object(obj) => {
            if obj.is_empty() {
                String::new()
            } else {
                let mut out = String::new();
                py_repr_into(value, &mut out);
                out
            }
        }
    }
}

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

pub fn _jwt_chatgpt_account_id(access_token: String) -> Result<String, String> {
    let mut parts = access_token.split('.');
    let _header = parts.next();
    let Some(segment) = parts.next() else {
        return Ok(String::new());
    };
    let Some(decoded) = py_urlsafe_b64decode_ascii(segment) else {
        return Ok(String::new());
    };
    let Ok(text) = std::str::from_utf8(&decoded) else {
        return Ok(String::new());
    };
    let Ok(claims) = serde_json::from_str::<Value>(text) else {
        return Ok(String::new());
    };
    let Some(obj) = claims.as_object() else {
        return Ok(String::new());
    };
    let Some(auth) = obj
        .get("https://api.openai.com/auth")
        .and_then(|v| v.as_object())
    else {
        return Ok(String::new());
    };
    let Some(aid_val) = auth.get("chatgpt_account_id") else {
        return Ok(String::new());
    };
    let aid = py_str_or_empty(aid_val)
        .trim_matches(py_is_space)
        .to_string();
    if !aid.is_empty() && aid != "default" {
        Ok(aid)
    } else {
        Ok(String::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_non_default_account_id() {
        let tok = "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdC0xMjMifX0.sig";
        assert_eq!(
            _jwt_chatgpt_account_id(tok.to_string()).unwrap(),
            "acct-123"
        );
        let def_tok = "a.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiZGVmYXVsdCJ9fQ.c";
        assert_eq!(_jwt_chatgpt_account_id(def_tok.to_string()).unwrap(), "");
        assert_eq!(
            _jwt_chatgpt_account_id("not-a-jwt".to_string()).unwrap(),
            ""
        );
    }
}
