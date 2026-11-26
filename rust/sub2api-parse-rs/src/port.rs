//! Rust port of origin URL normalization and delimited name/int list parsing in
//! `sms_tool/importers/sub2api.py`.

use std::collections::HashSet;

use serde_json::Value;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

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
        Value::Null | Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if i == 0 { String::new() } else { i.to_string() }
            } else if let Some(u) = n.as_u64() {
                if u == 0 { String::new() } else { u.to_string() }
            } else if let Some(f) = n.as_f64() {
                if f == 0.0 {
                    String::new()
                } else {
                    n.to_string()
                }
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(arr) => {
            if arr.is_empty() {
                String::new()
            } else {
                let mut out = String::new();
                py_repr_into(value, &mut out);
                out
            }
        }
        Value::Object(obj) => {
            if obj.is_empty() {
                String::new()
            } else {
                let mut out = String::new();
                py_repr_into(value, &mut out);
                out
            }
        }
    }
}

pub fn _normalize_sub2api_origin(api_url: String) -> Result<String, String> {
    let raw = py_trim(&api_url).trim_end_matches('/');
    if raw.is_empty() {
        return Ok(String::new());
    }

    // CPython urlsplit: strip leading C0/space & remove embedded \t\r\n
    let trimmed = raw.trim_start_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: String = trimmed
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let s = cleaned.as_str();

    let (scheme, rest) = if let Some(idx) = s.find(':') {
        let cand = &s[..idx];
        let valid_scheme = !cand.is_empty()
            && cand.as_bytes()[0].is_ascii_alphabetic()
            && cand
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
        if valid_scheme {
            (cand.to_ascii_lowercase(), &s[idx + 1..])
        } else {
            (String::new(), s)
        }
    } else {
        (String::new(), s)
    };

    let before_frag = rest.split_once('#').map_or(rest, |(a, _)| a);
    let before_query = before_frag.split_once('?').map_or(before_frag, |(a, _)| a);
    let (netloc, path_raw) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };

    if scheme.is_empty() || netloc.is_empty() {
        return Ok(raw.to_string());
    }

    // Validate IPv6 brackets in netloc (matches CPython urlsplit)
    let hostinfo = match netloc.rfind('@') {
        Some(pos) => &netloc[pos + 1..],
        None => netloc,
    };
    if (hostinfo.contains('[') && !hostinfo.contains(']'))
        || (hostinfo.contains(']') && !hostinfo.contains('['))
    {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    if let Some(after_open) = hostinfo.strip_prefix('[') {
        let Some(close) = after_open.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let host = &after_open[..close];
        let ip_part = host.split('%').next().unwrap_or(host);
        if ip_part.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(format!(
                "ValueError: {host:?} does not appear to be an IPv4 or IPv6 address"
            ));
        }
    }

    let mut path = path_raw.trim_end_matches('/');
    let lowered = path.to_lowercase();
    for marker in ["/api/v1", "/api"] {
        if let Some(byte_idx) = lowered.find(marker) {
            // Slice original `path` by character count matching Python `path[:index]`
            let char_idx = lowered[..byte_idx].chars().count();
            let cut = path
                .char_indices()
                .nth(char_idx)
                .map_or(path.len(), |(i, _)| i);
            path = &path[..cut];
            break;
        }
    }
    let clean_path = path.trim_end_matches('/');
    let mut out = String::with_capacity(scheme.len() + 3 + netloc.len() + clean_path.len() + 1);
    out.push_str(&scheme);
    out.push_str("://");
    out.push_str(netloc);
    if !clean_path.is_empty() && !clean_path.starts_with('/') {
        out.push('/');
    }
    out.push_str(clean_path);
    Ok(out)
}

#[inline]
fn is_list_delim(c: char) -> bool {
    matches!(c, '\r' | '\n' | ',' | ';' | '，' | '；')
}

fn parse_py_int_str(s: &str) -> Option<i64> {
    let trimmed = py_trim(s);
    if trimmed.is_empty() {
        return None;
    }
    let bytes = trimmed.as_bytes();
    let (neg, rest) = if let Some(r) = bytes.strip_prefix(b"+") {
        (false, r)
    } else if let Some(r) = bytes.strip_prefix(b"-") {
        (true, r)
    } else {
        (false, bytes)
    };
    if rest.is_empty() || rest[0] == b'_' || rest[rest.len() - 1] == b'_' {
        return None;
    }
    let mut digits = String::with_capacity(rest.len() + 1);
    if neg {
        digits.push('-');
    }
    let mut prev_us = false;
    for &b in rest {
        if b == b'_' {
            if prev_us {
                return None;
            }
            prev_us = true;
        } else if b.is_ascii_digit() {
            prev_us = false;
            digits.push(b as char);
        } else {
            return None;
        }
    }
    digits.parse::<i64>().ok()
}

pub fn _parse_int_list(value: serde_json::Value) -> Result<Vec<i64>, String> {
    if value.is_null() {
        return Ok(Vec::new());
    }
    let mut result: Vec<i64> = Vec::new();
    let mut seen: HashSet<i64> = HashSet::new();

    match value {
        Value::Array(items) => {
            for item in items {
                let s = py_str_or_empty(&item);
                let text = py_trim(&s).trim_start_matches('#');
                if let Some(parsed) = parse_py_int_str(text)
                    && parsed > 0
                    && seen.insert(parsed)
                {
                    result.push(parsed);
                }
            }
        }
        other => {
            let raw = py_str_or_empty(&other);
            for part in raw.split(is_list_delim) {
                let text = py_trim(part).trim_start_matches('#');
                if let Some(parsed) = parse_py_int_str(text)
                    && parsed > 0
                    && seen.insert(parsed)
                {
                    result.push(parsed);
                }
            }
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn normalizes_origin_and_parses_lists() {
        assert_eq!(
            _normalize_sub2api_origin("https://panel.example.com/sub/api/v1/accounts/".to_string())
                .unwrap(),
            "https://panel.example.com/sub"
        );
        assert_eq!(
            _parse_int_list(json!("#1, 2；#1, -3, 0, abc, 42")).unwrap(),
            vec![1, 2, 42]
        );
    }
}
