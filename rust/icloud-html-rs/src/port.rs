//! Rust port of HTML visible-text stripping, data-URI decoding, JS string unescaping,
//! Chinese timestamp parsing, and URL limit rewriting from
//! `sms_tool/providers/mailbox_icloud_url.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

const HEX: &[u8; 16] = b"0123456789ABCDEF";

fn comment_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?s)<!--.*?-->").expect("valid regex"))
}

fn noise_block_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?is)<(?:head|style|script|noscript|svg)\b[^>]*>.*?</(?:head|style|script|noscript|svg)>",
        )
        .expect("valid regex")
    })
}

fn block_break_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)(?:</(?:p|div|tr|li|h[1-6]|section|article|blockquote|pre|td|th)>|<br\s*/?>|<hr\s*/?>)",
        )
        .expect("valid regex")
    })
}

fn tag_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"<[^>]+>").expect("valid regex"))
}

fn charref_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)").expect("valid regex")
    })
}

fn cn_time_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"([0-9]{4})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日\s*([0-9]{1,2})\s*:\s*([0-9]{2})\s*:\s*([0-9]{2})",
        )
        .expect("valid regex")
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

fn invalid_charref(num: u32) -> Option<&'static str> {
    match num {
        0x00 => Some("\u{fffd}"),
        0x0d => Some("\r"),
        0x80 => Some("\u{20ac}"),
        0x81 => Some("\u{0081}"),
        0x82 => Some("\u{201a}"),
        0x83 => Some("\u{0192}"),
        0x84 => Some("\u{201e}"),
        0x85 => Some("\u{2026}"),
        0x86 => Some("\u{2020}"),
        0x87 => Some("\u{2021}"),
        0x88 => Some("\u{02c6}"),
        0x89 => Some("\u{2030}"),
        0x8a => Some("\u{0160}"),
        0x8b => Some("\u{2039}"),
        0x8c => Some("\u{0152}"),
        0x8d => Some("\u{008d}"),
        0x8e => Some("\u{017d}"),
        0x8f => Some("\u{008f}"),
        0x90 => Some("\u{0090}"),
        0x91 => Some("\u{2018}"),
        0x92 => Some("\u{2019}"),
        0x93 => Some("\u{201c}"),
        0x94 => Some("\u{201d}"),
        0x95 => Some("\u{2022}"),
        0x96 => Some("\u{2013}"),
        0x97 => Some("\u{2014}"),
        0x98 => Some("\u{02dc}"),
        0x99 => Some("\u{2122}"),
        0x9a => Some("\u{0161}"),
        0x9b => Some("\u{203a}"),
        0x9c => Some("\u{0153}"),
        0x9d => Some("\u{009d}"),
        0x9e => Some("\u{017e}"),
        0x9f => Some("\u{0178}"),
        _ => None,
    }
}

fn is_invalid_codepoint(num: u32) -> bool {
    matches!(
        num,
        0x01..=0x08 | 0x0b | 0x0e..=0x1f | 0x7f..=0x9f | 0xfdd0..=0xfdef
    ) || ((num & 0xffff) == 0xfffe || (num & 0xffff) == 0xffff)
}

fn html_unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut last = 0usize;
    for caps in charref_re().captures_iter(s) {
        let Some(m) = caps.get(0) else {
            continue;
        };
        let Some(inner_m) = caps.get(1) else {
            continue;
        };
        out.push_str(&s[last..m.start()]);
        last = m.end();
        let inner = inner_m.as_str();
        if let Some(rest) = inner.strip_prefix('#') {
            let parsed =
                if let Some(hex) = rest.strip_prefix('x').or_else(|| rest.strip_prefix('X')) {
                    u32::from_str_radix(hex.trim_end_matches(';'), 16).ok()
                } else {
                    rest.trim_end_matches(';').parse::<u32>().ok()
                };
            match parsed {
                Some(num) => {
                    if let Some(rep) = invalid_charref(num) {
                        out.push_str(rep);
                    } else if (0xd800..=0xdfff).contains(&num) || num > 0x10ffff {
                        out.push('\u{fffd}');
                    } else if !is_invalid_codepoint(num)
                        && let Some(ch) = char::from_u32(num)
                    {
                        out.push(ch);
                    }
                }
                None => {
                    out.push('\u{fffd}');
                }
            }
        } else {
            let mut probe = String::with_capacity(inner.len() + 1);
            probe.push('&');
            probe.push_str(inner);
            let decoded = html_escape::decode_html_entities(&probe);
            out.push_str(&decoded);
        }
    }
    out.push_str(&s[last..]);
    out
}

#[inline]
fn is_py_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{000b}'
            | '\u{000c}'
            | '\u{001c}'
            | '\u{001d}'
            | '\u{001e}'
            | '\u{0085}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

fn push_clean_line(line: &str, out: &mut String, has_lines: &mut bool) {
    let trimmed = py_trim(line);
    if trimmed.is_empty() {
        return;
    }
    if *has_lines {
        out.push('\n');
    }
    *has_lines = true;
    let mut in_space = false;
    for ch in trimmed.chars() {
        if py_is_space(ch) {
            if !in_space {
                out.push(' ');
                in_space = true;
            }
        } else {
            out.push(ch);
            in_space = false;
        }
    }
}

pub fn _strip_html_to_visible_text(raw_html: serde_json::Value) -> Result<String, String> {
    let src = py_str_or_empty(&raw_html);
    if src.is_empty() {
        return Ok(String::new());
    }
    let t1 = comment_re().replace_all(&src, " ");
    let t2 = noise_block_re().replace_all(&t1, " ");
    let t3 = block_break_re().replace_all(&t2, "\n");
    let t4 = tag_re().replace_all(&t3, " ");
    let unescaped = html_unescape(&t4);

    let mut out = String::with_capacity(unescaped.len());
    let mut has_lines = false;
    let mut line_start = 0usize;
    let mut chars = unescaped.char_indices().peekable();
    while let Some((idx, ch)) = chars.next() {
        if is_py_line_break(ch) {
            push_clean_line(&unescaped[line_start..idx], &mut out, &mut has_lines);
            if ch == '\r'
                && matches!(chars.peek(), Some(&(_, '\n')))
                && let Some((nl_idx, _)) = chars.next()
            {
                line_start = nl_idx + 1;
            } else {
                line_start = idx + ch.len_utf8();
            }
        }
    }
    if line_start < unescaped.len() {
        push_clean_line(&unescaped[line_start..], &mut out, &mut has_lines);
    }
    Ok(out)
}

#[inline]
fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn days_in_month(year: u32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

pub fn _latest_mail_received_at(value: serde_json::Value) -> Result<String, String> {
    let text = match &value {
        Value::Null | Value::Bool(false) => "",
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            ""
        }
        Value::Array(a) if a.is_empty() => "",
        Value::Object(o) if o.is_empty() => "",
        Value::String(s) => s.as_str(),
        _ => return Err("TypeError: expected string or bytes-like object".to_string()),
    };
    if text.is_empty() {
        return Ok(String::new());
    }
    let Some(caps) = cn_time_re().captures(text) else {
        return Ok(String::new());
    };
    let year: u32 = caps[1].parse().unwrap_or(0);
    let month: u32 = caps[2].parse().unwrap_or(0);
    let day: u32 = caps[3].parse().unwrap_or(0);
    let hour: u32 = caps[4].parse().unwrap_or(99);
    let minute: u32 = caps[5].parse().unwrap_or(99);
    let second: u32 = caps[6].parse().unwrap_or(99);
    if !(1..=9999).contains(&year)
        || !(1..=12).contains(&month)
        || day == 0
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return Ok(String::new());
    }
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}+08:00"
    ))
}

pub fn _decode_js_string(raw: String) -> Result<String, String> {
    let raw = raw.as_str();
    let mut wrapped = String::with_capacity(raw.len() + 2);
    wrapped.push('"');
    wrapped.push_str(raw);
    wrapped.push('"');
    if let Ok(decoded) = serde_json::from_str::<String>(&wrapped) {
        return Ok(decoded);
    }
    Ok(raw
        .replace("\\r\\n", "\n")
        .replace("\\\"", "\"")
        .replace("\\/", "/"))
}

fn quote_plus(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if b == b' ' {
            out.push('+');
        } else if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(HEX[(b >> 4) as usize] as char);
            out.push(HEX[(b & 0x0f) as usize] as char);
        }
    }
    out
}

fn unquote_plus(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'+' {
            out.push(b' ');
            i += 1;
        } else if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2]))
        {
            out.push((hi << 4) | lo);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub fn _with_message_limit(url: String, limit: serde_json::Value) -> Result<String, String> {
    let url = url.as_str();
    let limit_int: i64 = match &limit {
        Value::Null | Value::Bool(false) => 25,
        Value::Bool(true) => 1,
        Value::Array(a) if a.is_empty() => 25,
        Value::Object(o) if o.is_empty() => 25,
        Value::Number(n) => {
            let v = n
                .as_i64()
                .or_else(|| n.as_f64().map(|f| f as i64))
                .unwrap_or(25);
            if v == 0 { 25 } else { v }
        }
        Value::String(s) => {
            if s.is_empty() {
                25
            } else {
                let t = py_trim(s);
                let v = t.parse::<i64>().map_err(|e| format!("ValueError: {e}"))?;
                if v == 0 { 25 } else { v }
            }
        }
        _ => return Err("TypeError: invalid limit".to_string()),
    };
    let clamped = limit_int.clamp(1, 50);

    // Exact `urllib.parse.urlsplit` decomposition: CPython strips only leading unsafe bytes (<= 0x20)
    // and strips embedded CR/LF/TAB characters.
    let trimmed = url.trim_start_matches(|c: char| (c as u32) <= 0x20);
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
    let (before_frag, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, query_raw) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let (netloc, path) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };

    // Ordered dict of query params (parse_qsl(keep_blank_values=True))
    let mut pairs: Vec<(String, String)> = Vec::new();
    if !query_raw.is_empty() {
        for segment in query_raw.split('&') {
            if segment.is_empty() {
                continue;
            }
            let (k_raw, v_raw) = segment.split_once('=').unwrap_or((segment, ""));
            let k = unquote_plus(k_raw);
            let v = unquote_plus(v_raw);
            if let Some(existing) = pairs.iter_mut().find(|(ek, _)| ek == &k) {
                existing.1 = v;
            } else {
                pairs.push((k, v));
            }
        }
    }
    let n_val = clamped.to_string();
    if let Some(existing) = pairs.iter_mut().find(|(ek, _)| ek == "n") {
        existing.1 = n_val;
    } else {
        pairs.push(("n".to_string(), n_val));
    }

    let mut encoded_query = String::new();
    for (idx, (k, v)) in pairs.iter().enumerate() {
        if idx > 0 {
            encoded_query.push('&');
        }
        encoded_query.push_str(&quote_plus(k));
        encoded_query.push('=');
        encoded_query.push_str(&quote_plus(v));
    }

    // urlunsplit((scheme, netloc, path, query, fragment))
    let mut out = String::with_capacity(url.len() + 16);
    if !netloc.is_empty() || (!scheme.is_empty() && path.starts_with("//")) {
        if !scheme.is_empty() {
            out.push_str(&scheme);
            out.push(':');
        }
        out.push_str("//");
        out.push_str(netloc);
        if !path.is_empty() && !path.starts_with('/') {
            out.push('/');
        }
        out.push_str(path);
    } else {
        if !scheme.is_empty() {
            out.push_str(&scheme);
            out.push(':');
        }
        out.push_str(path);
    }
    if !encoded_query.is_empty() {
        out.push('?');
        out.push_str(&encoded_query);
    }
    if !fragment.is_empty() {
        out.push('#');
        out.push_str(fragment);
    }
    Ok(out)
}

const SPA_CREDENTIAL_KEYS: [&str; 6] = ["key", "token", "auth_code", "auth", "secret", "password"];

struct SplitUrl {
    scheme: String,
    netloc: String,
    query: String,
    fragment: String,
}

fn py_urlsplit(raw: &str) -> SplitUrl {
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
    let (before_frag, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, query) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => &after[..pos],
            None => after,
        }
    } else {
        ""
    };
    SplitUrl {
        scheme,
        netloc: netloc.to_string(),
        query: query.to_string(),
        fragment: fragment.to_string(),
    }
}

fn netloc_host_port(netloc: &str) -> Result<(String, Option<u16>), String> {
    if netloc.is_empty() {
        return Ok((String::new(), None));
    }
    let hostinfo = match netloc.rfind('@') {
        Some(pos) => &netloc[pos + 1..],
        None => netloc,
    };
    if (hostinfo.contains('[') && !hostinfo.contains(']'))
        || (hostinfo.contains(']') && !hostinfo.contains('['))
    {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let (host, port_str) = if let Some(after_open) = hostinfo.strip_prefix('[') {
        let Some(close) = after_open.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let h = &after_open[..close];
        let ip_part = h.split('%').next().unwrap_or(h);
        if ip_part.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(format!(
                "ValueError: {h:?} does not appear to be an IPv4 or IPv6 address"
            ));
        }
        let tail = &after_open[close + 1..];
        let p = tail.strip_prefix(':').unwrap_or("");
        (h.to_lowercase(), p)
    } else if hostinfo.contains('[') || hostinfo.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    } else if let Some((h, p)) = hostinfo.split_once(':') {
        (h.to_lowercase(), p)
    } else {
        (hostinfo.to_lowercase(), "")
    };

    let port = if port_str.is_empty() {
        None
    } else if port_str.bytes().all(|b| b.is_ascii_digit()) {
        match port_str.parse::<u32>() {
            Ok(v) if v <= 65535 => Some(v as u16),
            _ => {
                return Err(format!(
                    "ValueError: Port out of range 0-65535: {port_str:?}"
                ));
            }
        }
    } else {
        return Err(format!(
            "ValueError: Port could not be cast to integer value as {port_str:?}"
        ));
    };
    Ok((host, port))
}

pub fn _same_origin(base_url: String, candidate_url: String) -> Result<bool, String> {
    let left = py_urlsplit(&base_url);
    let right = py_urlsplit(&candidate_url);
    if left.scheme != right.scheme {
        return Ok(false);
    }
    let (lh, lp) = netloc_host_port(&left.netloc)?;
    let (rh, rp) = netloc_host_port(&right.netloc)?;
    if lh != rh {
        return Ok(false);
    }
    let default_port = match left.scheme.as_str() {
        "http" => Some(80u16),
        "https" => Some(443u16),
        _ => None,
    };
    let lp_eff = match lp {
        Some(p) if p > 0 => Some(p),
        _ => default_port,
    };
    let rp_eff = match rp {
        Some(p) if p > 0 => Some(p),
        _ => default_port,
    };
    Ok(lp_eff == rp_eff)
}

fn parse_url_params(raw: &str) -> Vec<(String, String)> {
    let mut text = py_trim(raw);
    if let Some((_, after)) = text.split_once('?') {
        text = after;
    } else if text.starts_with('/') {
        return Vec::new();
    }
    let mut pairs: Vec<(String, String)> = Vec::new();
    if text.is_empty() {
        return pairs;
    }
    for segment in text.split('&') {
        if segment.is_empty() {
            continue;
        }
        let (k_raw, v_raw) = segment.split_once('=').unwrap_or((segment, ""));
        let k_decoded = unquote_plus(k_raw);
        if k_decoded.is_empty() {
            continue;
        }
        let k = py_trim(&k_decoded).to_lowercase();
        let v = py_trim(&unquote_plus(v_raw)).to_string();
        if let Some(existing) = pairs.iter_mut().find(|(ek, _)| ek == &k) {
            existing.1 = v;
        } else {
            pairs.push((k, v));
        }
    }
    pairs
}

fn get_param<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
        .filter(|v| !v.is_empty())
}

pub fn _extract_url_credentials(
    url: String,
    fallback_email: String,
) -> Result<(String, String, bool), String> {
    let parsed = py_urlsplit(&url);
    let frag_params = parse_url_params(&parsed.fragment);
    let query_params = parse_url_params(&parsed.query);
    let email = if let Some(e) = get_param(&frag_params, "email") {
        e.to_lowercase()
    } else if let Some(e) = get_param(&query_params, "email") {
        e.to_lowercase()
    } else {
        py_trim(&fallback_email).to_lowercase()
    };
    for field in SPA_CREDENTIAL_KEYS {
        if let Some(val) = get_param(&frag_params, field) {
            return Ok((email, val.to_string(), true));
        }
    }
    for field in SPA_CREDENTIAL_KEYS {
        if let Some(val) = get_param(&query_params, field) {
            return Ok((email, val.to_string(), false));
        }
    }
    Ok((email, String::new(), false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_origin_and_credentials_work() {
        assert!(
            _same_origin(
                "https://mail.example:443/a".to_string(),
                "https://mail.example/b".to_string()
            )
            .unwrap()
        );
        assert_eq!(
            _extract_url_credentials(
                "https://mail.example/inbox#email=Alice@iCloud.com&key=k123".to_string(),
                "fb@icloud.com".to_string()
            )
            .unwrap(),
            ("alice@icloud.com".to_string(), "k123".to_string(), true)
        );
    }
}
