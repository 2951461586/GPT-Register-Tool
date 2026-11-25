//! Rust port of `classify_edge_response` in `sms_tool/proxy/edge_probe.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use aho_corasick::{AhoCorasick, AhoCorasickBuilder};
use serde_json::Value;

const BODY_LIMIT: usize = 65536;

/// Pinned to `sms_tool/proxy/edge_probe.py::_CHALLENGE_MARKERS`
/// by `tests/governance/test_wave5_rust_parity.py`.
pub const CHALLENGE_MARKERS: [&str; 7] = [
    "cf-chl",
    "cf_chl",
    "cf-mitigated",
    "challenge-platform",
    "just a moment",
    "attention required",
    "enable javascript and cookies to continue",
];

fn matcher() -> Result<&'static AhoCorasick, String> {
    static AC: OnceLock<Result<AhoCorasick, String>> = OnceLock::new();
    AC.get_or_init(|| {
        AhoCorasickBuilder::new()
            .ascii_case_insensitive(true)
            .build(CHALLENGE_MARKERS)
            .map_err(|e| format!("ValueError: {e}"))
    })
    .as_ref()
    .map_err(Clone::clone)
}

fn py_str_scalar(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        Value::Object(map) if map.len() == 1 => {
            if let Some(Value::String(s)) = map.get("$int") {
                return s.clone();
            }
            if let Some(Value::String(s)) = map.get("$num") {
                return match s.as_str() {
                    "NaN" => "nan".to_string(),
                    "Infinity" => "inf".to_string(),
                    "-Infinity" => "-inf".to_string(),
                    "-0" => "-0.0".to_string(),
                    other => other.to_string(),
                };
            }
            if let Some(Value::String(s)) = map.get("$repr") {
                return s.clone();
            }
            v.to_string()
        }
        _ => v.to_string(),
    }
}

fn body_prefix_has_marker(ac: &AhoCorasick, text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    let slice = if text.len() <= BODY_LIMIT {
        text
    } else {
        match text.char_indices().nth(BODY_LIMIT) {
            Some((byte_idx, _)) => &text[..byte_idx],
            None => text,
        }
    };
    ac.is_match(slice)
}

fn unwrap_pair(item: &Value) -> Option<(&Value, &Value)> {
    match item {
        Value::Array(pair) if pair.len() == 2 => Some((&pair[0], &pair[1])),
        Value::Object(m) if m.len() == 1 => {
            if let Some(Value::Array(pair)) = m.get("$tuple")
                && pair.len() == 2
            {
                return Some((&pair[0], &pair[1]));
            }
            None
        }
        _ => None,
    }
}

fn headers_have_marker(ac: &AhoCorasick, headers: &Value) -> bool {
    match headers {
        Value::Object(map) => {
            if map.len() == 1
                && let Some(Value::Array(items)) = map.get("$tuple")
            {
                let mut combined = String::new();
                for item in items {
                    let Some((k, v)) = unwrap_pair(item) else {
                        return false;
                    };
                    if !combined.is_empty() {
                        combined.push(' ');
                    }
                    combined.push_str(&py_str_scalar(k));
                    combined.push(':');
                    combined.push_str(&py_str_scalar(v));
                }
                return ac.is_match(&combined);
            }
            if map.len() == 1
                && let Some(Value::Array(entries)) = map.get("$dict")
            {
                let mut combined = String::new();
                for entry in entries {
                    let Some((k, v)) = unwrap_pair(entry) else {
                        return false;
                    };
                    if !combined.is_empty() {
                        combined.push(' ');
                    }
                    combined.push_str(&py_str_scalar(k));
                    combined.push(':');
                    combined.push_str(&py_str_scalar(v));
                }
                return ac.is_match(&combined);
            }
            if map.len() == 1
                && (map.contains_key("$set")
                    || map.contains_key("$bytes")
                    || map.contains_key("$repr")
                    || map.contains_key("$int")
                    || map.contains_key("$num")
                    || map.contains_key("$undefined"))
            {
                return false;
            }
            let mut combined = String::new();
            for (k, v) in map {
                if !combined.is_empty() {
                    combined.push(' ');
                }
                combined.push_str(k);
                combined.push(':');
                combined.push_str(&py_str_scalar(v));
            }
            ac.is_match(&combined)
        }
        Value::Array(items) => {
            let mut combined = String::new();
            for item in items {
                let Some((k, v)) = unwrap_pair(item) else {
                    return false;
                };
                if !combined.is_empty() {
                    combined.push(' ');
                }
                combined.push_str(&py_str_scalar(k));
                combined.push(':');
                combined.push_str(&py_str_scalar(v));
            }
            ac.is_match(&combined)
        }
        _ => false,
    }
}

#[inline]
fn is_py_ws(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

fn parse_py_int_str(raw: &str) -> Option<i64> {
    let s = raw.trim_matches(is_py_ws);
    if s.is_empty() {
        return None;
    }
    let (neg, body) = if let Some(r) = s.strip_prefix('-') {
        (true, r)
    } else if let Some(r) = s.strip_prefix('+') {
        (false, r)
    } else {
        (false, s)
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') {
        return None;
    }
    let mut digits = String::with_capacity(body.len());
    let mut prev_us = false;
    for ch in body.chars() {
        if ch == '_' {
            if prev_us {
                return None;
            }
            prev_us = true;
            continue;
        }
        prev_us = false;
        let d = ch.to_digit(10)?;
        digits.push((b'0' + d as u8) as char);
    }
    if digits.is_empty() {
        return None;
    }
    if neg {
        format!("-{digits}").parse::<i64>().ok()
    } else {
        digits.parse::<i64>().ok()
    }
}

fn status_code_to_int(value: &Value) -> Result<i64, String> {
    match value {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i)
            } else if let Some(u) = n.as_u64() {
                Ok(u.min(i64::MAX as u64) as i64)
            } else if let Some(f) = n.as_f64() {
                if f.is_nan() {
                    Ok(0)
                } else if f.is_infinite() {
                    Err("OverflowError: cannot convert float infinity to integer".to_string())
                } else {
                    Ok(f.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64)
                }
            } else {
                Ok(0)
            }
        }
        Value::String(s) => Ok(parse_py_int_str(s).unwrap_or(0)),
        Value::Object(map) if map.len() == 1 => {
            if let Some(Value::String(num_s)) = map.get("$num") {
                return match num_s.as_str() {
                    "Infinity" | "-Infinity" => {
                        Err("OverflowError: cannot convert float infinity to integer".to_string())
                    }
                    _ => Ok(0),
                };
            }
            if let Some(Value::String(int_s)) = map.get("$int") {
                return Ok(if int_s.starts_with('-') {
                    i64::MIN
                } else {
                    i64::MAX
                });
            }
            Ok(0)
        }
        _ => Ok(0),
    }
}

/// Port of `classify_edge_response` (`sms_tool/proxy/edge_probe.py:135`).
pub fn classify_edge_response(
    status_code: serde_json::Value,
    body: Option<String>,
    headers: Option<serde_json::Value>,
) -> Result<String, String> {
    let code = status_code_to_int(&status_code)?;
    if code <= 0 {
        return Ok("dead".to_string());
    }
    if code == 403 {
        return Ok("blocked".to_string());
    }
    if (200..400).contains(&code) {
        return Ok("clean".to_string());
    }
    if code >= 400 {
        let ac = matcher()?;
        let body_hit = body
            .as_deref()
            .is_some_and(|b| body_prefix_has_marker(ac, b));
        let header_hit = !body_hit && headers.as_ref().is_some_and(|h| headers_have_marker(ac, h));
        if body_hit || header_hit {
            return Ok("blocked".to_string());
        }
    }
    if code == 401 || code == 429 {
        return Ok("clean".to_string());
    }
    Ok("degraded".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn classifies_cloudflare_and_clean_responses() {
        assert_eq!(
            classify_edge_response(json!(0), None, None).expect("ok"),
            "dead"
        );
        assert_eq!(
            classify_edge_response(json!("not-a-number"), Some(String::new()), None).expect("ok"),
            "dead"
        );
        assert_eq!(
            classify_edge_response(json!(200), Some("<html>ok</html>".into()), None).expect("ok"),
            "clean"
        );
        assert_eq!(
            classify_edge_response(json!(403), Some(String::new()), None).expect("ok"),
            "blocked"
        );
        assert_eq!(
            classify_edge_response(json!(429), Some("rate limit".into()), None).expect("ok"),
            "clean"
        );
        assert_eq!(
            classify_edge_response(
                json!(429),
                Some("<title>Just a moment...</title>".into()),
                None
            )
            .expect("ok"),
            "blocked"
        );
        assert_eq!(
            classify_edge_response(
                json!(503),
                Some("busy".into()),
                Some(json!({"cf-mitigated": "challenge"}))
            )
            .expect("ok"),
            "blocked"
        );
    }
}
