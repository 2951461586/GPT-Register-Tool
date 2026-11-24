//! Rust Aho-Corasick port of `sms_tool/kernel/error_classification.py`.
//!
//! Marker parity between `markers.json` and `sms_tool/kernel/failure_registry.py`
//! is enforced by `tests/governance/test_error_classification_rust_parity.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use aho_corasick::AhoCorasick;
use serde::Deserialize;
use serde_json::Value;

const MARKERS_JSON: &str = include_str!("../markers.json");

#[derive(Deserialize)]
struct ClassSpec {
    code: String,
    markers: Vec<String>,
}

#[derive(Deserialize)]
struct RegistrySpec {
    classes: Vec<ClassSpec>,
    terminal_markers: Vec<String>,
}

#[derive(Clone, Copy)]
struct PatternMeta {
    class_idx: usize,
    is_curl: bool,
    is_generic_net: bool,
}

struct ClassifierEngine {
    codes: Vec<String>,
    internal_idx: usize,
    network_idx: usize,
    class_ac: AhoCorasick,
    metas: Vec<PatternMeta>,
    terminal_ac: AhoCorasick,
}

fn engine() -> Result<&'static ClassifierEngine, String> {
    static CELL: OnceLock<Result<ClassifierEngine, String>> = OnceLock::new();
    let res = CELL.get_or_init(|| {
        let spec: RegistrySpec = serde_json::from_str(MARKERS_JSON).map_err(|e| e.to_string())?;
        let mut codes = Vec::with_capacity(spec.classes.len());
        let mut patterns: Vec<String> = Vec::with_capacity(128);
        let mut metas: Vec<PatternMeta> = Vec::with_capacity(128);
        let mut internal_idx = usize::MAX;
        let mut network_idx = usize::MAX;
        for (class_idx, cls) in spec.classes.into_iter().enumerate() {
            let is_net = cls.code == "network";
            if cls.code == "internal" {
                internal_idx = class_idx;
            }
            if is_net {
                network_idx = class_idx;
            }
            codes.push(cls.code);
            for marker in cls.markers {
                let is_curl = is_net && marker.starts_with("curl:");
                let is_generic_net =
                    is_net && !marker.is_empty() && marker.chars().all(char::is_alphabetic);
                patterns.push(marker);
                metas.push(PatternMeta {
                    class_idx,
                    is_curl,
                    is_generic_net,
                });
            }
        }
        let class_ac = AhoCorasick::new(&patterns).map_err(|e| e.to_string())?;
        let terminal_ac = AhoCorasick::new(&spec.terminal_markers).map_err(|e| e.to_string())?;
        Ok(ClassifierEngine {
            codes,
            internal_idx,
            network_idx,
            class_ac,
            metas,
            terminal_ac,
        })
    });
    res.as_ref().map_err(Clone::clone)
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
        Value::Object(m) => !m.is_empty(),
    }
}

fn py_repr_str(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(q);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(q);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

fn py_str_val(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        Value::Array(arr) => format!(
            "[{}]",
            arr.iter().map(py_repr_val).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, val)| format!("{}: {}", py_repr_str(k), py_repr_val(val)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn py_repr_val(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr_str(s),
        _ => py_str_val(v),
    }
}

fn write_py_json(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => {
            if let Ok(encoded) = serde_json::to_string(s) {
                out.push_str(&encoded);
            }
        }
        Value::Array(arr) => {
            out.push('[');
            for (idx, item) in arr.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                write_py_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (idx, (k, val)) in map.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                if let Ok(encoded) = serde_json::to_string(k) {
                    out.push_str(&encoded);
                }
                out.push_str(": ");
                write_py_json(val, out);
            }
            out.push('}');
        }
    }
}

fn error_text_str(value: &Value) -> String {
    if let Value::Object(map) = value {
        let mut parts: Vec<String> = Vec::new();
        for key in [
            "error",
            "error_code",
            "message",
            "body",
            "status",
            "scan_status",
            "quota_status",
        ] {
            if let Some(item) = map.get(key)
                && is_py_truthy(item)
            {
                parts.push(py_str_val(item));
            }
        }
        for key in ["refresh", "oauth", "relogin", "token_probe"] {
            if let Some(item @ Value::Object(_)) = map.get(key) {
                parts.push(error_text_str(item));
            }
        }
        if parts.is_empty() {
            let mut dumped = String::new();
            write_py_json(value, &mut dumped);
            parts.push(dumped.chars().take(1000).collect());
        }
        return parts.join(" ").to_lowercase();
    }
    if is_py_truthy(value) {
        py_str_val(value).to_lowercase()
    } else {
        String::new()
    }
}

pub fn classify_error(value: serde_json::Value) -> Result<String, String> {
    let eng = engine()?;
    let text = error_text_str(&value);
    let mut class_hit: u32 = 0;
    let mut curl_match = false;
    let mut net_non_generic_hit = false;
    for mat in eng.class_ac.find_overlapping_iter(&text) {
        let meta = eng.metas[mat.pattern().as_usize()];
        class_hit |= 1u32 << meta.class_idx;
        curl_match |= meta.is_curl;
        net_non_generic_hit |= meta.class_idx == eng.network_idx && !meta.is_generic_net;
    }
    let internal_match = eng.internal_idx < 32 && (class_hit & (1u32 << eng.internal_idx)) != 0;
    let mut generic_network_only = false;
    for (idx, code) in eng.codes.iter().enumerate() {
        if idx == eng.internal_idx {
            if internal_match && !curl_match {
                return Ok(code.clone());
            }
            continue;
        }
        if (class_hit & (1u32 << idx)) == 0 {
            continue;
        }
        if idx == eng.network_idx && !net_non_generic_hit {
            generic_network_only = true;
            continue;
        }
        return Ok(code.clone());
    }
    Ok(if generic_network_only {
        "network"
    } else {
        "unknown"
    }
    .to_string())
}

pub fn is_terminal_registration_error(value: serde_json::Value) -> Result<bool, String> {
    let eng = engine()?;
    Ok(eng.terminal_ac.is_match(&error_text_str(&value)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn curl_demotes_internal_and_timeout_yields() {
        assert_eq!(
            classify_error(json!("registration_internal_error:curl: (56)")).unwrap(),
            "network"
        );
        assert_eq!(
            classify_error(json!("registration_internal_error:NameError: x")).unwrap(),
            "internal"
        );
        assert_eq!(
            classify_error(json!("browser_profile_submit_timeout")).unwrap(),
            "auth_state"
        );
        assert_eq!(
            classify_error(json!("upi_redirect_timeout")).unwrap(),
            "upi_payment"
        );
        assert_eq!(classify_error(json!("plain timeout")).unwrap(), "network");
        assert!(is_terminal_registration_error(json!({"error": "http_429"})).unwrap());
        assert!(!is_terminal_registration_error(json!({"error": "tls"})).unwrap());
    }
}
