//! Rust port of JWT plan-type extraction in `sms_tool/accounts/account_scan.py`.

use serde_json::Value;

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

fn is_py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                true
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

pub fn _jwt_plan_type(token: String) -> Result<serde_json::Value, String> {
    let raw = token.as_str();
    let mut parts = raw.split('.');
    let _header = parts.next();
    let Some(segment) = parts.next() else {
        return Ok(Value::String(String::new()));
    };
    if segment.is_empty() {
        return Ok(Value::String(String::new()));
    }
    let Some(decoded) = py_urlsafe_b64decode_ascii(segment) else {
        return Ok(Value::String(String::new()));
    };
    let Ok(body) = serde_json::from_slice::<Value>(&decoded) else {
        return Ok(Value::String(String::new()));
    };
    let Some(auth) = body
        .as_object()
        .and_then(|m| m.get("https://api.openai.com/auth"))
        .and_then(|v| v.as_object())
    else {
        return Ok(Value::String(String::new()));
    };
    if let Some(v) = auth.get("chatgpt_plan_type")
        && is_py_truthy(v)
    {
        return Ok(v.clone());
    }
    if let Some(v) = auth.get("plan_type")
        && is_py_truthy(v)
    {
        return Ok(v.clone());
    }
    Ok(Value::String(String::new()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_chatgpt_plan_type_or_fallback() {
        // {"https://api.openai.com/auth":{"chatgpt_plan_type":"plus"}}
        let tok =
            "a.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9wbGFuX3R5cGUiOiJwbHVzIn19.c";
        assert_eq!(_jwt_plan_type(tok.to_string()).unwrap(), json!("plus"));
        assert_eq!(_jwt_plan_type(String::new()).unwrap(), json!(""));
        assert_eq!(_jwt_plan_type("bad".to_string()).unwrap(), json!(""));
    }
}
