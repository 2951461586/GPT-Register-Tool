//! Rust port of HTML/CSS display-text cleaning and recursive email extraction
//! from `sms_tool/providers/cfworker_client.py`.

use std::collections::HashSet;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

fn email_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}").expect("valid regex")
    })
}

fn email_full_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$").expect("valid regex")
    })
}

fn html_detect_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?is)<(?:html|body|div|table|p|span|strong|title|br)\b").expect("valid regex")
    })
}

fn simple_selector_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)^(?:[#.]?[a-z][\w-]*)(?:\s*,\s*[#.]?[a-z][\w-]*)*$").expect("valid regex")
    })
}

fn at_media_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)^@media\b").expect("valid regex"))
}

fn charref_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)").expect("valid regex")
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

/// Exact CPython `html.unescape` numeric-charref replacement + WHATWG HTML5 named entities.
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
        0x01..=0x08
            | 0x0b
            | 0x0e..=0x1f
            | 0x7f..=0x9f
            | 0xfdd0..=0xfdef
    ) || ((num & 0xffff) == 0xfffe || (num & 0xffff) == 0xffff)
}

pub(crate) fn html_unescape(s: &str) -> String {
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
fn is_block_tag(tag: &str) -> bool {
    matches!(
        tag,
        "br" | "p"
            | "div"
            | "tr"
            | "li"
            | "table"
            | "section"
            | "article"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
    )
}

#[inline]
fn is_skip_tag(tag: &str) -> bool {
    matches!(tag, "style" | "script" | "head")
}

#[inline]
fn is_cdata_tag(tag: &str) -> bool {
    matches!(tag, "style" | "script")
}

fn find_cdata_end(bytes: &[u8], from: usize, tag: &str) -> Option<(usize, usize)> {
    let tag_bytes = tag.as_bytes();
    let tlen = tag_bytes.len();
    let mut i = from;
    while i + 2 + tlen <= bytes.len() {
        if bytes[i] == b'<'
            && bytes[i + 1] == b'/'
            && bytes[i + 2..i + 2 + tlen].eq_ignore_ascii_case(tag_bytes)
        {
            let mut j = i + 2 + tlen;
            if j == bytes.len()
                || matches!(
                    bytes[j],
                    b'>' | b' ' | b'\t' | b'\n' | b'\r' | b'\x0c' | b'/'
                )
            {
                while j < bytes.len() && bytes[j] != b'>' {
                    j += 1;
                }
                if j < bytes.len() {
                    return Some((i, j + 1));
                }
            }
        }
        i += 1;
    }
    None
}

fn readable_html_parse(input: &str) -> String {
    let bytes = input.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(len / 2 + 16);
    let mut skip_depth: usize = 0;
    let mut cdata_elem: Option<&'static str> = None;
    let mut i = 0usize;

    while i < len {
        if let Some(ctag) = cdata_elem {
            if let Some((_end_start, after_gt)) = find_cdata_end(bytes, i, ctag) {
                cdata_elem = None;
                skip_depth = skip_depth.saturating_sub(1);
                i = after_gt;
                continue;
            } else {
                break;
            }
        }

        // Fast-forward plain text to next '<'
        let text_start = i;
        while i < len && bytes[i] != b'<' {
            i += 1;
        }
        if i > text_start && skip_depth == 0 {
            let chunk = &input[text_start..i];
            if chunk.contains('&') {
                out.push_str(&html_unescape(chunk));
            } else {
                out.push_str(chunk);
            }
        }
        if i >= len {
            break;
        }

        // bytes[i] == b'<'
        if i + 4 <= len && &bytes[i..i + 4] == b"<!--" {
            if let Some(rel) = input[i + 4..].find("-->") {
                i = i + 4 + rel + 3;
                continue;
            } else {
                break;
            }
        }
        if i + 2 <= len && bytes[i + 1] == b'!' {
            if i + 9 <= len && bytes[i..i + 9].eq_ignore_ascii_case(b"<![cdata[") {
                if let Some(rel) = input[i + 9..].find("]]>") {
                    i = i + 9 + rel + 3;
                    continue;
                } else {
                    break;
                }
            }
            if let Some(rel) = input[i + 2..].find('>') {
                i = i + 2 + rel + 1;
                continue;
            } else {
                break;
            }
        }
        if i + 2 <= len && bytes[i + 1] == b'?' {
            if let Some(rel) = input[i + 2..].find('>') {
                i = i + 2 + rel + 1;
                continue;
            } else {
                break;
            }
        }

        // End tag `</name...>`
        if i + 2 < len && bytes[i + 1] == b'/' && bytes[i + 2].is_ascii_alphabetic() {
            let name_start = i + 2;
            let mut j = name_start;
            while j < len
                && !matches!(
                    bytes[j],
                    b'>' | b' ' | b'\t' | b'\n' | b'\r' | b'\x0c' | b'/'
                )
            {
                j += 1;
            }
            let tag_lower = input[name_start..j].to_ascii_lowercase();
            while j < len && bytes[j] != b'>' {
                j += 1;
            }
            if j < len {
                if is_skip_tag(&tag_lower) && skip_depth > 0 {
                    skip_depth -= 1;
                } else if skip_depth == 0 && is_block_tag(&tag_lower) {
                    out.push('\n');
                }
                i = j + 1;
                continue;
            } else {
                if skip_depth == 0 {
                    out.push_str(&html_unescape(&input[i..]));
                }
                break;
            }
        }

        // Start tag `<name ...>`
        if i + 1 < len && bytes[i + 1].is_ascii_alphabetic() {
            let name_start = i + 1;
            let mut j = name_start;
            while j < len
                && !matches!(
                    bytes[j],
                    b'>' | b' ' | b'\t' | b'\n' | b'\r' | b'\x0c' | b'/'
                )
            {
                j += 1;
            }
            let tag_lower = input[name_start..j].to_ascii_lowercase();
            // Scan attributes honoring quotes
            let mut quote: u8 = 0;
            while j < len {
                let b = bytes[j];
                if quote != 0 {
                    if b == quote {
                        quote = 0;
                    }
                } else if b == b'"' || b == b'\'' {
                    quote = b;
                } else if b == b'>' {
                    break;
                }
                j += 1;
            }
            if j < len {
                // Check self-closing `/>`
                let mut prev = j;
                while prev > name_start && bytes[prev - 1].is_ascii_whitespace() {
                    prev -= 1;
                }
                let self_closing = prev > name_start && bytes[prev - 1] == b'/';
                if is_skip_tag(&tag_lower) {
                    skip_depth += 1;
                    if !self_closing && is_cdata_tag(&tag_lower) {
                        cdata_elem = Some(if tag_lower == "script" {
                            "script"
                        } else {
                            "style"
                        });
                    }
                } else if skip_depth == 0 && is_block_tag(&tag_lower) {
                    out.push('\n');
                }
                if self_closing {
                    if is_skip_tag(&tag_lower) && skip_depth > 0 {
                        skip_depth -= 1;
                    } else if skip_depth == 0 && is_block_tag(&tag_lower) {
                        out.push('\n');
                    }
                }
                i = j + 1;
                continue;
            } else {
                if skip_depth == 0 {
                    out.push_str(&html_unescape(&input[i..]));
                }
                break;
            }
        }

        // Not a recognized tag — emit literal '<'
        if skip_depth == 0 {
            out.push('<');
        }
        i += 1;
    }

    out
}

fn strip_leading_css_str(text: &str) -> String {
    let bytes = text.as_bytes();
    let len = bytes.len();
    let mut position = 0usize;
    while position < len {
        let mut start = position;
        for (offset, ch) in text[position..].char_indices() {
            if !py_is_space(ch) {
                start = position + offset;
                break;
            }
            start = position + offset + ch.len_utf8();
        }
        if start >= len {
            break;
        }
        let Some(rel_open) = text[start..].find('{') else {
            break;
        };
        let opening = start + rel_open;
        if text[start..opening].chars().count() > 500 {
            break;
        }
        let selector = py_trim(&text[start..opening]);
        if !(simple_selector_re().is_match(selector) || at_media_re().is_match(selector)) {
            break;
        }
        let mut depth: i32 = 0;
        let mut closing: Option<usize> = None;
        for (idx, &b) in bytes[opening..].iter().enumerate() {
            if b == b'{' {
                depth += 1;
            } else if b == b'}' {
                depth -= 1;
                if depth == 0 {
                    closing = Some(opening + idx);
                    break;
                }
            }
        }
        let Some(close_idx) = closing else {
            break;
        };
        position = close_idx + 1;
    }
    if position > 0 {
        text[position..].to_string()
    } else {
        text.to_string()
    }
}

fn collapse_spaces_and_trim_line(line: &str, out: &mut String) {
    let trimmed = py_trim(line);
    let mut in_space = false;
    for ch in trimmed.chars() {
        if ch == ' ' || ch == '\t' {
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

pub fn _strip_leading_css(value: serde_json::Value) -> Result<String, String> {
    let text = py_str_or_empty(&value);
    Ok(strip_leading_css_str(&text))
}

pub fn _display_text(value: serde_json::Value) -> Result<String, String> {
    let mut text = py_str_or_empty(&value);
    if text.is_empty() {
        return Ok(String::new());
    }
    if html_detect_re().is_match(&text) {
        text = readable_html_parse(&text);
    }
    let unescaped = html_unescape(&text);
    let normalized = unescaped.replace("\r\n", "\n").replace('\r', "\n");
    let stripped = strip_leading_css_str(&normalized);

    let mut collapsed = String::with_capacity(stripped.len());
    let mut newline_run = 0usize;
    for (idx, line) in stripped.split('\n').enumerate() {
        if idx > 0 {
            newline_run += 1;
            if newline_run <= 2 {
                collapsed.push('\n');
            }
        }
        let before = collapsed.len();
        collapse_spaces_and_trim_line(line, &mut collapsed);
        if collapsed.len() > before {
            newline_run = 0;
        }
    }
    Ok(py_trim(&collapsed).to_string())
}

fn visit_emails(value: &Value, found: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for key in ["email", "address", "mailbox", "account"] {
                if let Some(child) = map.get(key) {
                    let raw = py_trim(&py_str_or_empty(child)).to_lowercase();
                    if email_full_re().is_match(&raw) {
                        found.push(raw);
                    }
                }
            }
            for child in map.values() {
                visit_emails(child, found);
            }
        }
        Value::Array(arr) => {
            for child in arr {
                visit_emails(child, found);
            }
        }
        Value::String(s) => {
            for m in email_re().find_iter(s) {
                found.push(m.as_str().to_ascii_lowercase());
            }
        }
        _ => {}
    }
}

pub fn _extract_emails(data: serde_json::Value, domain: String) -> Result<Vec<String>, String> {
    let mut found = Vec::new();
    visit_emails(&data, &mut found);
    let suffix = if domain.is_empty() {
        String::new()
    } else {
        format!("@{domain}")
    };
    let mut unique = Vec::with_capacity(found.len());
    let mut seen: HashSet<String> = HashSet::with_capacity(found.len());
    for email in found {
        if !suffix.is_empty() && !email.ends_with(&suffix) {
            continue;
        }
        if seen.insert(email.clone()) {
            unique.push(email);
        }
    }
    Ok(unique)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn display_text_strips_style_and_keeps_otp() {
        let html = "<html><head><style>.code{font-size:24px}</style></head><body><p>Your code is <strong>302959</strong></p></body></html>";
        let out = _display_text(json!(html)).unwrap();
        assert_eq!(out, "Your code is 302959");
    }

    #[test]
    fn strip_leading_css_handles_media_query() {
        let css = "#bodyCell { padding: 20px; } @media only screen { body { width: 100%; } } Hello 388302";
        let out = _strip_leading_css(json!(css)).unwrap();
        assert_eq!(out.trim(), "Hello 388302");
    }

    #[test]
    fn extract_emails_dedups_and_filters_domain() {
        let payload = json!({
            "emails": ["A@Liziai.Cloud", "b@other.com", "a@liziai.cloud"],
            "note": "contact c@liziai.cloud"
        });
        let out = _extract_emails(payload, "liziai.cloud".to_string()).unwrap();
        assert_eq!(out, vec!["a@liziai.cloud", "c@liziai.cloud"]);
    }
}
