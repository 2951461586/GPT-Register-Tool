//! Rust port of `_proxy_fingerprint` and `_stripe_reason_text` from
//! `sms_tool/payment/qualification_batch.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const REASON_CHARS: usize = 120;
const ACL_REASON_MARKERS: [&str; 5] = [
    "target host denied",
    "tunnel connection failed",
    "unable to connect to proxy",
    "forbidden ip",
    "proxy authentication",
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

fn empty_fingerprint() -> Value {
    let mut map = Map::with_capacity(3);
    map.insert("proxy_host".to_string(), Value::String(String::new()));
    map.insert("proxy_session".to_string(), Value::String(String::new()));
    map.insert(
        "proxy_fingerprint".to_string(),
        Value::String(String::new()),
    );
    Value::Object(map)
}

fn parse_url_host_port_user(url: &str) -> Option<(String, u16, String)> {
    let trimmed = url.trim_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: String = trimmed
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let s = cleaned.as_str();
    let rest = if let Some(idx) = s.find(':') {
        let cand = &s[..idx];
        let valid_scheme = !cand.is_empty()
            && cand.as_bytes()[0].is_ascii_alphabetic()
            && cand
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
        if valid_scheme { &s[idx + 1..] } else { s }
    } else {
        s
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
        return None;
    }
    let (username, hostport) = if let Some((ui, hp)) = netloc.rsplit_once('@') {
        let (u, _) = ui.split_once(':').unwrap_or((ui, ""));
        (u.to_string(), hp)
    } else {
        (String::new(), netloc)
    };
    let (hostname, raw_port) = if hostport.starts_with('[') {
        let close = hostport.find(']')?;
        let host = hostport[1..close].to_lowercase();
        let addr_part = host.split_once('%').map_or(host.as_str(), |(a, _)| a);
        if addr_part.parse::<std::net::Ipv4Addr>().is_ok()
            || addr_part.parse::<std::net::Ipv6Addr>().is_err()
        {
            return None;
        }
        let tail = &hostport[close + 1..];
        (host, tail.strip_prefix(':'))
    } else if hostport.contains('[') || hostport.contains(']') {
        return None;
    } else if let Some((h, ps)) = hostport.split_once(':') {
        (h.to_lowercase(), Some(ps))
    } else {
        (hostport.to_lowercase(), None)
    };
    let port = match raw_port {
        None | Some("") => 0u16,
        Some(ps) => {
            if !ps.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let p: u32 = ps.parse().ok()?;
            if p > 65535 {
                return None;
            }
            p as u16
        }
    };
    Some((hostname, port, username))
}

fn sha256_prefix10(text: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let digest = Sha256::digest(text.as_bytes());
    let mut out = String::with_capacity(10);
    for &b in &digest[..5] {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

pub fn _proxy_fingerprint(proxy: serde_json::Value) -> Result<serde_json::Value, String> {
    let raw = py_str_or_empty(&proxy);
    let value = py_trim(&raw);
    if value.is_empty() {
        return Ok(empty_fingerprint());
    }
    let with_scheme;
    let url = if value.contains("://") {
        value
    } else {
        with_scheme = format!("http://{value}");
        with_scheme.as_str()
    };
    let Some((host, port, raw_user)) = parse_url_host_port_user(url) else {
        return Ok(empty_fingerprint());
    };
    let host_label = if port != 0 {
        format!("{host}:{port}")
    } else {
        host
    };
    let username = percent_decode(&raw_user);
    if username.is_empty() {
        let mut map = Map::with_capacity(3);
        map.insert("proxy_host".to_string(), Value::String(host_label.clone()));
        map.insert("proxy_session".to_string(), Value::String(String::new()));
        map.insert("proxy_fingerprint".to_string(), Value::String(host_label));
        return Ok(Value::Object(map));
    }
    let session = sha256_prefix10(&username);
    let fp = format!("{host_label}#sid-{session}");
    let mut map = Map::with_capacity(3);
    map.insert("proxy_host".to_string(), Value::String(host_label));
    map.insert("proxy_session".to_string(), Value::String(session));
    map.insert("proxy_fingerprint".to_string(), Value::String(fp));
    Ok(Value::Object(map))
}

fn py_collapse_whitespace(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_word = false;
    for ch in s.chars() {
        if py_is_space(ch) {
            in_word = false;
        } else {
            if !in_word && !out.is_empty() {
                out.push(' ');
            }
            out.push(ch);
            in_word = true;
        }
    }
    out
}

pub fn _stripe_reason_text(text: serde_json::Value) -> Result<String, String> {
    let raw = py_str_or_empty(&text);
    let collapsed = py_collapse_whitespace(&raw);
    if collapsed.is_empty() {
        return Ok(String::new());
    }

    // Build Python-equivalent lowercased string with char-index tracking.
    // Note: in Python, `lowered = collapsed.lower()` may expand a single codepoint
    // (e.g., U+0130 -> 'i' + U+0307), shifting subsequent char indices in `lowered`
    // relative to `collapsed`. Computing char positions in `lowered` mirrors Python's
    // `lowered.find(marker)` and `collapsed[index:end]` semantics directly.
    let lowered = collapsed.to_lowercase();
    let collapsed_chars: Vec<char> = collapsed.chars().collect();
    let total_chars = collapsed_chars.len();

    let mut earliest_char_idx: Option<usize> = None;
    for marker in ACL_REASON_MARKERS {
        if let Some(byte_pos) = lowered.find(marker) {
            let char_pos = lowered[..byte_pos].chars().count();
            earliest_char_idx = Some(match earliest_char_idx {
                Some(prev) => prev.min(char_pos),
                None => char_pos,
            });
        }
    }

    if let Some(index) = earliest_char_idx {
        let end = total_chars.min(index + 88);
        let slice_start = index.min(total_chars);
        let raw_slice: String = collapsed_chars[slice_start..end].iter().collect();
        let snippet = py_trim(&raw_slice).trim_end_matches(['\'', '"', '(', ')']);
        let mut out = String::with_capacity(snippet.len() + 6);
        if index != 0 {
            out.push('…');
        }
        out.push_str(snippet);
        if end < total_chars {
            out.push('…');
        }
        return Ok(out);
    }

    if total_chars <= REASON_CHARS {
        return Ok(collapsed);
    }
    let mut cut = None;
    for i in (0..REASON_CHARS).rev() {
        if collapsed_chars[i] == ' ' {
            cut = Some(i);
            break;
        }
    }
    let final_cut = match cut {
        Some(pos) if pos >= 40 => pos,
        _ => REASON_CHARS,
    };
    let prefix: String = collapsed_chars[..final_cut].iter().collect();
    let trimmed = prefix.trim_end_matches([' ', ',', ';', '(', ':']);
    Ok(format!("{trimmed}…"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fingerprints_proxy_with_and_without_user() {
        assert_eq!(
            _proxy_fingerprint(json!(null)).unwrap(),
            json!({"proxy_host": "", "proxy_session": "", "proxy_fingerprint": ""})
        );
        assert_eq!(
            _proxy_fingerprint(json!("127.0.0.1:8080")).unwrap(),
            json!({"proxy_host": "127.0.0.1:8080", "proxy_session": "", "proxy_fingerprint": "127.0.0.1:8080"})
        );
        let fp = _proxy_fingerprint(json!("http://alice:pw@proxy.example:8080")).unwrap();
        assert_eq!(fp["proxy_host"], json!("proxy.example:8080"));
        assert_eq!(fp["proxy_session"].as_str().unwrap().len(), 10);
    }

    #[test]
    fn extracts_acl_reason_and_cuts_at_word_boundary() {
        let acl = "ProxyError: Max retries (Caused by ProxyError('Tunnel connection failed: 503 Target host denied'))";
        let out = _stripe_reason_text(json!(acl)).unwrap();
        assert_eq!(out, "…Tunnel connection failed: 503 Target host denied");
        let long = format!("{} {}", "x".repeat(40), "y".repeat(200));
        assert_eq!(
            _stripe_reason_text(json!(long)).unwrap(),
            format!("{}…", "x".repeat(40))
        );
    }
}
