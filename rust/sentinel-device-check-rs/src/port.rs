//! Pure Rust port of `_validate_sentinel_device_payload` from `sms_tool/registration/sentinel_tokens.py`.

use serde_json::Value;

fn extract_token_id(raw: &str) -> Result<String, ()> {
    let parsed: Value = serde_json::from_str(raw).map_err(|_| ())?;
    let Some(map) = parsed.as_object() else {
        return Err(());
    };
    let id_str = match map.get("id") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => {
            if n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Some(Value::Array(a)) if a.is_empty() => String::new(),
        Some(Value::Object(o)) if o.is_empty() => String::new(),
        Some(other) => other.to_string().trim().to_owned(),
    };
    Ok(id_str)
}

fn find_cookie_oai_did(cookie_header: &str) -> Option<&str> {
    const PREFIX: &[u8] = b"oai-did=";
    let bytes = cookie_header.as_bytes();
    let mut prev_is_delim = true;
    for (idx, ch) in cookie_header.char_indices() {
        if prev_is_delim && bytes.len() >= idx + PREFIX.len() {
            let slice = &bytes[idx..idx + PREFIX.len()];
            if slice.eq_ignore_ascii_case(PREFIX) {
                let val_start = idx + PREFIX.len();
                let tail = &cookie_header[val_start..];
                let val_len = tail
                    .char_indices()
                    .find(|&(_, c)| c == ';' || c.is_whitespace())
                    .map_or(tail.len(), |(pos, _)| pos);
                if val_len > 0 {
                    return Some(&tail[..val_len]);
                }
            }
        }
        prev_is_delim = ch == ';' || ch.is_whitespace();
    }
    None
}

pub fn _validate_sentinel_device_payload(
    expected: String,
    actual: String,
    sentinel_token: String,
    sentinel_oauth_token: String,
    cookie_header: String,
) -> String {
    if !expected.is_empty() && !actual.is_empty() && expected != actual {
        return "sentinel_extract_failed: oai-did does not match Sentinel device id".to_owned();
    }
    let resolved_actual = if !expected.is_empty() && actual.is_empty() {
        expected.as_str()
    } else {
        actual.as_str()
    };
    for (key, raw) in [
        ("sentinel_token", sentinel_token.as_str()),
        ("sentinel_oauth_token", sentinel_oauth_token.as_str()),
    ] {
        if raw.is_empty() {
            continue;
        }
        let Ok(token_id) = extract_token_id(raw) else {
            return format!("sentinel_extract_failed: invalid {key}");
        };
        if !expected.is_empty() && token_id != expected {
            return format!("sentinel_extract_failed: {key} id does not match oai-did");
        }
    }
    if !expected.is_empty() && resolved_actual != expected {
        return "sentinel_extract_failed: missing Sentinel device id".to_owned();
    }
    if !expected.is_empty()
        && !cookie_header.is_empty()
        && let Some(found) = find_cookie_oai_did(&cookie_header)
        && found.trim() != expected
    {
        return "sentinel_extract_failed: cookie oai-did does not match Sentinel device id"
            .to_owned();
    }
    String::new()
}
