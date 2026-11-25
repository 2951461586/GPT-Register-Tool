//! Rust port of `extract_web_deployment_attestation` and `_compact_diagnostic`
//! from `sms_tool/payment/wire.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use crate::pynum::Num;
use regex::Regex;
use serde_json::Value;

struct AttestationRegexes {
    json_re: Regex,
    alt_re: Regex,
}

fn rx() -> &'static AttestationRegexes {
    static CACHE: OnceLock<AttestationRegexes> = OnceLock::new();
    CACHE.get_or_init(|| AttestationRegexes {
        json_re: Regex::new(
            r#"\\?"webDeploymentAttestation\\?"[\s\x1c-\x1f]*:[\s\x1c-\x1f]*\\?"([^"\\]+)\\?""#,
        )
        .expect("valid regex"),
        alt_re: Regex::new(
            r#"[^A-Za-z0-9]webDeploymentAttestation[=:][\s\x1c-\x1f]*["']?([A-Za-z0-9._-]+)"#,
        )
        .expect("valid regex"),
    })
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

pub fn extract_web_deployment_attestation(html: serde_json::Value) -> Result<String, String> {
    let text = py_str_or_empty(&html);
    if !text.contains("webDeploymentAttestation") {
        return Ok(String::new());
    }
    let regs = rx();
    for pattern in [&regs.json_re, &regs.alt_re] {
        if let Some(caps) = pattern.captures(&text)
            && let Some(m) = caps.get(1)
        {
            let value = py_trim(m.as_str());
            if !value.is_empty() && value.contains('.') {
                return Ok(value.to_string());
            }
        }
    }
    Ok(String::new())
}

fn collapse_whitespace_and_strip(text: &str) -> String {
    let mut out = String::with_capacity(text.len().min(256));
    let mut in_space = false;
    let mut has_output = false;
    for ch in text.chars() {
        if py_is_space(ch) {
            if has_output {
                in_space = true;
            }
        } else {
            if in_space {
                out.push(' ');
                in_space = false;
            }
            out.push(ch);
            has_output = true;
        }
    }
    out
}

pub fn _compact_diagnostic(value: serde_json::Value, limit: Option<Num>) -> Result<String, String> {
    let raw = py_str_or_empty(&value);
    let collapsed = collapse_whitespace_and_strip(&raw);
    let char_count = collapsed.chars().count();
    match limit.unwrap_or(Num::Int(180)) {
        Num::Int(lim) => {
            if (char_count as i64) <= lim {
                return Ok(collapsed);
            }
            let end_idx = lim.saturating_sub(3);
            let take_chars = if end_idx < 0 {
                ((char_count as i64).saturating_add(end_idx)).max(0) as usize
            } else {
                end_idx as usize
            };
            let mut out: String = collapsed.chars().take(take_chars).collect();
            out.push_str("...");
            Ok(out)
        }
        Num::Float(lim) => {
            if (char_count as f64) <= lim {
                Ok(collapsed)
            } else {
                Err(
                    "TypeError: slice indices must be integers or None or have an __index__ method"
                        .to_string(),
                )
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_attestation_tokens() {
        let html = r#"<script>{"webDeploymentAttestation":"hdr.payload.sig"}</script>"#;
        assert_eq!(
            extract_web_deployment_attestation(json!(html)).unwrap(),
            "hdr.payload.sig"
        );
        let alt = "foo?webDeploymentAttestation=abc.def&bar=1";
        assert_eq!(
            extract_web_deployment_attestation(json!(alt)).unwrap(),
            "abc.def"
        );
        assert_eq!(
            extract_web_deployment_attestation(json!("webDeploymentAttestation=no.prefix"))
                .unwrap(),
            ""
        );
    }

    #[test]
    fn compacts_and_truncates_diagnostics() {
        assert_eq!(
            _compact_diagnostic(json!("  a\r\n\tb   c  "), Some(Num::Int(180))).unwrap(),
            "a b c"
        );
        assert_eq!(
            _compact_diagnostic(json!("abcdef"), Some(Num::Int(5))).unwrap(),
            "ab..."
        );
        assert!(_compact_diagnostic(json!("abcdef"), Some(Num::Float(2.5))).is_err());
    }
}
