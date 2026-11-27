//! Rust port of `_playwright_proxy` from
//! `sms_tool/payment/capability_browser.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};

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
        Value::Null | Value::Bool(false) => String::new(),
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

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn percent_decode(s: &str) -> String {
    if !s.contains('%') {
        return s.to_string();
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2]))
        {
            out.push((hi << 4) | lo);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

struct SplitParts {
    scheme: String,
    username: String,
    password: String,
    hostname: String,
    raw_port: Option<String>,
}

/// Mirror `urllib.parse.urlsplit` structural validation (raises `ValueError` for bad
/// IPv6 brackets), while deferring port integer/range validation to `parsed.port`
/// access just like Python's `SplitResult.port`.
fn py_urlsplit_parts(url: &str) -> Result<SplitParts, String> {
    let trimmed = url.trim_matches(|c: char| (c as u32) <= 0x20);
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
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        let split_at = after.find('/').unwrap_or(after.len());
        &after[..split_at]
    } else {
        ""
    };
    if netloc.contains('[') != netloc.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let (username, password, hostport) = if let Some((ui, hp)) = netloc.rsplit_once('@') {
        let (u, p) = ui.split_once(':').unwrap_or((ui, ""));
        (u.to_string(), p.to_string(), hp)
    } else {
        (String::new(), String::new(), netloc)
    };
    let (hostname, raw_port) = if hostport.starts_with('[') {
        let Some(close) = hostport.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let host = hostport[1..close].to_lowercase();
        let addr_part = host.split_once('%').map_or(host.as_str(), |(a, _)| a);
        if addr_part.parse::<std::net::Ipv4Addr>().is_ok() {
            return Err("ValueError: An IPv4 address cannot be in brackets".to_string());
        }
        if addr_part.parse::<std::net::Ipv6Addr>().is_err() {
            let mut repr = String::new();
            py_str_repr(&host, &mut repr);
            return Err(format!(
                "ValueError: {repr} does not appear to be an IPv4 or IPv6 address"
            ));
        }
        let tail = &hostport[close + 1..];
        let raw_port = tail.strip_prefix(':').map(ToString::to_string);
        (host, raw_port)
    } else if hostport.contains('[') || hostport.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    } else if let Some((h, ps)) = hostport.split_once(':') {
        (h.to_lowercase(), Some(ps.to_string()))
    } else {
        (hostport.to_lowercase(), None)
    };
    Ok(SplitParts {
        scheme,
        username,
        password,
        hostname,
        raw_port,
    })
}

fn parse_split_port(raw_port: Option<&str>) -> Result<Option<u16>, String> {
    let Some(ps) = raw_port else {
        return Ok(None);
    };
    if ps.is_empty() {
        return Ok(None);
    }
    if !ps.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!(
            "ValueError: Port could not be cast to integer value as '{ps}'"
        ));
    }
    let p: u32 = ps
        .parse()
        .map_err(|_| "ValueError: Port out of range 0-65535".to_string())?;
    if p > 65535 {
        return Err("ValueError: Port out of range 0-65535".to_string());
    }
    Ok(Some(p as u16))
}

pub fn _playwright_proxy(proxy: serde_json::Value) -> Result<Option<serde_json::Value>, String> {
    let raw = py_str_or_empty(&proxy);
    let value = py_trim(&raw);
    if value.is_empty() {
        return Ok(None);
    }
    let parsed = match py_urlsplit_parts(value) {
        Ok(p) => p,
        Err(_) => return Ok(None),
    };
    if parsed.scheme.is_empty() || parsed.hostname.is_empty() {
        return Ok(None);
    }
    let scheme = if parsed.scheme == "socks5h" {
        "socks5"
    } else {
        parsed.scheme.as_str()
    };
    let mut server = format!("{scheme}://{}", parsed.hostname);
    if let Some(port) = parse_split_port(parsed.raw_port.as_deref())?
        && port != 0
    {
        server.push(':');
        server.push_str(&port.to_string());
    }
    let mut result = Map::with_capacity(3);
    result.insert("server".to_string(), Value::String(server));
    if !parsed.username.is_empty() {
        result.insert(
            "username".to_string(),
            Value::String(percent_decode(&parsed.username)),
        );
    }
    if !parsed.password.is_empty() {
        result.insert(
            "password".to_string(),
            Value::String(percent_decode(&parsed.password)),
        );
    }
    Ok(Some(Value::Object(result)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn converts_proxy_and_errors_on_bad_port() {
        assert_eq!(_playwright_proxy(json!(null)).unwrap(), None);
        assert_eq!(_playwright_proxy(json!("http://[bad:8080")).unwrap(), None);
        assert_eq!(
            _playwright_proxy(json!("socks5h://u%401:p%3A2@127.0.0.1:1080")).unwrap(),
            Some(json!({
                "server": "socks5://127.0.0.1:1080",
                "username": "u@1",
                "password": "p:2",
            }))
        );
        assert!(_playwright_proxy(json!("http://host:99999")).is_err());
    }
}
