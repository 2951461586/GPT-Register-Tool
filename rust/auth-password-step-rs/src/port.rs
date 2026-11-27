//! Rust port of `_is_login_password_step` from
//! `sms_tool/auth_flow/password_step.py`.

#![forbid(unsafe_code)]

use serde_json::Value;
use std::borrow::Cow;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

#[inline]
fn uses_params_ascii_ci(scheme: &str) -> bool {
    scheme.is_empty()
        || scheme.eq_ignore_ascii_case("http")
        || scheme.eq_ignore_ascii_case("https")
        || scheme.eq_ignore_ascii_case("ftp")
        || scheme.eq_ignore_ascii_case("hdl")
        || scheme.eq_ignore_ascii_case("prospero")
        || scheme.eq_ignore_ascii_case("imap")
        || scheme.eq_ignore_ascii_case("shttp")
        || scheme.eq_ignore_ascii_case("rtsp")
        || scheme.eq_ignore_ascii_case("rtsps")
        || scheme.eq_ignore_ascii_case("rtspu")
        || scheme.eq_ignore_ascii_case("sip")
        || scheme.eq_ignore_ascii_case("sips")
        || scheme.eq_ignore_ascii_case("mms")
        || scheme.eq_ignore_ascii_case("sftp")
        || scheme.eq_ignore_ascii_case("tel")
}

#[inline]
fn ends_with_ascii_ci(haystack: &str, suffix: &str) -> bool {
    let hb = haystack.as_bytes();
    let sb = suffix.as_bytes();
    hb.len() >= sb.len() && hb[hb.len() - sb.len()..].eq_ignore_ascii_case(sb)
}

fn url_matches_password_step(url: &str) -> Result<bool, String> {
    let trimmed = url.trim_start_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: Cow<'_, str> = if trimmed
        .as_bytes()
        .iter()
        .any(|&b| matches!(b, b'\t' | b'\r' | b'\n'))
    {
        Cow::Owned(
            trimmed
                .chars()
                .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
                .collect(),
        )
    } else {
        Cow::Borrowed(trimmed)
    };
    let s = cleaned.as_ref();
    let (scheme, rest) = if let Some(idx) = s.find(':') {
        let cand = &s[..idx];
        let valid_scheme = !cand.is_empty()
            && cand.as_bytes()[0].is_ascii_alphabetic()
            && cand
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
        if valid_scheme {
            (cand, &s[idx + 1..])
        } else {
            ("", s)
        }
    } else {
        ("", s)
    };
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let (netloc, raw_path) = if let Some(after) = before_query.strip_prefix("//") {
        let split_at = after.find('/').unwrap_or(after.len());
        (&after[..split_at], &after[split_at..])
    } else {
        ("", before_query)
    };
    if netloc.contains('[') != netloc.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    if !ends_with_ascii_ci(netloc, "auth.openai.com") {
        return Ok(false);
    }
    let path = if uses_params_ascii_ci(scheme) && raw_path.contains(';') {
        if let Some(last_slash) = raw_path.rfind('/') {
            if let Some(rel_semi) = raw_path[last_slash..].find(';') {
                &raw_path[..last_slash + rel_semi]
            } else {
                raw_path
            }
        } else if let Some(semi) = raw_path.find(';') {
            &raw_path[..semi]
        } else {
            raw_path
        }
    } else {
        raw_path
    };
    let path_trimmed = path.trim_end_matches('/');
    Ok(ends_with_ascii_ci(path_trimmed, "/log-in/password")
        || ends_with_ascii_ci(path_trimmed, "/create-account/password"))
}

fn value_as_nonempty_string(v: &Value) -> Option<String> {
    match v {
        Value::Null | Value::Bool(false) => None,
        Value::Bool(true) => Some("True".to_string()),
        Value::Number(n) => {
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) {
                None
            } else {
                Some(n.to_string())
            }
        }
        Value::String(s) if s.is_empty() => None,
        Value::String(s) => Some(s.clone()),
        Value::Array(items) if !items.is_empty() => Some("[...]".to_string()),
        Value::Object(map) if !map.is_empty() => Some("{...}".to_string()),
        _ => None,
    }
}

fn login_password_page_type(payload: Option<&Value>) -> String {
    let Some(Value::Object(map)) = payload else {
        return String::new();
    };
    let from_page = map
        .get("page")
        .and_then(|p| p.as_object())
        .and_then(|page| page.get("type"))
        .and_then(value_as_nonempty_string);
    let raw = from_page
        .or_else(|| map.get("page_type").and_then(value_as_nonempty_string))
        .unwrap_or_default();
    let trimmed = py_trim(&raw);
    if trimmed.is_ascii() {
        trimmed.to_ascii_lowercase()
    } else {
        trimmed.to_lowercase()
    }
}

pub fn _is_login_password_step(
    url: Option<String>,
    payload: Option<serde_json::Value>,
) -> Result<bool, String> {
    let raw_url = url.unwrap_or_default();
    if url_matches_password_step(&raw_url)? {
        return Ok(true);
    }
    let page_type = login_password_page_type(payload.as_ref());
    Ok(matches!(
        page_type.as_str(),
        "password" | "login_password" | "create_account_password"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_password_step_url_and_payload() {
        assert!(
            _is_login_password_step(Some("https://auth.openai.com/log-in/password".into()), None)
                .unwrap()
        );
        assert!(
            _is_login_password_step(
                Some("https://auth.openai.com/create-account/password///;s=1?x=1".into()),
                None
            )
            .unwrap()
        );
        assert!(
            !_is_login_password_step(
                Some("https://auth.openai.com/email-verification".into()),
                None
            )
            .unwrap()
        );
        assert!(
            _is_login_password_step(
                Some(String::new()),
                Some(json!({"page": {"type": "PASSWORD"}}))
            )
            .unwrap()
        );
        assert!(
            _is_login_password_step(None, Some(json!({"page_type": "login_password"}))).unwrap()
        );
        assert!(
            !_is_login_password_step(None, Some(json!({"page": {"type": "email_otp"}}))).unwrap()
        );
    }
}
