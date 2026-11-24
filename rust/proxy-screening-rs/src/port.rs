//! Rust Aho-Corasick port of `classify_network_type` in `sms_tool/proxy/screening.py`.
//!
//! Parity between `markers.json` and `sms_tool/proxy/screening.py` is pinned by
//! `tests/governance/test_proxy_screening_rust_parity.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use aho_corasick::{AhoCorasick, AhoCorasickBuilder};
use serde::Deserialize;
use serde_json::{Map, Value};

const MARKERS_JSON: &str = include_str!("../markers.json");

const FLAG_DATACENTER: u8 = 1 << 0;
const FLAG_MOBILE: u8 = 1 << 1;

#[derive(Deserialize)]
struct Spec {
    datacenter_keywords: Vec<String>,
    mobile_keywords: Vec<String>,
}

struct ScreeningEngine {
    ac: AhoCorasick,
    flags: Vec<u8>,
}

fn engine() -> Result<&'static ScreeningEngine, String> {
    static CELL: OnceLock<Result<ScreeningEngine, String>> = OnceLock::new();
    let res = CELL.get_or_init(|| {
        let spec: Spec = serde_json::from_str(MARKERS_JSON).map_err(|e| e.to_string())?;
        let mut patterns: Vec<String> =
            Vec::with_capacity(spec.datacenter_keywords.len() + spec.mobile_keywords.len());
        let mut flags: Vec<u8> =
            Vec::with_capacity(spec.datacenter_keywords.len() + spec.mobile_keywords.len());
        for kw in spec.datacenter_keywords {
            patterns.push(kw);
            flags.push(FLAG_DATACENTER);
        }
        for kw in spec.mobile_keywords {
            patterns.push(kw);
            flags.push(FLAG_MOBILE);
        }
        let ac = AhoCorasickBuilder::new()
            .ascii_case_insensitive(true)
            .build(&patterns)
            .map_err(|e| e.to_string())?;
        Ok(ScreeningEngine { ac, flags })
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
        Value::Object(m) => {
            if m.len() == 1
                && let Some(Value::String(tag)) = m.get("$num")
            {
                return tag != "-0";
            }
            !m.is_empty()
        }
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
        Value::Object(map) => {
            if map.len() == 1 {
                if let Some(Value::String(tag)) = map.get("$num") {
                    return match tag.as_str() {
                        "NaN" => "nan".to_string(),
                        "Infinity" => "inf".to_string(),
                        "-Infinity" => "-inf".to_string(),
                        "-0" => "-0.0".to_string(),
                        other => other.to_string(),
                    };
                }
                if let Some(Value::String(i)) = map.get("$int") {
                    return i.clone();
                }
            }
            format!(
                "{{{}}}",
                map.iter()
                    .map(|(k, val)| format!("{}: {}", py_repr_str(k), py_repr_val(val)))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
    }
}

fn py_repr_val(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr_str(s),
        _ => py_str_val(v),
    }
}

fn append_py_or_empty(v: Option<&Value>, out: &mut String) {
    let Some(val) = v else {
        return;
    };
    if !is_py_truthy(val) {
        return;
    }
    out.push_str(&py_str_val(val));
}

fn is_plain_dict(map: &Map<String, Value>) -> bool {
    !(map.len() == 1
        && map
            .keys()
            .next()
            .is_some_and(|k| matches!(k.as_str(), "$num" | "$int" | "$tuple" | "$set" | "$bytes")))
}

fn has_privacy_flag(map: &Map<String, Value>) -> bool {
    for name in ["hosting", "proxy", "vpn"] {
        if map.get(name) == Some(&Value::Bool(true)) {
            return true;
        }
        for block_key in ["privacy", "security"] {
            if let Some(Value::Object(sub)) = map.get(block_key)
                && is_plain_dict(sub)
                && sub.get(name) == Some(&Value::Bool(true))
            {
                return true;
            }
        }
    }
    false
}

pub fn classify_network_type(intel: serde_json::Value) -> Result<String, String> {
    let Value::Object(map) = intel else {
        return Ok("unknown".to_string());
    };
    if !is_plain_dict(&map) {
        return Ok("unknown".to_string());
    }
    if has_privacy_flag(&map) {
        return Ok("datacenter".to_string());
    }
    let eng = engine()?;
    let mut haystack = String::with_capacity(96);
    for (idx, key) in ["org", "isp", "as", "asn", "hostname", "reverse_dns", "type"]
        .iter()
        .enumerate()
    {
        if idx > 0 {
            haystack.push(' ');
        }
        append_py_or_empty(map.get(*key), &mut haystack);
    }
    let mut hit = 0u8;
    if haystack.is_ascii() {
        for mat in eng.ac.find_overlapping_iter(&haystack) {
            hit |= eng.flags[mat.pattern().as_usize()];
            if (hit & FLAG_DATACENTER) != 0 {
                return Ok("datacenter".to_string());
            }
        }
    } else {
        let lowered = haystack.to_lowercase();
        for mat in eng.ac.find_overlapping_iter(&lowered) {
            hit |= eng.flags[mat.pattern().as_usize()];
            if (hit & FLAG_DATACENTER) != 0 {
                return Ok("datacenter".to_string());
            }
        }
    }
    if (hit & FLAG_MOBILE) != 0 {
        return Ok("mobile".to_string());
    }
    let has_country = map.get("country").is_some_and(is_py_truthy);
    let mut org_text = String::new();
    append_py_or_empty(map.get("org"), &mut org_text);
    if has_country && !org_text.trim().is_empty() {
        return Ok("residential".to_string());
    }
    Ok("unknown".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn classifies_datacenter_mobile_residential_unknown() {
        assert_eq!(classify_network_type(Value::Null).unwrap(), "unknown");
        assert_eq!(
            classify_network_type(
                json!({"country": "US", "org": "Comcast Cable", "privacy": {"vpn": true}})
            )
            .unwrap(),
            "datacenter"
        );
        assert_eq!(
            classify_network_type(json!({"country": "US", "org": "AS16509 Amazon.com, Inc."}))
                .unwrap(),
            "datacenter"
        );
        assert_eq!(
            classify_network_type(
                json!({"country": "ID", "org": "PT Telekomunikasi Selular (Telkomsel)"})
            )
            .unwrap(),
            "mobile"
        );
        assert_eq!(
            classify_network_type(json!({"country": "US", "org": "Comcast Cable Communications"}))
                .unwrap(),
            "residential"
        );
        assert_eq!(
            classify_network_type(json!({"country": "US", "org": "   "})).unwrap(),
            "unknown"
        );
    }
}
