//! Rust port of HTML/form parsers in `sms_tool/paypal/reverse_protocol.py`.

#![forbid(unsafe_code)]

use regex::Regex;
use serde_json::{Map, Value};
use std::sync::LazyLock;

static RE_FORM_ACTION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)<form[^>]*action=["']([^"']*)["']"#).unwrap());
static RE_FORM_BLOCK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<form[^>]*>(.*?)</form>").unwrap());
static RE_SUBMIT_HINT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:agree|pay\s*now|continue|submit|confirm|authorize)").unwrap()
});
static RE_HIDDEN_INPUT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)<input[^>]*type=["']hidden["'][^>]*>"#).unwrap());
static RE_ANY_INPUT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)<input[^>]*>").unwrap());
static RE_SELECT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<select[^>]*>.*?</select>").unwrap());
static RE_OPTION_SELECTED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)<option[^>]*selected[^>]*value=["']([^"']*)["']"#).unwrap());
static RE_OPTION_FIRST: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)<option[^>]*value=["']([^"']*)["']"#).unwrap());
static RE_ATTR_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)name=["']([^"']*)["']"#).unwrap());
static RE_ATTR_VALUE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)value=["']([^"']*)["']"#).unwrap());
static RE_ATTR_TYPE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)type=["']([^"']*)["']"#).unwrap());
static RE_CHALLENGE_ACTION_1: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)<form[^>]*name=["']?challenge["']?[^>]*action=["']([^"']+)["']"#).unwrap()
});
static RE_CHALLENGE_ACTION_2: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)action=["']([^"']*(?:challenge|auth)[^"']*)["']"#).unwrap());
static RE_CHALLENGE_FORM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?is)<form[^>]*name=["']?challenge["']?[^>]*>(.*?)</form>"#).unwrap()
});

const SIGNUP_FIELD_NAMES: [&str; 4] = ["email", "login_email", "signup_email", "login_emailcopy"];

fn validate_netloc_brackets(netloc: &str) -> Result<(), String> {
    if netloc.is_empty() {
        return Ok(());
    }
    let hostinfo = match netloc.rsplit_once('@') {
        Some((_, right)) => right,
        None => netloc,
    };
    let has_open = hostinfo.contains('[');
    let has_close = hostinfo.contains(']');
    if has_open != has_close {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    if has_open {
        let Some(after_open) = hostinfo.strip_prefix('[') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let Some(close) = after_open.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let host = &after_open[..close];
        let ip_part = host.split('%').next().unwrap_or(host);
        if ip_part.parse::<std::net::Ipv4Addr>().is_ok()
            || ip_part.parse::<std::net::Ipv6Addr>().is_err()
        {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        }
    }
    Ok(())
}

fn urlparse_scheme_netloc(url: &str) -> Result<(String, String), String> {
    let cleaned: String = url
        .trim_start_matches(|c: char| (c as u32) <= 0x20)
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
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => &after[..pos],
            None => after,
        }
    } else {
        ""
    };
    validate_netloc_brackets(netloc)?;
    Ok((scheme, netloc.to_string()))
}

#[inline]
fn cap1<'a>(re: &Regex, text: &'a str) -> &'a str {
    re.captures(text)
        .and_then(|c| c.get(1))
        .map(|m| m.as_str())
        .unwrap_or("")
}

fn form_action(form_html: &str, current_url: &str) -> Result<String, String> {
    if let Some(m) = RE_FORM_ACTION.captures(form_html).and_then(|c| c.get(1)) {
        let action = m.as_str();
        if action.starts_with('/') {
            let (scheme, netloc) = urlparse_scheme_netloc(current_url)?;
            return Ok(format!("{scheme}://{netloc}{action}"));
        }
        if action.starts_with("http") {
            return Ok(action.to_string());
        }
    }
    Ok(current_url.to_string())
}

fn hidden_fields_map(form_html: &str) -> Map<String, Value> {
    let mut fields = Map::new();
    for m in RE_HIDDEN_INPUT.find_iter(form_html) {
        let tag = m.as_str();
        let name = cap1(&RE_ATTR_NAME, tag);
        if !name.is_empty() {
            let value = cap1(&RE_ATTR_VALUE, tag);
            fields.insert(name.to_string(), Value::String(value.to_string()));
        }
    }
    fields
}

fn input_fields_map(form_html: &str) -> Map<String, Value> {
    let mut fields = Map::new();
    for m in RE_ANY_INPUT.find_iter(form_html) {
        let tag = m.as_str();
        let name = cap1(&RE_ATTR_NAME, tag);
        if name.is_empty() {
            continue;
        }
        let input_type = cap1(&RE_ATTR_TYPE, tag).to_lowercase();
        if matches!(input_type.as_str(), "submit" | "button" | "image") {
            continue;
        }
        let value = cap1(&RE_ATTR_VALUE, tag);
        fields.insert(name.to_string(), Value::String(value.to_string()));
    }
    fields
}

fn select_fields_map(form_html: &str) -> Map<String, Value> {
    let mut fields = Map::new();
    for m in RE_SELECT.find_iter(form_html) {
        let tag = m.as_str();
        let name = cap1(&RE_ATTR_NAME, tag);
        if name.is_empty() {
            continue;
        }
        if let Some(sel) = RE_OPTION_SELECTED.captures(tag).and_then(|c| c.get(1)) {
            fields.insert(name.to_string(), Value::String(sel.as_str().to_string()));
            continue;
        }
        let first = cap1(&RE_OPTION_FIRST, tag);
        fields.insert(name.to_string(), Value::String(first.to_string()));
    }
    fields
}

pub fn extract_hidden_fields(form_html: String) -> Result<serde_json::Value, String> {
    Ok(Value::Object(hidden_fields_map(&form_html)))
}

pub fn extract_input_fields(form_html: String) -> Result<serde_json::Value, String> {
    Ok(Value::Object(input_fields_map(&form_html)))
}

pub fn extract_select_fields(form_html: String) -> Result<serde_json::Value, String> {
    Ok(Value::Object(select_fields_map(&form_html)))
}

pub fn find_signup_form(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    for m in RE_FORM_BLOCK.find_iter(&html) {
        let form_html = m.as_str();
        let form_lower = form_html.to_lowercase();
        if SIGNUP_FIELD_NAMES
            .iter()
            .any(|name| form_lower.contains(name))
        {
            let action = form_action(form_html, &current_url)?;
            let mut fields = hidden_fields_map(form_html);
            fields.extend(input_fields_map(form_html));
            fields.extend(select_fields_map(form_html));
            return Ok(Some((action, Value::Object(fields))));
        }
    }
    Ok(None)
}

pub fn find_submit_form(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    for m in RE_FORM_BLOCK.find_iter(&html) {
        let form_html = m.as_str();
        if RE_SUBMIT_HINT.is_match(form_html) {
            let action = form_action(form_html, &current_url)?;
            let fields = hidden_fields_map(form_html);
            return Ok(Some((action, Value::Object(fields))));
        }
    }
    Ok(None)
}

pub fn extract_challenge_form(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    let action_raw = RE_CHALLENGE_ACTION_1
        .captures(&html)
        .and_then(|c| c.get(1))
        .or_else(|| RE_CHALLENGE_ACTION_2.captures(&html).and_then(|c| c.get(1)))
        .map(|m| m.as_str());
    let Some(raw) = action_raw else {
        return Ok(None);
    };
    let action = if raw.starts_with('/') {
        let (scheme, netloc) = urlparse_scheme_netloc(&current_url)?;
        format!("{scheme}://{netloc}{raw}")
    } else {
        raw.to_string()
    };
    let fields = match RE_CHALLENGE_FORM.captures(&html).and_then(|c| c.get(1)) {
        Some(inner) => hidden_fields_map(inner.as_str()),
        None => Map::new(),
    };
    Ok(Some((action, Value::Object(fields))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_form_fields_signup_submit_and_challenge() {
        let html = r#"<form name="challenge" action="/auth/verify"><input type="hidden" name="_csrf" value="tok1"><input type="text" name="email" value="a@b.c"><input type="submit" name="go" value="Continue"><select name="cc"><option value="GB">GB</option><option selected value="US">US</option></select></form>"#;
        assert_eq!(
            extract_hidden_fields(html.into()).unwrap(),
            json!({"_csrf": "tok1"})
        );
        assert_eq!(
            extract_input_fields(html.into()).unwrap(),
            json!({"_csrf": "tok1", "email": "a@b.c"})
        );
        assert_eq!(
            extract_select_fields(html.into()).unwrap(),
            json!({"cc": "US"})
        );
        assert_eq!(
            find_signup_form(html.into(), "https://www.paypal.com/signin".into()).unwrap(),
            Some((
                "https://www.paypal.com/auth/verify".to_string(),
                json!({"_csrf": "tok1", "email": "a@b.c", "cc": "US"})
            ))
        );
        assert_eq!(
            find_submit_form(html.into(), "https://www.paypal.com/signin".into()).unwrap(),
            Some((
                "https://www.paypal.com/auth/verify".to_string(),
                json!({"_csrf": "tok1"})
            ))
        );
        assert_eq!(
            extract_challenge_form(html.into(), "https://www.paypal.com/signin".into()).unwrap(),
            Some((
                "https://www.paypal.com/auth/verify".to_string(),
                json!({"_csrf": "tok1"})
            ))
        );
    }
}
