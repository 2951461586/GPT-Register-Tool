//! Rust port of `is_retryable_network_error` and `_proxy_scheme_candidates`
//! from `sms_tool/paypal/proxy.py`.

#![forbid(unsafe_code)]

const HEX_UPPER: &[u8; 16] = b"0123456789ABCDEF";
const NETWORK_ERROR_MARKERS: [&str; 19] = [
    "timed out",
    "timeout",
    "connection aborted",
    "connection reset",
    "connection refused",
    "remote end closed",
    "remote disconnected",
    "unexpected_eof",
    "eof occurred",
    "ssleoferror",
    "max retries exceeded",
    "proxyerror",
    "unable to connect to proxy",
    "failed to connect",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
];

struct ParsedProxy {
    scheme: &'static str,
    host: String,
    port: u16,
    username: String,
    password: String,
}

#[inline]
fn is_py_ws(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(is_py_ws)
}

pub fn is_retryable_network_error(err_text: Option<String>) -> Result<bool, String> {
    let Some(raw) = err_text else {
        return Ok(false);
    };
    if raw.len() < 7 {
        return Ok(false);
    }
    let lowered = if raw.is_ascii() {
        raw.to_ascii_lowercase()
    } else {
        raw.to_lowercase()
    };
    Ok(NETWORK_ERROR_MARKERS.iter().any(|m| lowered.contains(m)))
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

fn urlsplit_scheme(url: &str) -> String {
    let cleaned: String = url
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let Some(idx) = cleaned.find(':') else {
        return String::new();
    };
    let cand = &cleaned[..idx];
    if !cand.is_empty()
        && cand.as_bytes()[0].is_ascii_alphabetic()
        && cand
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
    {
        return cand.to_ascii_lowercase();
    }
    String::new()
}

pub fn _proxy_scheme_candidates(proxy: Option<String>) -> Result<Vec<String>, String> {
    let raw = proxy.unwrap_or_default();
    let value = normalize_str(&raw, "http");
    if value.is_empty() {
        return Ok(Vec::new());
    }
    let scheme = urlsplit_scheme(&value);
    let alternates: &[&str] = match scheme.as_str() {
        "http" => &["socks5h", "socks5"],
        "https" => &["http", "socks5h", "socks5"],
        "socks5" => &["socks5h", "http"],
        "socks5h" => &["socks5", "http"],
        _ => &[],
    };
    let Some((_, suffix)) = value.split_once("://") else {
        return Err("IndexError: list index out of range".to_string());
    };
    let mut out = Vec::with_capacity(1 + alternates.len());
    out.push(value.clone());
    for alt in alternates {
        let cand = format!("{alt}://{suffix}");
        if !out.contains(&cand) {
            out.push(cand);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retryable_network_markers_match() {
        assert!(!is_retryable_network_error(None).unwrap());
        assert!(is_retryable_network_error(Some("curl: (35) SSL connect error".into())).unwrap());
        assert!(
            is_retryable_network_error(Some("HTTPSConnectionPool: Read timed out.".into()))
                .unwrap()
        );
        assert!(!is_retryable_network_error(Some("HTTP 403 Forbidden".into())).unwrap());
    }

    #[test]
    fn scheme_candidates_expand_in_order() {
        assert_eq!(
            _proxy_scheme_candidates(None).unwrap(),
            Vec::<String>::new()
        );
        assert_eq!(
            _proxy_scheme_candidates(Some("127.0.0.1:7897".into())).unwrap(),
            vec![
                "http://127.0.0.1:7897",
                "socks5h://127.0.0.1:7897",
                "socks5://127.0.0.1:7897"
            ]
        );
    }
}
