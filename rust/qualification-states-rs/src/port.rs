//! Rust port of `qualification_state` and `qualification_label`
//! from `sms_tool/payment/qualification_states.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};

const AUTH_INVALID_MARKERS: &[&str] = &[
    "401",
    "unauthorized",
    "token_invalid",
    "access_token_invalid",
    "missing_access_token",
    "checkout_unauthorized",
    "stripe_init_unauthorized",
    "invalid_grant",
];

const COUNTRY_MISMATCH_MARKERS: &[&str] = &[
    "billing country must match",
    "country must match request country",
];

const EGRESS_DENIED_MARKERS: &[&str] = &[
    "target host denied",
    "tunnel connection failed",
    "unable to connect to proxy",
    "forbidden ip",
    "proxy authentication",
];

const TRANSPORT_MARKERS: &[&str] = &[
    "transport",
    "timeout",
    "timed out",
    "curl: (",
    "connection",
    "proxy",
    "tls",
];

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
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
        other => {
            let mut out = String::new();
            py_repr_into(other, &mut out);
            out
        }
    }
}

fn text_of(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(v) => py_trim(&py_str_or_empty(v)).to_lowercase(),
    }
}

fn method_label_str(raw: &str) -> String {
    let key = py_trim(raw).to_lowercase().replace('-', "_");
    match key.as_str() {
        "paypal" => "PayPal".to_string(),
        "gcash" => "GCash".to_string(),
        "momo" => "MoMo".to_string(),
        "upi" => "UPI".to_string(),
        "gopay" => "GoPay".to_string(),
        "grabpay" => "GrabPay".to_string(),
        "qris" => "QRIS".to_string(),
        "bizum" => "Bizum".to_string(),
        "naver_pay" => "Naver Pay".to_string(),
        "ideal" => "iDEAL".to_string(),
        "pix" => "PIX".to_string(),
        "kakao" => "Kakao Pay".to_string(),
        "blik" => "BLIK".to_string(),
        "twint" => "TWINT".to_string(),
        "direct_card" => "卡支付".to_string(),
        _ => key.to_uppercase(),
    }
}

#[inline]
fn has_any_marker_in_parts(parts: &[&str], markers: &[&str]) -> bool {
    for &p in parts {
        if !p.is_empty() && markers.iter().any(|&m| p.contains(m)) {
            return true;
        }
    }
    false
}

fn classify_map(map: &Map<String, Value>) -> &'static str {
    if map.is_empty() {
        return "unknown";
    }
    let conclusive = map.get("conclusive").is_some_and(|v| !is_py_falsy(v));
    if conclusive {
        match map.get("eligible") {
            Some(Value::Bool(true)) => return "eligible",
            Some(Value::Bool(false)) => return "ineligible",
            _ => {}
        }
    }
    let decision = text_of(map.get("decision"));
    if decision == "method_country_mismatch" {
        return "ineligible";
    }
    let err = text_of(map.get("error"));
    let err_code = text_of(map.get("error_code"));
    if has_any_marker_in_parts(&[&err, &err_code, &decision], COUNTRY_MISMATCH_MARKERS) {
        return "ineligible";
    }
    let err_stage = text_of(map.get("error_stage"));
    let err_parts = [err.as_str(), err_code.as_str(), err_stage.as_str()];
    if has_any_marker_in_parts(&err_parts, AUTH_INVALID_MARKERS) {
        return "auth_invalid";
    }
    if has_any_marker_in_parts(&err_parts, EGRESS_DENIED_MARKERS) {
        return "egress_denied";
    }
    let status = text_of(map.get("status"));
    if status == "failed" || has_any_marker_in_parts(&err_parts, TRANSPORT_MARKERS) {
        return "probe_failed";
    }
    "unknown"
}

pub fn qualification_state(result: serde_json::Value) -> Result<String, String> {
    let Value::Object(map) = &result else {
        return Ok("unknown".to_string());
    };
    Ok(classify_map(map).to_string())
}

pub fn qualification_label(
    result: serde_json::Value,
    method: Option<serde_json::Value>,
) -> Result<String, String> {
    let map = match &result {
        Value::Object(m) => Some(m),
        _ => None,
    };
    let state = map.map(classify_map).unwrap_or("unknown");
    match state {
        "eligible" => {
            let chosen_method = match &method {
                Some(v) if !is_py_falsy(v) => py_str_or_empty(v),
                _ => map
                    .and_then(|m| m.get("payment_method"))
                    .filter(|v| !is_py_falsy(v))
                    .map(py_str_or_empty)
                    .unwrap_or_default(),
            };
            let name = method_label_str(&chosen_method);
            let mut label = if name.is_empty() {
                "可用".to_string()
            } else {
                format!("可用·{name}")
            };
            let kind_val = map.and_then(|m| {
                m.get("checkout_kind")
                    .filter(|v| !is_py_falsy(v))
                    .or_else(|| m.get("probe_checkout_kind"))
            });
            let kind = text_of(kind_val);
            if kind == "oaics" {
                label.push_str("（自定义）");
            } else if kind == "stripe" {
                label.push_str("（Stripe）");
            }
            Ok(label)
        }
        "ineligible" => {
            let decision = text_of(map.and_then(|m| m.get("decision")));
            if decision == "nonzero_offer" {
                return Ok("无0元优惠".to_string());
            }
            if decision == "checkout_currency_mismatch" {
                return Ok("币种不符".to_string());
            }
            if decision == "method_country_mismatch" {
                return Ok("无可用方式·地区不符".to_string());
            }
            let err = text_of(map.and_then(|m| m.get("error")));
            let err_code = text_of(map.and_then(|m| m.get("error_code")));
            if has_any_marker_in_parts(&[&err, &err_code, &decision], COUNTRY_MISMATCH_MARKERS) {
                return Ok("无可用方式·地区不符".to_string());
            }
            Ok("无可用方式".to_string())
        }
        "auth_invalid" => Ok("AT失效".to_string()),
        "egress_denied" => Ok("出口被拒".to_string()),
        "probe_failed" => {
            let err = text_of(map.and_then(|m| m.get("error")));
            let err_code = text_of(map.and_then(|m| m.get("error_code")));
            if err.contains("unusual activity") || err_code.contains("unusual activity") {
                Ok("风控拦截".to_string())
            } else {
                Ok("检测失败".to_string())
            }
        }
        _ => Ok("未确定".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn classifies_states_and_labels() {
        let ok = json!({"eligible": true, "conclusive": true, "payment_method": "paypal", "checkout_kind": "stripe"});
        assert_eq!(qualification_state(ok.clone()).unwrap(), "eligible");
        assert_eq!(
            qualification_label(ok, None).unwrap(),
            "可用·PayPal（Stripe）"
        );
        let risk = json!({"error": "checkout HTTP 400 unusual activity", "status": "failed"});
        assert_eq!(qualification_state(risk.clone()).unwrap(), "probe_failed");
        assert_eq!(qualification_label(risk, None).unwrap(), "风控拦截");
    }
}
