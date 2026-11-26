//! Rust port of URL step predicates and SO-token flow guards from
//! `sms_tool/auth_flow/steps.py`.

#![forbid(unsafe_code)]

use serde_json::Value;

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

fn uses_params_scheme(scheme: &str) -> bool {
    matches!(
        scheme,
        "" | "ftp"
            | "hdl"
            | "prospero"
            | "http"
            | "imap"
            | "https"
            | "shttp"
            | "rtsp"
            | "rtsps"
            | "rtspu"
            | "sip"
            | "sips"
            | "mms"
            | "sftp"
            | "tel"
    )
}

/// Exact `urllib.parse.urlparse(url)` netloc and path extraction (lowercased, path trailing `/` trimmed).
fn urlparse_host_and_path(raw: &str) -> (String, String) {
    let trimmed = raw.trim_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: String = if trimmed.bytes().any(|b| matches!(b, b'\t' | b'\r' | b'\n')) {
        trimmed
            .chars()
            .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
            .collect()
    } else {
        String::new()
    };
    let s = if cleaned.is_empty() && !trimmed.is_empty() {
        trimmed
    } else {
        cleaned.as_str()
    };

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

    let (netloc, raw_path) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };

    // Match `urllib.parse._splitparams` when scheme uses params.
    let path_without_params = if uses_params_scheme(&scheme) && raw_path.contains(';') {
        if let Some(slash_pos) = raw_path.rfind('/') {
            let tail = &raw_path[slash_pos..];
            if let Some(semi) = tail.find(';') {
                &raw_path[..slash_pos + semi]
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

    let host = netloc.to_lowercase();
    let path = path_without_params.trim_end_matches('/').to_lowercase();
    (host, path)
}

fn url_or_empty(v: &Value) -> Result<&str, String> {
    match v {
        Value::Null | Value::Bool(false) => Ok(""),
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            Ok("")
        }
        Value::Array(a) if a.is_empty() => Ok(""),
        Value::Object(o) if o.is_empty() => Ok(""),
        Value::String(s) => Ok(s.as_str()),
        _ => Err("TypeError: expected str or falsy value".to_string()),
    }
}

pub fn _is_chatgpt_auth_login_landing(url: serde_json::Value) -> Result<bool, String> {
    let raw = url_or_empty(&url)?;
    let (host, path) = urlparse_host_and_path(raw);
    Ok(host.ends_with("chatgpt.com") && matches!(path.as_str(), "/auth/login" | "/auth/log-in"))
}

pub fn _is_signup_password_page(url: serde_json::Value) -> Result<bool, String> {
    let raw = url_or_empty(&url)?;
    let (host, path) = urlparse_host_and_path(raw);
    Ok(host.ends_with("auth.openai.com") && path.ends_with("/create-account/password"))
}

pub fn _is_signup_password_step(url: serde_json::Value) -> Result<bool, String> {
    let raw = url_or_empty(&url)?;
    let (host, path) = urlparse_host_and_path(raw);
    if !host.ends_with("auth.openai.com") {
        return Ok(false);
    }
    Ok(path.ends_with("/create-account/password") || path.ends_with("/create-account"))
}

pub fn _is_email_verification_step(url: serde_json::Value) -> Result<bool, String> {
    let raw = url_or_empty(&url)?;
    let (host, path) = urlparse_host_and_path(raw);
    if !host.ends_with("auth.openai.com") {
        return Ok(false);
    }
    Ok(path.ends_with("/email-verification") || path.contains("email-otp"))
}

fn so_token_flow_inner(token: &Value) -> String {
    let raw = py_str_or_empty(token);
    if raw.is_empty() {
        return String::new();
    }
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) else {
        return String::new();
    };
    let Some(flow_val) = map.get("flow") else {
        return String::new();
    };
    py_trim(&py_str_or_empty(flow_val)).to_string()
}

pub fn so_token_flow(token: serde_json::Value) -> Result<String, String> {
    Ok(so_token_flow_inner(&token))
}

pub fn same_flow_so_token(
    token: serde_json::Value,
    expected_flow: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let declared = so_token_flow_inner(&token);
    let expected_raw = py_str_or_empty(&expected_flow);
    let expected = py_trim(&expected_raw);
    if !declared.is_empty() && declared != expected {
        return Ok(Value::String(String::new()));
    }
    Ok(token)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn url_step_predicates() {
        assert!(_is_chatgpt_auth_login_landing(json!("https://chatgpt.com/auth/login/")).unwrap());
        assert!(
            _is_signup_password_page(json!("https://auth.openai.com/create-account/password?x=1"))
                .unwrap()
        );
        assert!(_is_signup_password_step(json!("https://auth.openai.com/create-account")).unwrap());
        assert!(
            _is_email_verification_step(json!("https://auth.openai.com/email-verification"))
                .unwrap()
        );
        assert!(
            _is_email_verification_step(json!("https://auth.openai.com/api/email-otp/send"))
                .unwrap()
        );
    }

    #[test]
    fn so_token_flow_guards() {
        let tok = json!(r#"{"p":"abc","flow":"authorize_continue"}"#);
        assert_eq!(so_token_flow(tok.clone()).unwrap(), "authorize_continue");
        assert_eq!(
            same_flow_so_token(tok.clone(), json!("authorize_continue")).unwrap(),
            tok
        );
        assert_eq!(
            same_flow_so_token(tok, json!("oauth_create_account")).unwrap(),
            json!("")
        );
        let undeclared = json!(r#"{"p":"abc"}"#);
        assert_eq!(
            same_flow_so_token(undeclared.clone(), json!("authorize_continue")).unwrap(),
            undeclared
        );
    }
}
