//! Rust port of `is_cacheable_static_asset` from
//! `sms_tool/registration_drivers/data_saver.py`.

use serde_json::Value;

const PROTECTED_KEYWORDS: &[&str] = &[
    "captcha",
    "challenge",
    "turnstile",
    "arkose",
    "funcaptcha",
    "sentinel",
    "cdn-cgi",
];

const CACHEABLE_HOST_SUFFIXES: &[&str] = &[
    "chatgpt.com",
    "auth.openai.com",
    "oaistatic.com",
    "cdn.openai.com",
];

const DENIED_CACHE_PATH_PARTS: &[&str] = &[
    "/api/",
    "/backend-api/",
    "/backend-anon/",
    "/ces/",
    "/cdn-cgi/",
    "/sentinel/",
    "/authorize",
    "/oauth",
    "/login",
    "/logout",
    "/email-verification",
    "/about-you",
    "/bazaar/",
    "/obi/",
];

const CACHEABLE_RESOURCE_TYPES: &[&str] = &["script", "stylesheet", "font", "image"];

const STATIC_EXTENSIONS: &[&str] = &[
    ".js", ".mjs", ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".avif", ".svg", ".ico",
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

fn is_method_get(method: Option<&Value>) -> bool {
    let Some(val) = method else {
        return true;
    };
    let text = py_str_or_empty(val);
    if text.is_empty() {
        // `method or "GET"` falls back to "GET" for any falsy value.
        return true;
    }
    text.eq_ignore_ascii_case("GET")
}

/// Sanitize URL exactly like Python 3.12 `urllib.parse.urlsplit`:
/// strip leading/trailing C0 controls + space, then remove `\t`, `\r`, `\n`.
fn clean_url_for_split(raw: &str) -> std::borrow::Cow<'_, str> {
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

/// Split an http/https URL like `urllib.parse.urlsplit`, returning `(hostname_lower, path)`.
/// Returns `None` if scheme is not http/https, hostname is missing, or netloc has invalid IPv6 brackets.
fn split_http_url(raw: &str) -> Option<(String, &str)> {
    let bytes = raw.as_bytes();
    let colon = bytes.iter().position(|&b| b == b':')?;
    let scheme = &raw[..colon];
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return None;
    }
    let after_colon = &raw[colon + 1..];
    let rest = after_colon.strip_prefix("//")?;
    let mut end = rest.len();
    for (idx, b) in rest.bytes().enumerate() {
        if matches!(b, b'/' | b'?' | b'#') {
            end = idx;
            break;
        }
    }
    let netloc = &rest[..end];
    let after_netloc = &rest[end..];
    // Validate bracket balance like `urllib.parse._check_bracketed_netloc`
    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return None;
    }
    let hostport = match netloc.rsplit_once('@') {
        Some((_, hp)) => hp,
        None => netloc,
    };
    if hostport.contains('[') || hostport.contains(']') {
        // Bracketed IPv6 host never matches our domain allowlist anyway.
        return None;
    }
    let host = match hostport.split_once(':') {
        Some((h, _)) => h,
        None => hostport,
    };
    if host.is_empty() {
        return None;
    }
    // Extract path (up to `?` or `#`).
    let path_end = after_netloc
        .bytes()
        .position(|b| b == b'?' || b == b'#')
        .unwrap_or(after_netloc.len());
    let path = &after_netloc[..path_end];
    Some((host.to_lowercase(), path))
}

/// Exact Windows/POSIX `Path(path).suffix.lower()` behavior on a lowercased URL path.
fn path_suffix(path_lower: &str) -> &str {
    // On Windows `Path` treats both `/` and `\` as separators and strips trailing separators.
    let trimmed = path_lower.trim_end_matches(['/', '\\']);
    if trimmed.is_empty() {
        return "";
    }
    let name = match trimmed.rfind(['/', '\\']) {
        Some(idx) => &trimmed[idx + 1..],
        None => trimmed,
    };
    if name.is_empty() || name == "." || name == ".." {
        return "";
    }
    let Some(dot_idx) = name.rfind('.') else {
        return "";
    };
    // Trailing dot (`foo.`) has no suffix in Python's PurePath.
    if dot_idx + 1 == name.len() {
        return "";
    }
    // Leading dots only (`.js`, `..js`) have no suffix in Python's PurePath.
    if name[..dot_idx].bytes().all(|b| b == b'.') {
        return "";
    }
    &name[dot_idx..]
}

pub fn is_cacheable_static_asset(
    url: serde_json::Value,
    resource_type: Option<serde_json::Value>,
    method: Option<serde_json::Value>,
) -> Result<bool, String> {
    if !is_method_get(method.as_ref()) {
        return Ok(false);
    }
    let url_str = py_str_or_empty(&url);
    let raw_url = py_trim(&url_str);
    if raw_url.is_empty() {
        return Ok(false);
    }
    let lowered_url = raw_url.to_lowercase();
    if PROTECTED_KEYWORDS.iter().any(|kw| lowered_url.contains(kw)) {
        return Ok(false);
    }
    let cleaned = clean_url_for_split(raw_url);
    let Some((host_lower, path_raw)) = split_http_url(&cleaned) else {
        return Ok(false);
    };
    let host = host_lower.trim_end_matches('.');
    let host_allowed = CACHEABLE_HOST_SUFFIXES.iter().any(|suffix| {
        host == *suffix
            || (host.len() > suffix.len()
                && host.ends_with(suffix)
                && host.as_bytes()[host.len() - suffix.len() - 1] == b'.')
    });
    if !host_allowed {
        return Ok(false);
    }
    let path = path_raw.to_lowercase();
    if DENIED_CACHE_PATH_PARTS
        .iter()
        .any(|part| path.contains(part))
    {
        return Ok(false);
    }
    if let Some(rt_val) = resource_type.as_ref() {
        let rt_str = py_str_or_empty(rt_val);
        let rtype = py_trim(&rt_str).to_lowercase();
        if !rtype.is_empty() && !CACHEABLE_RESOURCE_TYPES.contains(&rtype.as_str()) {
            return Ok(false);
        }
    }
    let suffix = path_suffix(&path);
    Ok(STATIC_EXTENSIONS.contains(&suffix)
        || path.contains("/_next/static/")
        || path.contains("/assets/"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn allows_cdn_js_asset() {
        assert!(
            is_cacheable_static_asset(
                json!("https://cdn.oaistatic.com/assets/app-123.js?v=1"),
                Some(json!("script")),
                Some(json!("GET")),
            )
            .unwrap()
        );
    }

    #[test]
    fn rejects_protected_and_api_paths() {
        assert!(
            !is_cacheable_static_asset(
                json!("https://chatgpt.com/backend-api/sentinel/chat.js"),
                Some(json!("script")),
                None,
            )
            .unwrap()
        );
        assert!(
            !is_cacheable_static_asset(
                json!("https://cdn.oaistatic.com/assets/app.js"),
                Some(json!("script")),
                Some(json!("POST")),
            )
            .unwrap()
        );
    }

    #[test]
    fn suffix_matches_python_purepath() {
        assert_eq!(path_suffix("/assets/app.min.js"), ".js");
        assert_eq!(path_suffix("/.js"), "");
        assert_eq!(path_suffix("/..js"), "");
        assert_eq!(path_suffix("/a..js"), ".js");
        assert_eq!(path_suffix("/a."), "");
        assert_eq!(path_suffix("/assets/app.css/"), ".css");
    }
}
