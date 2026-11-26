//! Rust port of `_is_existing_login_redirect` from
//! `sms_tool/transport/auth_wire.py`.

#![forbid(unsafe_code)]

use serde_json::Value;

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

fn urlparse_netloc_path(raw: &str) -> (String, String) {
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

    (netloc.to_lowercase(), path_without_params.to_string())
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

pub fn _is_existing_login_redirect(url: serde_json::Value) -> Result<bool, String> {
    let raw = url_or_empty(&url)?;
    let (host, parsed_path) = urlparse_netloc_path(raw);
    if !host.is_empty() && host != "auth.openai.com" && !host.ends_with(".auth.openai.com") {
        return Ok(false);
    }
    let path_raw = if parsed_path.is_empty() {
        raw
    } else {
        parsed_path.as_str()
    };
    if path_raw.is_empty() {
        return Ok(false);
    }
    let lower = path_raw.to_lowercase();
    let path = lower.trim_end_matches('/');
    Ok(matches!(path, "/log-in" | "/login")
        || path.starts_with("/log-in/")
        || path.starts_with("/login/"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn login_redirect_checks() {
        assert!(
            _is_existing_login_redirect(json!("https://auth.openai.com/log-in/password")).unwrap()
        );
        assert!(_is_existing_login_redirect(json!("/log-in")).unwrap());
        assert!(!_is_existing_login_redirect(json!("https://example.com/log-in")).unwrap());
        assert!(!_is_existing_login_redirect(json!("")).unwrap());
    }
}
