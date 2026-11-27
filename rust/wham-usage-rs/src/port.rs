//! Rust port of `parse_wham_usage` in `sms_tool/accounts/wham_usage.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Number, Value};

const WINDOW_KEYS: [&str; 2] = ["5h", "7d"];
const USED_KEYS: [&str; 4] = ["used", "num_tokens_used", "tokens_used", "consumed"];
const LIMIT_KEYS: [&str; 5] = ["limit", "num_tokens_limit", "tokens_limit", "max", "cap"];
const REMAINING_KEYS: [&str; 4] = [
    "remaining",
    "num_tokens_remaining",
    "tokens_remaining",
    "available",
];
const RESET_KEYS: [&str; 4] = ["resets_at", "reset_at", "reset_time", "expires_at"];

fn window_alternatives(window_key: &str) -> &'static [&'static str] {
    match window_key {
        "5h" => &["5h", "300min", "five_hours", "short"],
        "7d" => &["7d", "10080min", "seven_days", "weekly", "long"],
        _ => &[],
    }
}

fn py_int(val: &Value) -> Option<i64> {
    match val {
        Value::Bool(b) => Some(if *b { 1 } else { 0 }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Some(i)
            } else if let Some(u) = n.as_u64() {
                i64::try_from(u).ok()
            } else if let Some(f) = n.as_f64() {
                if f.is_finite() {
                    Some(f.trunc() as i64)
                } else {
                    None
                }
            } else {
                None
            }
        }
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn py_str_scalar(val: &Value) -> String {
    match val {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

fn pick_int(window: &Map<String, Value>, keys: &[&str]) -> Option<i64> {
    for &key in keys {
        if let Some(val) = window.get(key)
            && !val.is_null()
            && let Some(parsed) = py_int(val)
        {
            return Some(parsed);
        }
    }
    None
}

fn parse_usage_window(body: &Map<String, Value>, window_key: &str) -> Option<Map<String, Value>> {
    let mut containers: Vec<&Map<String, Value>> = Vec::with_capacity(5);
    for key in ["usage", "rate_limits", "limits", "rate_limits_info"] {
        if let Some(Value::Object(section)) = body.get(key) {
            containers.push(section);
        }
    }
    containers.push(body);

    let alts = window_alternatives(window_key);
    for container in containers {
        let mut found: Option<&Map<String, Value>> = None;
        for &alt in alts {
            if let Some(Value::Object(win)) = container.get(alt) {
                found = Some(win);
                break;
            }
        }
        let Some(window_data) = found else {
            continue;
        };
        let mut used = pick_int(window_data, &USED_KEYS);
        let limit = pick_int(window_data, &LIMIT_KEYS);
        let mut remaining = pick_int(window_data, &REMAINING_KEYS);

        if remaining.is_none()
            && let (Some(u), Some(l)) = (used, limit)
        {
            remaining = Some((l - u).max(0));
        }
        if used.is_none()
            && let (Some(r), Some(l)) = (remaining, limit)
        {
            used = Some((l - r).max(0));
        }
        if used.is_some() || limit.is_some() || remaining.is_some() {
            let u = used.unwrap_or(0);
            let l = limit.unwrap_or(0);
            let r = remaining.unwrap_or(0);
            let percent = if l != 0 {
                let raw = (u as f64) * 100.0 / (l as f64);
                let rounded = (raw * 10.0).round_ties_even() / 10.0;
                if rounded == 0.0 { 0.0 } else { rounded }
            } else {
                0.0
            };
            let mut out = Map::new();
            out.insert("used".to_string(), Value::Number(Number::from(u)));
            out.insert("limit".to_string(), Value::Number(Number::from(l)));
            out.insert("remaining".to_string(), Value::Number(Number::from(r)));
            out.insert(
                "percent".to_string(),
                Number::from_f64(percent)
                    .map(Value::Number)
                    .unwrap_or(Value::Null),
            );
            return Some(out);
        }
    }
    None
}

pub fn parse_wham_usage(body: serde_json::Value) -> Result<Option<serde_json::Value>, String> {
    let owned: Value = match body {
        Value::String(s) => match serde_json::from_str::<Value>(&s) {
            Ok(v) => v,
            Err(_) => return Ok(None),
        },
        other => other,
    };
    let Value::Object(map) = owned else {
        return Ok(None);
    };

    let mut result: Map<String, Value> = Map::new();
    for window_key in WINDOW_KEYS {
        if let Some(parsed) = parse_usage_window(&map, window_key) {
            result.insert(window_key.to_string(), Value::Object(parsed));
        }
    }

    for window_key in WINDOW_KEYS {
        if !result.contains_key(window_key) {
            continue;
        }
        for container_key in ["usage", "rate_limits", "limits"] {
            let container = match map.get(container_key) {
                Some(Value::Object(sec)) => sec,
                _ => &map,
            };
            let Some(Value::Object(window)) = container.get(window_key) else {
                continue;
            };
            for &reset_key in &RESET_KEYS {
                if let Some(reset_val) = window.get(reset_key)
                    && !reset_val.is_null()
                {
                    if let Some(Value::Object(dest)) = result.get_mut(window_key) {
                        dest.insert(
                            "reset_at".to_string(),
                            Value::String(py_str_scalar(reset_val)),
                        );
                    }
                    break;
                }
            }
        }
    }

    if result.is_empty() {
        Ok(None)
    } else {
        Ok(Some(Value::Object(result)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_wham_usage_windows_and_resets() {
        let body = json!({
            "usage": {
                "5h": {"used": 1200, "limit": 10000, "resets_at": "2026-09-25T00:00:00Z"},
                "7d": {"remaining": 250, "limit": 1000}
            }
        });
        let parsed = parse_wham_usage(body).unwrap().unwrap();
        assert_eq!(
            parsed,
            json!({
                "5h": {"used": 1200, "limit": 10000, "remaining": 8800, "percent": 12.0, "reset_at": "2026-09-25T00:00:00Z"},
                "7d": {"used": 750, "limit": 1000, "remaining": 250, "percent": 75.0}
            })
        );
    }
}
