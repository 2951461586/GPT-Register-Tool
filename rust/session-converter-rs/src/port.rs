//! Rust port of pure JSON/JWT helpers in `sms_tool/session/converter.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};

fn is_tagged_non_dict(map: &Map<String, Value>) -> bool {
    map.len() == 1
        && map.keys().next().is_some_and(|k| {
            matches!(
                k.as_str(),
                "$num" | "$int" | "$tuple" | "$set" | "$bytes" | "$dict"
            )
        })
}

fn is_plain_dict(v: &Value) -> bool {
    matches!(v, Value::Object(m) if !is_tagged_non_dict(m))
}

fn is_py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            n.as_i64().is_none_or(|i| i != 0)
                && n.as_u64().is_none_or(|u| u != 0)
                && n.as_f64().is_none_or(|f| f != 0.0)
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => {
            if m.len() == 1
                && let Some(Value::String(tag)) = m.get("$num")
            {
                return tag != "-0";
            }
            if m.len() == 1
                && let Some(Value::Array(items)) = m
                    .get("$tuple")
                    .or_else(|| m.get("$set"))
                    .or_else(|| m.get("$bytes"))
            {
                return !items.is_empty();
            }
            !m.is_empty()
        }
    }
}

fn py_scalar_str(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::Bool(true) => Some("True".to_string()),
        Value::Bool(false) => Some("False".to_string()),
        Value::Number(n) => Some(n.to_string()),
        Value::String(s) => Some(s.clone()),
        Value::Array(_) => None,
        Value::Object(m) => {
            if m.len() == 1 {
                if let Some(Value::String(tag)) = m.get("$num") {
                    return Some(match tag.as_str() {
                        "NaN" => "nan".to_string(),
                        "Infinity" => "inf".to_string(),
                        "-Infinity" => "-inf".to_string(),
                        "-0" => "-0.0".to_string(),
                        other => other.to_string(),
                    });
                }
                if let Some(Value::String(i)) = m.get("$int") {
                    return Some(i.clone());
                }
            }
            None
        }
    }
}

fn b64url_val(b: u8) -> Option<u8> {
    match b {
        b'A'..=b'Z' => Some(b - b'A'),
        b'a'..=b'z' => Some(b - b'a' + 26),
        b'0'..=b'9' => Some(b - b'0' + 52),
        b'-' => Some(62),
        b'_' => Some(63),
        _ => None,
    }
}

fn decode_urlsafe_b64(input: &str) -> Option<Vec<u8>> {
    if !input.is_ascii() {
        return None;
    }
    let mut vals: Vec<u8> = Vec::with_capacity(input.len());
    for &b in input.as_bytes() {
        if b == b'=' {
            break;
        }
        if let Some(v) = b64url_val(b) {
            vals.push(v);
        }
    }
    let rem = vals.len() % 4;
    if rem == 1 {
        return None;
    }
    let mut out = Vec::with_capacity((vals.len() * 3) / 4);
    let mut i = 0;
    while i + 4 <= vals.len() {
        let n = ((vals[i] as u32) << 18)
            | ((vals[i + 1] as u32) << 12)
            | ((vals[i + 2] as u32) << 6)
            | (vals[i + 3] as u32);
        out.push((n >> 16) as u8);
        out.push(((n >> 8) & 0xff) as u8);
        out.push((n & 0xff) as u8);
        i += 4;
    }
    if rem == 2 {
        let n = ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12);
        out.push((n >> 16) as u8);
    } else if rem == 3 {
        let n =
            ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12) | ((vals[i + 2] as u32) << 6);
        out.push((n >> 16) as u8);
        out.push(((n >> 8) & 0xff) as u8);
    }
    Some(out)
}

pub fn parse_jwt_payload(token: serde_json::Value) -> Result<serde_json::Value, String> {
    if !is_py_truthy(&token) {
        return Ok(Value::Object(Map::new()));
    }
    let Value::String(text) = &token else {
        return Ok(Value::Object(Map::new()));
    };
    let mut split = text.split('.');
    let _header = split.next();
    let Some(payload_b64) = split.next() else {
        return Ok(Value::Object(Map::new()));
    };
    let Some(raw_bytes) = decode_urlsafe_b64(payload_b64) else {
        return Ok(Value::Object(Map::new()));
    };
    let Ok(json_str) = std::str::from_utf8(&raw_bytes) else {
        return Ok(Value::Object(Map::new()));
    };
    match serde_json::from_str::<Value>(json_str) {
        Ok(Value::Object(map)) => Ok(Value::Object(map)),
        _ => Ok(Value::Object(Map::new())),
    }
}

fn nested_val<'a>(data: &'a Map<String, Value>, path: &[&str]) -> Option<&'a Value> {
    let mut current: Option<&Value> = None;
    for (idx, key) in path.iter().enumerate() {
        current = if idx == 0 {
            data.get(*key)
        } else {
            match current {
                Some(Value::Object(m)) if !is_tagged_non_dict(m) => m.get(*key),
                _ => return None,
            }
        };
    }
    current
}

fn has_non_empty(val: Option<&Value>) -> bool {
    let Some(v) = val else {
        return false;
    };
    match v {
        Value::String(s) => !s.trim().is_empty(),
        other => py_scalar_str(other).is_some_and(|s| !s.trim().is_empty()),
    }
}

const TOKEN_PATHS: &[&[&str]] = &[
    &["accessToken"],
    &["access_token"],
    &["tokens", "accessToken"],
    &["tokens", "access_token"],
    &["token", "accessToken"],
    &["token", "access_token"],
    &["credentials", "accessToken"],
    &["credentials", "access_token"],
    &["auth_session", "accessToken"],
    &["auth_session", "access_token"],
    &["auth_session", "session", "accessToken"],
    &["auth_session", "session", "access_token"],
];

const IDENTITY_PATHS: &[&[&str]] = &[
    &["email"],
    &["name"],
    &["label"],
    &["meta", "label"],
    &["tokens", "accountId"],
    &["tokens", "account_id"],
    &["tokens", "chatgptAccountId"],
    &["tokens", "chatgpt_account_id"],
    &["credentials", "accountId"],
    &["credentials", "account_id"],
    &["credentials", "chatgptAccountId"],
    &["credentials", "chatgpt_account_id"],
    &["credentials", "email"],
    &["providerSpecificData", "chatgptAccountId"],
    &["providerSpecificData", "chatgpt_account_id"],
    &["account_id"],
    &["chatgpt_account_id"],
    &["account", "id"],
    &["id"],
];

fn visit_sessions(item: &Value, path: &str, source_name: &Value, found: &mut Vec<Value>) {
    match item {
        Value::Object(map) if !is_tagged_non_dict(map) => {
            let has_token = TOKEN_PATHS
                .iter()
                .any(|p| has_non_empty(nested_val(map, p)));
            let has_identity = map.get("user").is_some_and(is_plain_dict)
                || IDENTITY_PATHS
                    .iter()
                    .any(|p| has_non_empty(nested_val(map, p)));
            if has_token && has_identity {
                let mut entry = Map::with_capacity(3);
                entry.insert("value".to_string(), item.clone());
                entry.insert("sourceName".to_string(), source_name.clone());
                entry.insert("path".to_string(), Value::String(path.to_string()));
                found.push(Value::Object(entry));
                return;
            }
            for (key, child) in map {
                if matches!(
                    key.as_str(),
                    "accessToken" | "access_token" | "sessionToken" | "session_token"
                ) {
                    continue;
                }
                let next_path = format!("{path}.{key}");
                visit_sessions(child, &next_path, source_name, found);
            }
        }
        Value::Array(arr) => {
            for (idx, child) in arr.iter().enumerate() {
                let next_path = format!("{path}[{idx}]");
                visit_sessions(child, &next_path, source_name, found);
            }
        }
        _ => {}
    }
}

pub fn collect_session_like_objects(
    value: serde_json::Value,
    source_name: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let src = source_name.unwrap_or_else(|| Value::String("pasted-json".to_string()));
    let mut found = Vec::new();
    visit_sessions(&value, "$", &src, &mut found);
    Ok(Value::Array(found))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn jwt_strip_and_collect_cases() {
        let tok =
            "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20iLCJleHAiOjE3MDAwMDAwMDB9.";
        let parsed = parse_jwt_payload(json!(tok)).unwrap();
        assert_eq!(parsed["email"], "user@example.com");
        assert_eq!(parse_jwt_payload(json!("not-a-jwt")).unwrap(), json!({}));

        let collected = collect_session_like_objects(
            json!({"accounts": [{"email": "a@b.com", "access_token": "tok1"}]}),
            None,
        )
        .unwrap();
        assert_eq!(collected.as_array().unwrap().len(), 1);
        assert_eq!(collected[0]["path"], "$.accounts[0]");
    }
}
