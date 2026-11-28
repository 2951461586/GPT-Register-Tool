//! Pure Rust port of `_aggregate_progress_jsonl` from `sms_tool/registration/progress.py`.

use serde_json::{Map, Number, Value};

fn empty_metrics_json() -> String {
    r#"{"runs":0,"success_rate":0.0,"failure_count":0,"retry_count":0,"auth_session":{"count":0,"average_ms":0.0,"p95_ms":0.0},"stages":{}}"#.to_owned()
}

fn is_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|v| v != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn value_as_stage_string(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => {
            if n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Some(other) => {
            if is_truthy(Some(other)) {
                other.to_string()
            } else {
                String::new()
            }
        }
    }
}

fn value_as_duration_ms(value: Option<&Value>) -> Result<f64, ()> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(0.0),
        Some(Value::Bool(true)) => Ok(1.0),
        Some(Value::Number(n)) => n.as_f64().ok_or(()),
        Some(Value::String(s)) => {
            if s.is_empty() {
                Ok(0.0)
            } else {
                s.trim().parse::<f64>().map_err(|_| ())
            }
        }
        Some(Value::Array(a)) if a.is_empty() => Ok(0.0),
        Some(Value::Object(o)) if o.is_empty() => Ok(0.0),
        Some(_) => Err(()),
    }
}

fn round_py(val: f64, digits: usize) -> f64 {
    // Format uses exact IEEE-754 binary64-to-decimal round-half-to-even,
    // matching CPython's `round(float, ndigits)` on decimal midpoints like 592.15.
    let r = format!("{val:.digits$}").parse::<f64>().unwrap_or(0.0);
    if r == 0.0 { 0.0 } else { r }
}

fn f64_to_json(v: f64) -> Value {
    Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn stats_value(values: &[f64]) -> Value {
    let mut map = Map::new();
    if values.is_empty() {
        map.insert("count".to_owned(), Value::from(0_u64));
        map.insert("average_ms".to_owned(), f64_to_json(0.0));
        map.insert("p95_ms".to_owned(), f64_to_json(0.0));
        return Value::Object(map);
    }
    let mut ordered = values.to_vec();
    ordered.sort_by(f64::total_cmp);
    let n = ordered.len();
    let p95_rank = ((n as f64) * 0.95).round_ties_even() as isize - 1;
    let index = p95_rank.clamp(0, (n - 1) as isize) as usize;
    let sum: f64 = values.iter().sum();
    let avg = round_py(sum / (n as f64), 1);
    let p95 = round_py(ordered[index], 1);
    map.insert("count".to_owned(), Value::from(n as u64));
    map.insert("average_ms".to_owned(), f64_to_json(avg));
    map.insert("p95_ms".to_owned(), f64_to_json(p95));
    Value::Object(map)
}

pub fn _aggregate_progress_jsonl(raw_jsonl: String) -> String {
    let mut runs: usize = 0;
    let mut success_count: usize = 0;
    let mut failure_count: usize = 0;
    let mut retry_count: usize = 0;
    let mut auth_durations: Vec<f64> = Vec::new();
    let mut stage_order: Vec<String> = Vec::new();
    let mut stage_samples: std::collections::HashMap<String, Vec<f64>> =
        std::collections::HashMap::new();

    for line in raw_jsonl.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let parsed: Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => return empty_metrics_json(),
        };
        let Some(row) = parsed.as_object() else {
            continue;
        };
        if row.get("source").and_then(Value::as_str) == Some("test") {
            continue;
        }
        runs += 1;
        if is_truthy(row.get("success")) {
            success_count += 1;
        } else {
            failure_count += 1;
        }
        if let Some(Value::Array(events)) = row.get("events") {
            for event in events {
                let Some(ev) = event.as_object() else {
                    continue;
                };
                let stage = value_as_stage_string(ev.get("stage"));
                let Ok(duration) = value_as_duration_ms(ev.get("duration_ms")) else {
                    return empty_metrics_json();
                };
                if duration > 0.0 {
                    if !stage_samples.contains_key(&stage) {
                        stage_order.push(stage.clone());
                    }
                    stage_samples
                        .entry(stage.clone())
                        .or_default()
                        .push(duration);
                    if stage == "auth_session" {
                        auth_durations.push(duration);
                    }
                }
                if stage.contains("retry") {
                    retry_count += 1;
                }
            }
        }
    }

    let success_rate = if runs > 0 {
        round_py((success_count as f64) / (runs as f64), 4)
    } else {
        0.0
    };

    let mut stages_map = Map::new();
    for name in stage_order {
        if let Some(vals) = stage_samples.get(&name) {
            stages_map.insert(name, stats_value(vals));
        }
    }

    let mut root = Map::new();
    root.insert("runs".to_owned(), Value::from(runs as u64));
    root.insert("success_rate".to_owned(), f64_to_json(success_rate));
    root.insert(
        "failure_count".to_owned(),
        Value::from(failure_count as u64),
    );
    root.insert("retry_count".to_owned(), Value::from(retry_count as u64));
    root.insert("auth_session".to_owned(), stats_value(&auth_durations));
    root.insert("stages".to_owned(), Value::Object(stages_map));
    serde_json::to_string(&Value::Object(root)).unwrap_or_else(|_| empty_metrics_json())
}
