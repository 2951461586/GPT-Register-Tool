//! Rust port of the hot OTP extraction and fake-context filtering loops
//! from `sms_tool/mailbox/mail_otp.py`.
//!
//! Eliminates per-candidate dynamic regex compilation (which thrashed CPython's
//! 512-entry `re._cache`) while preserving exact character-window and scoring
//! semantics.

#![forbid(unsafe_code)]

use std::borrow::Cow;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

const OTP_CONTEXT_MARKERS: &[&str] = &[
    "code",
    "verification",
    "verify",
    "openai",
    "chatgpt",
    "login",
    "验证码",
    "驗證碼",
];

const SECONDARY_CONTEXT_MARKERS: &[&str] = &["your", "is", "use", "enter", "sign"];

const FAKE_OTP_CONTEXT_MARKERS: &[&str] = &[
    "tracking",
    "track id",
    "message id",
    "ticket",
    "utm",
    "unsubscribe",
    "pixel",
    "color",
    "background",
    "border",
    "font-size",
    "padding",
    "margin",
    "radius",
    "hex",
    "rgb",
    "rgba",
    "received",
    "arc-",
    "dkim",
    "spf",
    "content-type",
    "mime",
    "boundary",
    "for <",
    "m=+",
    "timestamp",
    "message-id",
];

const CSS_COLOR_KEYWORDS: &[&str] = &["color", "background", "border", "rgb", "rgba", "font"];
const CSS_UNITS: &[&str] = &["px", "em", "rem", "%"];

struct CompiledRegexes {
    html_strip: Regex,
    otp: Regex,
    email_around: Regex,
}

fn regexes() -> Result<&'static CompiledRegexes, String> {
    static CACHE: OnceLock<Result<CompiledRegexes, String>> = OnceLock::new();
    CACHE
        .get_or_init(|| {
            Ok(CompiledRegexes {
                html_strip: Regex::new(
                    r"(?is)<!--.*?-->|<(?:style|script)\b[^>]*>.*?</(?:style|script)>",
                )
                .map_err(|e| format!("ValueError: {e}"))?,
                otp: Regex::new(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")
                    .map_err(|e| format!("ValueError: {e}"))?,
                email_around: Regex::new(r"(?i)[a-z0-9._%+-]\.?\d{6}@|@.*\d{6}[a-z0-9._-]")
                    .map_err(|e| format!("ValueError: {e}"))?,
            })
        })
        .as_ref()
        .map_err(Clone::clone)
}

#[inline]
fn is_py_whitespace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\x1c'..='\x1f')
}

#[inline]
fn is_py_word_char(ch: char) -> bool {
    ch == '_' || ch.is_alphanumeric()
}

fn has_any(haystack: &str, needles: &[&str]) -> bool {
    needles.iter().any(|n| haystack.contains(n))
}

fn left_n_chars(s: &str, byte_pos: usize, n: usize) -> usize {
    if s.is_ascii() {
        return byte_pos.saturating_sub(n);
    }
    let mut start = byte_pos;
    for _ in 0..n {
        let Some(ch) = s[..start].chars().next_back() else {
            break;
        };
        start -= ch.len_utf8();
    }
    start
}

fn right_n_chars(s: &str, byte_pos: usize, n: usize) -> usize {
    if s.is_ascii() {
        return (byte_pos + n).min(s.len());
    }
    let mut end = byte_pos;
    for _ in 0..n {
        let Some(ch) = s[end..].chars().next() else {
            break;
        };
        end += ch.len_utf8();
    }
    end
}

fn ends_with_ascii_ci(s: &str, kw: &str) -> bool {
    s.len() >= kw.len()
        && s.is_char_boundary(s.len() - kw.len())
        && s[s.len() - kw.len()..].eq_ignore_ascii_case(kw)
}

/// Exact non-compiling equivalent of:
/// `re.search(r"(?i)(color|background|border|rgb|rgba|font)[^\n]{0,20}#?" + code, context)`
fn matches_css_color_context(context: &str, code: &str) -> bool {
    let bytes = context.as_bytes();
    let mut search_from = 0usize;
    while let Some(rel) = context[search_from..].find(code) {
        let p = search_from + rel;
        let anchors = if p > 0 && bytes[p - 1] == b'#' {
            [Some(p), Some(p - 1)]
        } else {
            [Some(p), None]
        };
        for anchor in anchors.into_iter().flatten() {
            let mut cur = anchor;
            for step in 0..=20 {
                if CSS_COLOR_KEYWORDS
                    .iter()
                    .any(|kw| ends_with_ascii_ci(&context[..cur], kw))
                {
                    return true;
                }
                if step == 20 {
                    break;
                }
                let Some(ch) = context[..cur].chars().next_back() else {
                    break;
                };
                if ch == '\n' {
                    break;
                }
                cur -= ch.len_utf8();
            }
        }
        search_from = p + 1;
    }
    false
}

/// Exact non-compiling equivalent of:
/// `re.search(r"(?i)" + code + r"\s*(px|em|rem|%)\b", context)`
fn matches_css_unit_context(context: &str, code: &str) -> bool {
    let mut search_from = 0usize;
    while let Some(rel) = context[search_from..].find(code) {
        let p = search_from + rel;
        let mut q = p + code.len();
        for ch in context[q..].chars() {
            if is_py_whitespace(ch) {
                q += ch.len_utf8();
            } else {
                break;
            }
        }
        let rest = &context[q..];
        for unit in CSS_UNITS {
            if rest.len() >= unit.len()
                && rest.is_char_boundary(unit.len())
                && rest[..unit.len()].eq_ignore_ascii_case(unit)
            {
                let next_is_word = rest[unit.len()..]
                    .chars()
                    .next()
                    .is_some_and(is_py_word_char);
                let boundary_ok = if *unit == "%" {
                    next_is_word
                } else {
                    !next_is_word
                };
                if boundary_ok {
                    return true;
                }
            }
        }
        search_from = p + 1;
    }
    false
}

fn looks_fake_lowered(lowered: &str) -> bool {
    if has_any(lowered, OTP_CONTEXT_MARKERS) {
        return false;
    }
    has_any(lowered, FAKE_OTP_CONTEXT_MARKERS)
}

fn extract_str(input: &str) -> Result<String, String> {
    if input.is_empty() {
        return Ok(String::new());
    }
    let rx = regexes()?;
    let text: Cow<'_, str> = if input.contains('<') {
        rx.html_strip.replace_all(input, " ")
    } else {
        Cow::Borrowed(input)
    };

    let mut best_score = -1i32;
    let mut best_code: Option<&str> = None;

    for caps in rx.otp.captures_iter(&text) {
        let Some(m) = caps.get(2) else {
            continue;
        };
        let code = m.as_str();
        let (start, end) = (m.start(), m.end());

        let around = &text[left_n_chars(&text, start, 3)..right_n_chars(&text, end, 3)];
        if rx.email_around.is_match(around) {
            continue;
        }
        let char_before = text[..start].chars().next_back();
        let char_after = text[end..].chars().next();
        if char_before.is_some_and(|c| matches!(c, 'a'..='f' | 'A'..='F' | '0'..='9' | '-')) {
            continue;
        }
        if char_after.is_some_and(|c| matches!(c, 'a'..='f' | 'A'..='F' | '0'..='9' | '-')) {
            continue;
        }
        if char_before == Some('#') {
            continue;
        }

        let before = &text[left_n_chars(&text, start, 2)..start];
        let after = &text[end..right_n_chars(&text, end, 2)];
        let context = &text[left_n_chars(&text, start, 80)..right_n_chars(&text, end, 80)];
        let context_lc = context.to_lowercase();
        let before_lc = text[left_n_chars(&text, start, 60)..start].to_lowercase();

        if has_any(&before_lc, FAKE_OTP_CONTEXT_MARKERS)
            && !has_any(&before_lc, OTP_CONTEXT_MARKERS)
        {
            continue;
        }
        if looks_fake_lowered(&context_lc) {
            continue;
        }
        if matches_css_color_context(context, code) || matches_css_unit_context(context, code) {
            continue;
        }

        let mut score = 0i32;
        if has_any(&context_lc, OTP_CONTEXT_MARKERS) {
            score += 10;
        }
        if has_any(&context_lc, SECONDARY_CONTEXT_MARKERS) {
            score += 2;
        }
        if before.chars().any(|c| !is_py_whitespace(c))
            || after.chars().any(|c| !is_py_whitespace(c))
        {
            score += 1;
        }
        if score > best_score {
            best_score = score;
            best_code = Some(code);
        }
    }
    Ok(best_code.map(str::to_string).unwrap_or_default())
}

fn value_to_lower_str(value: Value) -> String {
    match value {
        Value::String(s) => s.to_lowercase(),
        Value::Null | Value::Bool(false) => String::new(),
        Value::Bool(true) => "true".to_string(),
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            String::new()
        }
        Value::Number(n) => n.to_string().to_lowercase(),
        Value::Array(ref a) if a.is_empty() => String::new(),
        Value::Object(ref m) if m.is_empty() => String::new(),
        other => other.to_string().to_lowercase(),
    }
}

/// Port of `_extract_otp_from_text` (`sms_tool/mailbox/mail_otp.py:46`).
pub fn _extract_otp_from_text(text: serde_json::Value) -> Result<String, String> {
    match text {
        Value::String(s) => extract_str(&s),
        Value::Null | Value::Bool(false) => Ok(String::new()),
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            Ok(String::new())
        }
        Value::Array(ref a) if a.is_empty() => Ok(String::new()),
        Value::Object(ref m) if m.is_empty() => Ok(String::new()),
        _ => Err("TypeError: expected string".to_string()),
    }
}

/// Port of `_looks_fake_otp_context` (`sms_tool/mailbox/mail_otp.py:195`).
pub fn _looks_fake_otp_context(lowered_text: serde_json::Value) -> Result<bool, String> {
    Ok(looks_fake_lowered(&value_to_lower_str(lowered_text)))
}

/// Port of `_message_looks_fake_otp_noise` (`sms_tool/mailbox/mail_otp.py:202`).
pub fn _message_looks_fake_otp_noise(text: serde_json::Value) -> Result<bool, String> {
    Ok(looks_fake_lowered(&value_to_lower_str(text)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_real_otp_and_rejects_css_hex_and_tracking_noise() {
        let html = r#"<style>.a{color:#123456;font-size:14px}</style><!-- tracking 998877 -->
        Hello user123456@example.com, your ChatGPT verification code is <b>482917</b>.
        <span style="color:#334455">margin 112233px tracking 776655</span>"#;
        assert_eq!(_extract_otp_from_text(json!(html)).expect("otp"), "482917");
        assert_eq!(
            _extract_otp_from_text(json!(r#"{"value":"453831","remark":"ChatGPT OTP"}"#))
                .expect("cfworker"),
            "453831"
        );
        assert_eq!(
            _extract_otp_from_text(json!("您的 OpenAI 验证码是 738291")).expect("zh"),
            "738291"
        );
        assert_eq!(
            _extract_otp_from_text(json!("width: 123456px; code 654321")).expect("px"),
            "654321"
        );
        assert!(_looks_fake_otp_context(json!("tracking id 123456")).expect("fake"));
        assert!(
            !_looks_fake_otp_context(json!("tracking id; verification code 123456")).expect("ok")
        );
    }
}
