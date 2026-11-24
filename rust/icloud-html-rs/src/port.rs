//! Rust port of HTML visible-text stripping from `sms_tool/providers/mailbox_icloud_url.py`.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn strip_html_drops_noise_and_keeps_lines() {
        let html = "<html><head><style>body{color:red}</style></head><body><!-- c --><p>Code: <b>654321</b></p><br/><p>OpenAI &mdash; Thanks</p></body></html>";
        let out = _strip_html_to_visible_text(json!(html)).unwrap();
        assert_eq!(out, "Code: 654321\nOpenAI \u{2014} Thanks");
    }
}
