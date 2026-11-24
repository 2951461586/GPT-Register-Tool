//! Rust port of `_is_allowed_callback_url` and `_minimal_chatgpt_cookie_header`
//! from `sms_tool/transport/http_utils.py`.

use serde_json::Value;

const KEEP_COOKIES: &[&str] = &[
    "__Host-next-auth.csrf-token",
    "__Secure-next-auth.callback-url",
    "__Secure-next-auth.session-token",
];

const CHUNKED_PREFIXES: &[&str] = &[
    "__Host-next-auth.csrf-token.",
    "__Secure-next-auth.callback-url.",
    "__Secure-next-auth.session-token.",
];

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

struct ParsedUrl {
    scheme: String,
    hostname: Option<String>,
    has_userinfo: bool,
    port_raw: Option<String>,
}

fn clean_url(raw: &str) -> std::borrow::Cow<'_, str> {
    let trimmed = raw.trim_matches(|c: char| (c as u32) <= 0x20);
    if !trimmed
        .as_bytes()
        .iter()
        .any(|&b| matches!(b, b'\t' | b'\r' | b'\n'))
    {
        std::borrow::Cow::Borrowed(trimmed)
    } else {
        let mut s = String::with_capacity(trimmed.len());
        for ch in trimmed.chars() {
            if !matches!(ch, '\t' | '\r' | '\n') {
                s.push(ch);
            }
        }
        std::borrow::Cow::Owned(s)
    }
}

fn is_valid_scheme(s: &str) -> bool {
    let mut bytes = s.bytes();
    let Some(first) = bytes.next() else {
        return false;
    };
    if !first.is_ascii_alphabetic() {
        return false;
    }
    bytes.all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
}

fn parse_url_py(raw: &str) -> Result<ParsedUrl, String> {
    let cleaned = clean_url(raw);
    let s = cleaned.as_ref();
    let (scheme, after_scheme) = match s.find(':') {
        Some(idx) if idx > 0 && is_valid_scheme(&s[..idx]) => {
            (s[..idx].to_ascii_lowercase(), &s[idx + 1..])
        }
        _ => (String::new(), s),
    };
    let Some(rest) = after_scheme.strip_prefix("//") else {
        return Ok(ParsedUrl {
            scheme,
            hostname: None,
            has_userinfo: false,
            port_raw: None,
        });
    };
    let end = rest
        .bytes()
        .position(|b| matches!(b, b'/' | b'?' | b'#'))
        .unwrap_or(rest.len());
    let netloc = &rest[..end];
    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let (has_userinfo, hostport) = match netloc.rsplit_once('@') {
        Some((userinfo, hp)) => {
            // `parsed.username or parsed.password` in Python:
            // empty userinfo (`@host`) -> username="", password=None -> False;
            // `:` alone (`:@host`) -> username="", password="" -> False;
            // any non-empty username or non-empty password -> True.
            let has_nonempty = match userinfo.split_once(':') {
                Some((u, p)) => !u.is_empty() || !p.is_empty(),
                None => !userinfo.is_empty(),
            };
            (has_nonempty, hp)
        }
        None => (false, netloc),
    };
    if let Some(after_open) = hostport.strip_prefix('[') {
        let Some((host, after_close)) = after_open.split_once(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let port_raw = after_close.strip_prefix(':').map(|p| p.to_string());
        return Ok(ParsedUrl {
            scheme,
            hostname: if host.is_empty() {
                None
            } else {
                Some(host.to_lowercase())
            },
            has_userinfo,
            port_raw,
        });
    }
    if hostport.contains('[') || hostport.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let (host, port_raw) = match hostport.split_once(':') {
        Some((h, p)) => (h, Some(p.to_string())),
        None => (hostport, None),
    };
    Ok(ParsedUrl {
        scheme,
        hostname: if host.is_empty() {
            None
        } else {
            Some(host.to_lowercase())
        },
        has_userinfo,
        port_raw,
    })
}

fn parse_py_port(port_raw: Option<&str>) -> Result<Option<u16>, String> {
    let Some(raw) = port_raw else {
        return Ok(None);
    };
    if raw.is_empty() {
        return Ok(None);
    }
    // Python checks `port.isascii() and port.isdigit()`, then `0 <= int(port) <= 65535`.
    if !raw.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!(
            "ValueError: Port could not be cast to integer value as '{raw}'"
        ));
    }
    let value: u32 = raw
        .parse()
        .map_err(|_| "ValueError: Port out of range 0-65535".to_string())?;
    if value > 65535 {
        return Err("ValueError: Port out of range 0-65535".to_string());
    }
    Ok(Some(value as u16))
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

fn base_hostname(base: &Value) -> Result<Option<String>, String> {
    match base {
        Value::String(s) => Ok(parse_url_py(s)?.hostname),
        other if is_py_falsy(other) => Ok(None),
        _ => Err("AttributeError: object has no attribute 'decode'".to_string()),
    }
}

pub fn _is_allowed_callback_url(
    url: serde_json::Value,
    auth_base: serde_json::Value,
    chat_base: serde_json::Value,
) -> Result<bool, String> {
    let url_str = py_str_or_empty(&url);
    let text = py_trim(&url_str);
    if text.is_empty() || text.bytes().any(|b| matches!(b, b'\r' | b'\n' | b'\\')) {
        return Ok(false);
    }
    let Ok(parsed) = parse_url_py(text) else {
        return Ok(false);
    };
    let h1 = base_hostname(&auth_base)?;
    let h2 = base_hostname(&chat_base)?;
    if parsed.scheme != "http" && parsed.scheme != "https" {
        return Ok(false);
    }
    let host = parsed.hostname.as_deref().unwrap_or("");
    let host_allowed =
        h1.as_deref().is_some_and(|h| h == host) || h2.as_deref().is_some_and(|h| h == host);
    if !host_allowed {
        return Ok(false);
    }
    if parsed.has_userinfo {
        return Ok(false);
    }
    let port = parse_py_port(parsed.port_raw.as_deref())?;
    Ok(matches!(port, None | Some(80) | Some(443)))
}

pub fn _minimal_chatgpt_cookie_header(cookie_header: serde_json::Value) -> Result<String, String> {
    let raw = py_str_or_empty(&cookie_header);
    if raw.is_empty() {
        return Ok(String::new());
    }
    let mut out = String::new();
    for item in raw.split(';') {
        let trimmed = py_trim(item);
        let Some((raw_name, raw_value)) = trimmed.split_once('=') else {
            continue;
        };
        let name = py_trim(raw_name);
        let value = py_trim(raw_value);
        if value.is_empty() {
            continue;
        }
        let keep =
            KEEP_COOKIES.contains(&name) || CHUNKED_PREFIXES.iter().any(|p| name.starts_with(p));
        if keep {
            if !out.is_empty() {
                out.push_str("; ");
            }
            out.push_str(name);
            out.push('=');
            out.push_str(value);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn allows_valid_callback_and_rejects_evil_or_userinfo() {
        assert!(
            _is_allowed_callback_url(
                json!("https://chatgpt.com/api/auth/callback/openai?code=abc"),
                json!("https://auth.openai.com"),
                json!("https://chatgpt.com"),
            )
            .unwrap()
        );
        assert!(
            !_is_allowed_callback_url(
                json!("https://user:pass@chatgpt.com/api/auth/callback"),
                json!("https://auth.openai.com"),
                json!("https://chatgpt.com"),
            )
            .unwrap()
        );
        assert!(
            !_is_allowed_callback_url(
                json!("https://chatgpt.com:8443/api/auth/callback"),
                json!("https://auth.openai.com"),
                json!("https://chatgpt.com"),
            )
            .unwrap()
        );
    }

    #[test]
    fn filters_only_essential_cookies() {
        let header = "foo=bar; __Host-next-auth.csrf-token=abc; __Secure-next-auth.session-token.0=s0; empty=; __Secure-next-auth.session-token.1=s1";
        assert_eq!(
            _minimal_chatgpt_cookie_header(json!(header)).unwrap(),
            "__Host-next-auth.csrf-token=abc; __Secure-next-auth.session-token.0=s0; __Secure-next-auth.session-token.1=s1"
        );
    }
}
