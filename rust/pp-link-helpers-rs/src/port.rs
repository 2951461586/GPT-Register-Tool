//! Rust port of proxy-template normalization/country rewriting and PayPal BA
//! approve-URL helpers from `sms_tool/payment/pp_link_helpers.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::{Captures, Regex};
use serde_json::Value;

const HEX: &[u8; 16] = b"0123456789ABCDEF";

struct RegexCache {
    host_port_left: Regex,
    user_region: Regex,
    ipwo_zone: Regex,
    kookeey_pw: Regex,
    pw_region: Regex,
    user_tail_cc: Regex,
    st_city: Regex,
}

fn rx() -> &'static RegexCache {
    static CACHE: OnceLock<RegexCache> = OnceLock::new();
    CACHE.get_or_init(|| RegexCache {
        host_port_left: Regex::new(r"^[a-zA-Z0-9.\-]+:\d+(?:\n?\z)").expect("valid regex"),
        user_region: Regex::new(r"(^|[-_])(region|geo|country)([-_])[A-Za-z]{2}([-_]|$)")
            .expect("valid regex"),
        ipwo_zone: Regex::new(r"(?i)((?:^|[_-])custom[_-]zone[_-])[A-Za-z]{2}($|[_-])")
            .expect("valid regex"),
        kookeey_pw: Regex::new(
            r"^(?P<base>.+?)-(?P<cc>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])$",
        )
        .expect("valid regex"),
        pw_region: Regex::new(r"(?i)((?:^|[-_])(?:country|region|geo)[-_])([A-Za-z]{2})([-_]|$)")
            .expect("valid regex"),
        user_tail_cc: Regex::new(r"-[A-Za-z]{2}$").expect("valid regex"),
        st_city: Regex::new(r"-st-[^-@]+-city-[^-@]+(-sid-)").expect("valid regex"),
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
    netloc: String,
    hostname: String,
    port: Option<u16>,
    path: String,
    query: String,
    fragment: String,
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

fn py_urlsplit(url: &str) -> Option<SplitUrl> {
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
    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return None;
    }
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
            } else if ps.bytes().all(|b| b.is_ascii_digit()) {
                ps.parse::<u32>()
                    .ok()
                    .filter(|&p| p <= 65535)
                    .map(|p| p as u16)
            } else {
                None
            }
        } else {
            None
        };
        (host, port)
    } else if hostport.contains('[') || hostport.contains(']') {
        return None;
    } else if let Some((h, ps)) = hostport.rsplit_once(':') {
        let port = if ps.is_empty() {
            None
        } else if ps.bytes().all(|b| b.is_ascii_digit()) {
            ps.parse::<u32>()
                .ok()
                .filter(|&p| p <= 65535)
                .map(|p| p as u16)
        } else {
            None
        };
        (h.to_lowercase(), port)
    } else {
        (hostport.to_lowercase(), None)
    };
    Some(SplitUrl {
        scheme,
        username,
        password,
        netloc: netloc.to_string(),
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

fn retarget_region_str(value: &str, iso_raw: &str) -> String {
    let iso = py_trim(iso_raw).to_uppercase();
    if value.is_empty() || iso.is_empty() {
        return value.to_string();
    }
    let Some(parsed) = py_urlsplit(value) else {
        return value.to_string();
    };
    let mut username = percent_decode(&parsed.username);
    let mut password = percent_decode(&parsed.password);
    if username.is_empty() && password.is_empty() {
        return value.to_string();
    }
    let mut changed = false;
    let regs = rx();

    if regs.user_region.is_match(&username) {
        username = regs
            .user_region
            .replacen(&username, 1, |caps: &Captures<'_>| {
                format!("{}{}{}{}{}", &caps[1], &caps[2], &caps[3], iso, &caps[4])
            })
            .into_owned();
        changed = true;
    }
    if regs.ipwo_zone.is_match(&username) {
        username = regs
            .ipwo_zone
            .replacen(&username, 1, |caps: &Captures<'_>| {
                format!("{}{}{}", &caps[1], iso, &caps[2])
            })
            .into_owned();
        changed = true;
    }
    if let Some(caps) = regs.kookeey_pw.captures(&password) {
        password = format!(
            "{}-{}-{}-{}",
            &caps["base"], iso, &caps["sid"], &caps["ttl"]
        );
        changed = true;
    } else if regs.pw_region.is_match(&password) {
        password = regs
            .pw_region
            .replacen(&password, 1, |caps: &Captures<'_>| {
                let cc_out = if caps[2].chars().all(|c| c.is_lowercase()) {
                    iso.to_lowercase()
                } else {
                    iso.clone()
                };
                format!("{}{}{}", &caps[1], cc_out, &caps[3])
            })
            .into_owned();
        changed = true;
    }
    if changed {
        rebuild_credentials(&parsed, &username, &password, value)
    } else {
        value.to_string()
    }
}

fn normalize_proxy_template_str(raw: &str) -> String {
    let proxy = py_trim(raw);
    if proxy.is_empty() {
        return String::new();
    }
    if !proxy.contains('@') {
        let parts: Vec<&str> = proxy.split(':').collect();
        if parts.len() == 4
            && parts[0].contains('.')
            && !parts[1].is_empty()
            && parts[1].chars().all(|c| c.is_numeric())
        {
            return normalize_proxy_url_str(
                &format!("{}:{}@{}:{}", parts[2], parts[3], parts[0], parts[1]),
                "http",
            );
        }
        return normalize_proxy_url_str(proxy, "http");
    }
    let parts: Vec<&str> = proxy.split('@').collect();
    if parts.len() != 2 {
        return normalize_proxy_url_str(proxy, "http");
    }
    let (left, right) = (parts[0], parts[1]);
    if rx().host_port_left.is_match(left) && left.split(':').next().is_some_and(|h| h.contains('.'))
    {
        return normalize_proxy_url_str(&format!("{right}@{left}"), "http");
    }
    normalize_proxy_url_str(proxy, "http")
}

pub fn normalize_proxy_template(template: serde_json::Value) -> Result<String, String> {
    let s = py_str_or_empty(&template);
    Ok(normalize_proxy_template_str(&s))
}

pub fn proxy_for_country_template(
    template: serde_json::Value,
    country: serde_json::Value,
) -> Result<String, String> {
    let tmpl_s = py_str_or_empty(&template);
    let proxy = normalize_proxy_template_str(&tmpl_s);
    let country_s = py_str_or_empty(&country);
    let cc = py_trim(&country_s).to_uppercase();
    if proxy.is_empty() || cc.is_empty() {
        return Ok(proxy);
    }
    let Some((userinfo, host)) = proxy.rsplit_once('@') else {
        return Ok(proxy);
    };

    let canonical = normalize_proxy_url_str(&proxy, "http");
    let rewritten = retarget_region_str(&canonical, &cc);
    let regs = rx();
    if rewritten == proxy {
        if regs.user_tail_cc.is_match(userinfo) {
            let replaced_user = regs
                .user_tail_cc
                .replacen(userinfo, 1, format!("-{cc}").as_str());
            return Ok(normalize_proxy_url_str(
                &format!("{replaced_user}@{host}"),
                "http",
            ));
        }
        return Ok(proxy);
    }

    if cc != "JP"
        && let Some((rewritten_user, rewritten_host)) = rewritten.rsplit_once('@')
        && regs.st_city.is_match(rewritten_user)
    {
        let cleaned = regs.st_city.replacen(rewritten_user, 1, "-sid-");
        if cleaned != rewritten_user {
            return Ok(normalize_proxy_url_str(
                &format!("{cleaned}@{rewritten_host}"),
                "http",
            ));
        }
    }
    Ok(rewritten)
}

fn unquote_plus(s: &str) -> String {
    if !s.contains('%') && !s.contains('+') {
        return s.to_string();
    }
    let plus_replaced = s.replace('+', " ");
    percent_decode(&plus_replaced)
}

pub fn is_paypal_ba_approve_url(url: serde_json::Value) -> Result<bool, String> {
    let Value::String(raw) = url else {
        return Ok(false);
    };
    let Some(parsed) = py_urlsplit(&raw) else {
        return Ok(false);
    };
    let host = parsed.netloc.as_str();
    let is_paypal_host = host.eq_ignore_ascii_case("paypal.com")
        || (host.len() > 11 && host[host.len() - 11..].eq_ignore_ascii_case(".paypal.com"));
    if !is_paypal_host {
        return Ok(false);
    }
    let path = parsed.path.trim_end_matches('/');
    if !path.eq_ignore_ascii_case("/agreements/approve") {
        return Ok(false);
    }
    let mut ba_val: Option<String> = None;
    if !parsed.query.is_empty() {
        for pair in parsed.query.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (k_raw, v_raw) = pair.split_once('=').unwrap_or((pair, ""));
            let is_ba_key =
                k_raw == "ba_token" || (k_raw.contains('%') && unquote_plus(k_raw) == "ba_token");
            if is_ba_key {
                ba_val = Some(unquote_plus(v_raw));
            }
        }
    }
    Ok(ba_val.is_some_and(|v| !py_trim(&v).is_empty()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rewrites_country_and_strips_st_city() {
        let out = proxy_for_country_template(
            json!("http://user-region-US-st-ca-city-la-sid-abc123:pass@proxy.example:8080"),
            json!("DE"),
        )
        .unwrap();
        assert_eq!(
            out,
            "http://user-region-DE-sid-abc123:pass@proxy.example:8080"
        );
    }

    #[test]
    fn validates_paypal_ba_url() {
        assert!(
            is_paypal_ba_approve_url(json!(
                "https://www.paypal.com/agreements/approve?ba_token=BA-12345&x=1"
            ))
            .unwrap()
        );
        assert!(
            !is_paypal_ba_approve_url(json!(
                "https://evil-paypal.com.example/agreements/approve?ba_token=BA-12345"
            ))
            .unwrap()
        );
    }
}
