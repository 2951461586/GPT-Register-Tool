//! Rust port of HTML-to-text stripping from `sms_tool/providers/mailbox_gmail.py`.

use std::sync::OnceLock;

use memchr::memchr;
use regex::Regex;
use serde_json::Value;

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

/// Find the earliest ASCII case-insensitive occurrence of `needle` (lowercase ASCII)
/// inside `haystack[from..]`, returning the absolute byte index in `haystack`.
#[inline]
fn find_ascii_ci(haystack: &[u8], from: usize, needle: &[u8]) -> Option<usize> {
    let first = needle[0];
    let mut pos = from;
    while pos + needle.len() <= haystack.len() {
        let rel = memchr(first, &haystack[pos..])?;
        let start = pos + rel;
        if start + needle.len() > haystack.len() {
            return None;
        }
        if haystack[start..start + needle.len()].eq_ignore_ascii_case(needle) {
            return Some(start);
        }
        pos = start + 1;
    }
    None
}

/// Exact equivalent of `re.sub(r"(?is)<(script|style).*?</\1>", " ", text)`.
fn strip_script_style_blocks(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut pos = 0usize;
    let mut copied_until = 0usize;
    while pos < bytes.len() {
        let Some(rel) = memchr(b'<', &bytes[pos..]) else {
            break;
        };
        let lt = pos + rel;
        let after_lt = lt + 1;
        let closing: Option<(&[u8], usize)> = if after_lt + 6 <= bytes.len()
            && bytes[after_lt..after_lt + 6].eq_ignore_ascii_case(b"script")
        {
            Some((b"</script>", after_lt + 6))
        } else if after_lt + 5 <= bytes.len()
            && bytes[after_lt..after_lt + 5].eq_ignore_ascii_case(b"style")
        {
            Some((b"</style>", after_lt + 5))
        } else {
            None
        };
        if let Some((close_tag, search_from)) = closing
            && let Some(close_idx) = find_ascii_ci(bytes, search_from, close_tag)
        {
            out.push_str(&text[copied_until..lt]);
            out.push(' ');
            let end = close_idx + close_tag.len();
            copied_until = end;
            pos = end;
            continue;
        }
        pos = lt + 1;
    }
    out.push_str(&text[copied_until..]);
    out
}

/// Exact equivalent of `re.sub(r"(?s)<[^>]+>", " ", text)`.
fn strip_html_tags(text: &str) -> String {
    let bytes = text.as_bytes();
    if memchr(b'<', bytes).is_none() {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut pos = 0usize;
    let mut copied_until = 0usize;
    while pos < bytes.len() {
        let Some(rel) = memchr(b'<', &bytes[pos..]) else {
            break;
        };
        let lt = pos + rel;
        let Some(gt_rel) = memchr(b'>', &bytes[lt + 1..]) else {
            break;
        };
        if gt_rel == 0 {
            // `<>` does not match `<[^>]+>`.
            pos = lt + 2;
            continue;
        }
        let gt = lt + 1 + gt_rel;
        out.push_str(&text[copied_until..lt]);
        out.push(' ');
        copied_until = gt + 1;
        pos = gt + 1;
    }
    out.push_str(&text[copied_until..]);
    out
}

/// Exact equivalent of `re.sub(r"\s+", " ", text).strip()`.
fn collapse_whitespace_and_strip(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_space = false;
    let mut has_output = false;
    for ch in text.chars() {
        if py_is_space(ch) {
            if has_output {
                in_space = true;
            }
        } else {
            if in_space {
                out.push(' ');
                in_space = false;
            }
            out.push(ch);
            has_output = true;
        }
    }
    out
}

pub fn _html_to_text(value: serde_json::Value) -> Result<String, String> {
    let raw = py_str_or_empty(&value);
    if raw.is_empty() {
        return Ok(String::new());
    }
    let no_blocks = strip_script_style_blocks(&raw);
    let no_tags = strip_html_tags(&no_blocks);
    let unescaped = html_unescape(&no_tags);
    Ok(collapse_whitespace_and_strip(&unescaped))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn html_to_text_strips_blocks_tags_and_entities() {
        let html = "<html><head><STYLE>.x{color:red}</STYLE><script>var a=1;</script></head><body><p>Code: <b>482910</b> &amp; &nbsp; valid</p></body></html>";
        assert_eq!(_html_to_text(json!(html)).unwrap(), "Code: 482910 & valid");
    }

    #[test]
    fn html_to_text_preserves_empty_angle_brackets() {
        assert_eq!(_html_to_text(json!("a <> b <<c> d")).unwrap(), "a <> b d");
    }
}
