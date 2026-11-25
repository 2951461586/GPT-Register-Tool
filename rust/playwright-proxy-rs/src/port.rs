//! Rust port of `_playwright_proxy` from
//! `sms_tool/registration_drivers/browser_session.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};

const HEX: &[u8; 16] = b"0123456789ABCDEF";

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

struct ParsedProxy {
    scheme: &'static str,
    host: String,
    port: u16,
    username: String,
    password: String,
}

struct SplitUrl {
    scheme: String,
    username: String,
    password: String,
    hostname: String,
    port: Option<u16>,
}

fn normalize_scheme(scheme: &str) -> Option<&'static str> {
    match py_trim(scheme).to_ascii_lowercase().as_str() {
        "socks" | "socks5" => Some("socks5"),
        "socks4" => Some("socks4"),
        "socks4a" => Some("socks4a"),
        "socks5h" => Some("socks5h"),
        "http" => Some("http"),
        "https" => Some("https"),
        _ => None,
    }
}

fn is_socks(scheme: &str) -> bool {
    matches!(scheme, "socks4" | "socks4a" | "socks5" | "socks5h")
}

fn parse_port(raw: &str) -> Option<u16> {
    let s = py_trim(raw);
    if s.is_empty() {
        return None;
    }
    let s = s.strip_prefix('+').unwrap_or(s);
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let n: u32 = s.parse().ok()?;
    (1..=65535).contains(&n).then_some(n as u16)
}

fn resolve_port(port: Option<u16>, scheme: &str) -> Option<u16> {
    port.or_else(|| is_socks(scheme).then_some(1080))
}

fn normalize_host(host: &str) -> Option<String> {
    let mut h = py_trim(host);
    if h.is_empty() {
        return None;
    }
    if let Some(inner) = h.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        h = inner;
    }
    Some(h.to_string())
}

fn split_host_port(raw: &str) -> (String, Option<u16>) {
    let hp = py_trim(raw);
    if let Some(after_open) = hp.strip_prefix('[') {
        let Some(end) = after_open.find(']') else {
            return (String::new(), None);
        };
        let host = &after_open[..end];
        let tail = &after_open[end + 1..];
        if let Some(port_s) = tail.strip_prefix(':') {
            return (host.to_string(), parse_port(port_s));
        }
        return (host.to_string(), None);
    }
    let colons = hp.bytes().filter(|&b| b == b':').count();
    if colons > 1 {
        return (String::new(), None);
    }
    if let Some((h, p)) = hp.rsplit_once(':') {
        return (h.to_string(), parse_port(p));
    }
    (hp.to_string(), None)
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

fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(HEX[(b >> 4) as usize] as char);
            out.push(HEX[(b & 0x0f) as usize] as char);
        }
    }
    out
}

fn parse_proxy_internal(raw: &str, default_scheme: &str) -> Option<ParsedProxy> {
    let trimmed = py_trim(raw);
    if trimmed.is_empty() {
        return None;
    }
    let owned_val;
    let value = if trimmed.contains('：') {
        owned_val = trimmed.replace('：', ":");
        owned_val.as_str()
    } else {
        trimmed
    };

    let default = normalize_scheme(default_scheme).unwrap_or("http");
    let (scheme, rest) = if let Some((scheme_raw, after)) = value.split_once("://") {
        (normalize_scheme(scheme_raw)?, after)
    } else {
        (default, value)
    };

    if let Some((userinfo, hostport)) = rest.rsplit_once('@') {
        let (u, p) = userinfo.split_once(':').unwrap_or((userinfo, ""));
        let (h_raw, port) = split_host_port(hostport);
        let host = normalize_host(&h_raw)?;
        let port = resolve_port(port, scheme)?;
        return Some(ParsedProxy {
            scheme,
            host,
            port,
            username: percent_decode(u),
            password: percent_decode(p),
        });
    }

    let parts: Vec<&str> = rest.split(':').collect();
    if parts.len() == 4
        && let Some(port) = parse_port(parts[1])
    {
        let host = normalize_host(parts[0])?;
        return Some(ParsedProxy {
            scheme,
            host,
            port,
            username: percent_decode(parts[2]),
            password: percent_decode(parts[3]),
        });
    }

    if let Some(after_open) = rest.strip_prefix('[') {
        let (h_raw, port) = split_host_port(rest);
        let host = normalize_host(&h_raw)?;
        if port.is_none()
            && parts.len() > 2
            && let Some((_, tail)) = after_open.split_once("]:")
            && let Some((port_s, creds)) = tail.split_once(':')
            && let Some(p_num) = parse_port(port_s)
        {
            let (u, p) = creds.split_once(':').unwrap_or((creds, ""));
            return Some(ParsedProxy {
                scheme,
                host,
                port: p_num,
                username: percent_decode(u),
                password: percent_decode(p),
            });
        }
        let port = resolve_port(port, scheme)?;
        return Some(ParsedProxy {
            scheme,
            host,
            port,
            username: String::new(),
            password: String::new(),
        });
    }

    let (h_raw, port) = split_host_port(rest);
    let host = normalize_host(&h_raw)?;
    let port = resolve_port(port, scheme)?;
    Some(ParsedProxy {
        scheme,
        host,
        port,
        username: String::new(),
        password: String::new(),
    })
}

fn proxy_to_url_internal(entry: &ParsedProxy) -> String {
    let mut netloc =
        String::with_capacity(entry.host.len() + entry.username.len() + entry.password.len() + 16);
    if !entry.username.is_empty() {
        netloc.push_str(&percent_encode(&entry.username));
        if !entry.password.is_empty() {
            netloc.push(':');
            netloc.push_str(&percent_encode(&entry.password));
        }
        netloc.push('@');
    }
    if entry.host.contains(':') && !entry.host.starts_with('[') {
        netloc.push('[');
        netloc.push_str(&entry.host);
        netloc.push(']');
    } else {
        netloc.push_str(&entry.host);
    }
    if entry.port != 0 {
        netloc.push(':');
        netloc.push_str(&entry.port.to_string());
    }
    format!("{}://{netloc}", entry.scheme)
}

fn normalize_proxy_url_str(raw: &str, default_scheme: &str) -> String {
    let value = py_trim(raw);
    if value.is_empty() {
        return String::new();
    }
    if let Some(entry) = parse_proxy_internal(value, default_scheme) {
        return proxy_to_url_internal(&entry);
    }
    let (scheme, rest) = value.split_once("://").unwrap_or(("", value));
    let parts: Vec<&str> = rest.split(':').collect();
    if !rest.contains('@')
        && parts.len() == 4
        && parts[0].contains('.')
        && !parts[1].is_empty()
        && parts[1].chars().all(|c| c.is_ascii_digit())
    {
        let sch = if scheme.is_empty() {
            default_scheme
        } else {
            scheme
        };
        return format!(
            "{sch}://{}:{}@{}:{}",
            percent_encode(parts[2]),
            percent_encode(parts[3]),
            parts[0],
            parts[1]
        );
    }
    if scheme.is_empty() {
        return format!("{default_scheme}://{rest}");
    }
    value.to_string()
}

fn py_urlsplit_strict(url: &str) -> Result<SplitUrl, String> {
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
    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let (username, password, hostport) = if let Some((ui, hp)) = netloc.rsplit_once('@') {
        let (u, p) = ui.split_once(':').unwrap_or((ui, ""));
        (u.to_string(), p.to_string(), hp)
    } else {
        (String::new(), String::new(), netloc)
    };
    let (hostname, port) = if hostport.starts_with('[') {
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
        let port = if let Some(ps) = tail.strip_prefix(':') {
            if ps.is_empty() {
                None
            } else {
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
                Some(p as u16)
            }
        } else {
            None
        };
        (host, port)
    } else if hostport.contains('[') || hostport.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    } else if let Some((h, ps)) = hostport.rsplit_once(':') {
        let port = if ps.is_empty() {
            None
        } else {
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
            Some(p as u16)
        };
        (h.to_lowercase(), port)
    } else {
        (hostport.to_lowercase(), None)
    };
    Ok(SplitUrl {
        scheme,
        username,
        password,
        hostname,
        port,
    })
}

pub fn _playwright_proxy(proxy: serde_json::Value) -> Result<Option<serde_json::Value>, String> {
    let raw = py_str_or_empty(&proxy);
    let value = normalize_proxy_url_str(&raw, "http");
    if value.is_empty() {
        return Ok(None);
    }
    let parsed = py_urlsplit_strict(&value)?;
    let scheme = if parsed.scheme == "socks5h" {
        "socks5"
    } else if parsed.scheme.is_empty() {
        "http"
    } else {
        parsed.scheme.as_str()
    };
    let mut server = format!("{scheme}://{}", parsed.hostname);
    if let Some(p) = parsed.port
        && p != 0
    {
        server.push(':');
        server.push_str(&p.to_string());
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
    fn converts_socks5h_and_credentials_to_playwright_dict() {
        assert_eq!(_playwright_proxy(json!(null)).unwrap(), None);
        assert_eq!(_playwright_proxy(json!("   ")).unwrap(), None);
        assert_eq!(
            _playwright_proxy(json!("socks5h://u%401:p%3A2@127.0.0.1:10808")).unwrap(),
            Some(json!({
                "server": "socks5://127.0.0.1:10808",
                "username": "u@1",
                "password": "p:2",
            }))
        );
        assert_eq!(
            _playwright_proxy(json!("proxy.example:8080:user1:pass1")).unwrap(),
            Some(json!({
                "server": "http://proxy.example:8080",
                "username": "user1",
                "password": "pass1",
            }))
        );
    }
}
