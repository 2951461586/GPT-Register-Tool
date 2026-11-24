//! Rust port of `classify_cpa_auth_file` from `sms_tool/importers/cpa.py`.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value};

fn invalid_auth_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"\b401\b|unauthorized|auth_unavailable|authentication token has been invalidated|token has been invalidated|refresh_token_expired|refresh token expired|refresh_token_reused|refresh_token_invalidated|invalid_grant",
        )
        .expect("valid regex")
    })
}

fn banned_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"\bbanned\b|\bsuspended\b|\bdeactivated\b|\bterminated\b|account closed|account_locked|fraud|abuse|违反|封禁",
        )
        .expect("valid regex")
    })
}

fn transient_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"timeout|temporarily|context canceled").expect("valid regex"))
}

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

fn py_str_raw(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        Value::Array(_) | Value::Object(_) => {
            let mut out = String::new();
            py_repr_into(value, &mut out);
            out
        }
    }
}

fn py_str_or_empty(value: Option<&Value>) -> String {
    let Some(v) = value else {
        return String::new();
    };
    match v {
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
        other => py_str_raw(other),
    }
}

fn is_py_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => {
            n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0)
        }
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

fn py_int(value: Option<&Value>) -> i64 {
    let Some(v) = value else {
        return 0;
    };
    match v {
        Value::Null => 0,
        Value::Bool(b) => i64::from(*b),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(u) = n.as_u64() {
                u as i64
            } else if let Some(f) = n.as_f64() {
                f.trunc() as i64
            } else {
                0
            }
        }
        Value::String(s) => py_trim(s).parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

fn first_truthy<'a>(a: Option<&'a Value>, b: Option<&'a Value>) -> Option<&'a Value> {
    if let Some(v) = a
        && !is_py_falsy(v)
    {
        return Some(v);
    }
    if let Some(v) = b
        && !is_py_falsy(v)
    {
        return Some(v);
    }
    None
}

pub fn classify_cpa_auth_file(item: serde_json::Value) -> Result<String, String> {
    let empty_map = Map::new();
    let map: &Map<String, Value> = match &item {
        Value::Object(m) => m,
        other if is_py_falsy(other) => &empty_map,
        _ => return Err("AttributeError: object has no attribute 'get'".to_string()),
    };

    let status_raw = py_str_or_empty(map.get("status"));
    let status = py_trim(&status_raw).to_lowercase();

    let probe_map: Option<&Map<String, Value>> = match map.get("probe") {
        Some(Value::Object(m)) => Some(m),
        _ => None,
    };

    let mut text = String::with_capacity(128);
    text.push_str(&status_raw);
    for key in ["status_message", "message", "error"] {
        text.push(' ');
        text.push_str(&py_str_or_empty(map.get(key)));
    }
    for key in ["status_code", "statusCode", "error", "raw"] {
        text.push(' ');
        if let Some(pm) = probe_map {
            match pm.get(key) {
                Some(v) => text.push_str(&py_str_raw(v)),
                None => text.push_str("None"),
            }
        }
    }
    let text_lower = text.to_lowercase();

    let probe_status = if let Some(pm) = probe_map {
        py_int(first_truthy(pm.get("status_code"), pm.get("statusCode")))
    } else {
        0
    };

    let disabled_true = matches!(map.get("disabled"), Some(Value::Bool(true)));
    let unavailable_true = matches!(map.get("unavailable"), Some(Value::Bool(true)));

    if invalid_auth_re().is_match(&text_lower)
        || banned_re().is_match(&text_lower)
        || probe_status == 401
        || matches!(status.as_str(), "disabled" | "unavailable")
        || disabled_true
        || unavailable_true
    {
        return Ok("token_invalid".to_string());
    }
    if matches!(status.as_str(), "active" | "ok") {
        return Ok("active".to_string());
    }
    if transient_re().is_match(&text_lower) {
        return Ok("transient".to_string());
    }
    Ok(if status.is_empty() {
        "unknown".to_string()
    } else {
        status
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn classifies_cpa_statuses() {
        assert_eq!(
            classify_cpa_auth_file(json!({"status": "active"})).unwrap(),
            "active"
        );
        assert_eq!(
            classify_cpa_auth_file(json!({"status": "ok", "probe": {"status_code": 401}})).unwrap(),
            "token_invalid"
        );
        assert_eq!(
            classify_cpa_auth_file(json!({"status": "err", "message": "context canceled"}))
                .unwrap(),
            "transient"
        );
        assert_eq!(classify_cpa_auth_file(json!(null)).unwrap(), "unknown");
    }
}
