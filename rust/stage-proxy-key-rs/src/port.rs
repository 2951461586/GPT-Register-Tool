//! Rust port of `proxy_key` from `sms_tool/proxy/stage_health.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;
use sha2::{Digest, Sha256};

const HEX_UPPER: &[u8; 16] = b"0123456789ABCDEF";
const HEX_LOWER: &[u8; 16] = b"0123456789abcdef";
const USER_SID_PREFIXES: &[&str] = &[
    "-sid-",
    "_sid_",
    "-sessid-",
    "_sessid_",
    "-session-",
    "_session_",
];
const PW_SID_PREFIXES: &[&str] = &[
    "-sid-",
    "_sid_",
    "-sessid-",
    "_sessid_",
    "-session-",
    "_session_",
    "_session-",
];

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
    path: String,
    query: String,
    fragment: String,
}

fn kookeey_pw_regex() -> Result<&'static Regex, String> {
    static CACHE: OnceLock<Result<Regex, String>> = OnceLock::new();
    CACHE
        .get_or_init(|| {
            Regex::new(
                r"\A(?P<base>.+?)-(?P<cc>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])\n?\z",
            )
            .map_err(|e| format!("ValueError: {e}"))
        })
        .as_ref()
        .map_err(Clone::clone)
}

#[inline]
fn is_py_ws(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(is_py_ws)
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
            out.push(HEX_UPPER[(b >> 4) as usize] as char);
            out.push(HEX_UPPER[(b & 0x0f) as usize] as char);
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

fn normalize_str(raw: &str, default_scheme: &str) -> String {
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

fn py_urlsplit(url: &str) -> Option<SplitUrl> {
    let cleaned: String = url
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
    let (before_frag, fragment) = match rest.split_once('#') {
        Some((a, b)) => (a, b.to_string()),
        None => (rest, String::new()),
    };
    let (before_query, query) = match before_frag.split_once('?') {
        Some((a, b)) => (a, b.to_string()),
        None => (before_frag, String::new()),
    };
    let (netloc, path) = if let Some(after) = before_query.strip_prefix("//") {
        let split_at = after.find('/').unwrap_or(after.len());
        (&after[..split_at], after[split_at..].to_string())
    } else {
        ("", before_query.to_string())
    };
    let (username, password, hostport) = if let Some((ui, hp)) = netloc.rsplit_once('@') {
        let (u, p) = ui.split_once(':').unwrap_or((ui, ""));
        (u.to_string(), p.to_string(), hp)
    } else {
        (String::new(), String::new(), netloc)
    };
    let (hostname, port) = if hostport.starts_with('[') {
        let close = hostport.find(']')?;
        let host = hostport[1..close].to_lowercase();
        let tail = &hostport[close + 1..];
        let port = if let Some(ps) = tail.strip_prefix(':') {
            if ps.is_empty() {
                None
            } else {
                if !ps.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                let p: u32 = ps.parse().ok()?;
                if p > 65535 {
                    return None;
                }
                Some(p as u16)
            }
        } else if tail.is_empty() {
            None
        } else {
            return None;
        };
        (host, port)
    } else if hostport.contains('[') || hostport.contains(']') {
        return None;
    } else if let Some((h, ps)) = hostport.rsplit_once(':') {
        let port = if ps.is_empty() {
            None
        } else {
            if !ps.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let p: u32 = ps.parse().ok()?;
            if p > 65535 {
                return None;
            }
            Some(p as u16)
        };
        (h.to_lowercase(), port)
    } else {
        (hostport.to_lowercase(), None)
    };
    Some(SplitUrl {
        scheme,
        username,
        password,
        hostname,
        port,
        path,
        query,
        fragment,
    })
}

fn unsplit_url(scheme: &str, netloc: &str, path: &str, query: &str, fragment: &str) -> String {
    let mut out = String::with_capacity(
        scheme.len() + netloc.len() + path.len() + query.len() + fragment.len() + 8,
    );
    if !scheme.is_empty() {
        out.push_str(scheme);
        out.push(':');
    }
    if !netloc.is_empty()
        || (!scheme.is_empty() && path.starts_with('/'))
        || matches!(
            scheme,
            "http" | "https" | "socks4" | "socks4a" | "socks5" | "socks5h"
        )
    {
        out.push_str("//");
        out.push_str(netloc);
    }
    out.push_str(path);
    if !query.is_empty() {
        out.push('?');
        out.push_str(query);
    }
    if !fragment.is_empty() {
        out.push('#');
        out.push_str(fragment);
    }
    out
}

fn rebuild_credentials(
    parsed: &SplitUrl,
    username: &str,
    password: &str,
    original: &str,
) -> String {
    if parsed.hostname.is_empty() {
        return original.to_string();
    }
    let mut host = if parsed.hostname.contains(':') && !parsed.hostname.starts_with('[') {
        format!("[{}]", parsed.hostname)
    } else {
        parsed.hostname.clone()
    };
    if let Some(p) = parsed.port
        && p != 0
    {
        host.push(':');
        host.push_str(&p.to_string());
    }
    if !username.is_empty() {
        let mut auth = percent_encode(username);
        if !password.is_empty() {
            auth.push(':');
            auth.push_str(&percent_encode(password));
        }
        host = format!("{auth}@{host}");
    }
    let scheme = if parsed.scheme.is_empty() {
        "http"
    } else {
        &parsed.scheme
    };
    unsplit_url(scheme, &host, &parsed.path, &parsed.query, &parsed.fragment)
}

fn replace_first_sid(text: &str, prefixes: &[&str]) -> String {
    let lower = text.to_ascii_lowercase();
    let bytes = text.as_bytes();
    let mut best: Option<(usize, usize)> = None;
    for pref in prefixes {
        let mut from = 0;
        while let Some(rel) = lower[from..].find(pref) {
            let start = from + rel + pref.len();
            let mut end = start;
            while end < bytes.len() && bytes[end].is_ascii_alphanumeric() {
                end += 1;
            }
            let boundary_ok = end == bytes.len() || matches!(bytes[end], b'_' | b'-');
            if end > start && boundary_ok && best.is_none_or(|(b, _)| start < b) {
                best = Some((start, end));
                break;
            }
            from = from + rel + 1;
        }
    }
    if let Some((s, e)) = best {
        format!("{}SESSION{}", &text[..s], &text[e..])
    } else {
        text.to_string()
    }
}

fn replace_first_rola_sid(text: &str) -> String {
    let bytes = text.as_bytes();
    for (i, &b) in bytes.iter().enumerate() {
        if b != b'_' {
            continue;
        }
        let start = i + 1;
        let mut end = start;
        while end < bytes.len() && bytes[end].is_ascii_alphanumeric() {
            end += 1;
        }
        if end == start {
            continue;
        }
        let rest = &text[end..];
        for tag in ["-region-", "-geo-", "-country-"] {
            if let Some(after) = rest.strip_prefix(tag)
                && after.len() >= 2
                && after.as_bytes()[0].is_ascii_alphabetic()
                && after.as_bytes()[1].is_ascii_alphabetic()
                && (after.len() == 2 || matches!(after.as_bytes()[2], b'_' | b':'))
            {
                return format!("{}SESSION{}", &text[..start], &text[end..]);
            }
        }
    }
    text.to_string()
}

fn strip_session_id_internal(proxy: &str) -> Result<String, String> {
    let value = normalize_str(proxy, "http");
    if value.is_empty() {
        return Ok(String::new());
    }
    let Some(parsed) = py_urlsplit(&value) else {
        return Ok(value);
    };
    let mut username = percent_decode(&parsed.username);
    let mut password = percent_decode(&parsed.password);
    if username.is_empty() && password.is_empty() {
        return Ok(value);
    }
    username = replace_first_sid(&username, USER_SID_PREFIXES);
    username = replace_first_rola_sid(&username);
    let rx = kookeey_pw_regex()?;
    if let Some(caps) = rx.captures(&password) {
        password = format!("{}-{}-SESSION-{}", &caps["base"], &caps["cc"], &caps["ttl"]);
    } else {
        password = replace_first_sid(&password, PW_SID_PREFIXES);
    }
    Ok(rebuild_credentials(&parsed, &username, &password, &value))
}

pub fn proxy_key(proxy: Option<String>) -> Result<String, String> {
    let raw = proxy.unwrap_or_default();
    let stripped = strip_session_id_internal(&raw)?;
    if stripped.is_empty() {
        return Ok("direct".to_string());
    }
    let digest = Sha256::digest(stripped.as_bytes());
    let mut out = String::with_capacity(24);
    for &b in &digest[..12] {
        out.push(HEX_LOWER[(b >> 4) as usize] as char);
        out.push(HEX_LOWER[(b & 0x0f) as usize] as char);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_proxy_returns_direct_and_sticky_sessions_collapse() {
        assert_eq!(proxy_key(None).unwrap(), "direct");
        assert_eq!(proxy_key(Some("   ".into())).unwrap(), "direct");
        let k1 = proxy_key(Some(
            "http://user-region-us-sid-abc123:pass@gate.example.com:8000".into(),
        ))
        .unwrap();
        let k2 = proxy_key(Some(
            "http://user-region-us-sid-xyz789:pass@gate.example.com:8000".into(),
        ))
        .unwrap();
        assert_eq!(k1.len(), 24);
        assert_eq!(k1, k2);
    }
}
