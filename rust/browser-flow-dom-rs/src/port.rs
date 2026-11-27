//! Rust port of `_is_openai_auth_url`, `_unexpected_identity_provider`,
//! `_session_context_closed`, and `_terminal_session_error` from
//! `sms_tool/registration_drivers/browser_flow/dom_fields.py`.

#![forbid(unsafe_code)]

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

/// Mirror `urllib.parse.urlsplit` structural validation and `.hostname` extraction.
fn py_urlsplit_scheme_host(url: &str) -> Result<(String, String), String> {
    let trimmed = url.trim_matches(|c: char| (c as u32) <= 0x20);
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
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        let split_at = after.find('/').unwrap_or(after.len());
        &after[..split_at]
    } else {
        ""
    };
    if netloc.contains('[') != netloc.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let hostport = if let Some((_, hp)) = netloc.rsplit_once('@') {
        hp
    } else {
        netloc
    };
    let hostname = if hostport.starts_with('[') {
        let Some(close) = hostport.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let host = hostport[1..close].to_lowercase();
        let addr_part = host.split_once('%').map_or(host.as_str(), |(a, _)| a);
        if addr_part.parse::<std::net::Ipv4Addr>().is_ok() {
            return Err("ValueError: An IPv4 address cannot be in brackets".to_string());
        }
        if addr_part.parse::<std::net::Ipv6Addr>().is_err() {
            let mut repr = String::new();
            py_str_repr(&host, &mut repr);
            return Err(format!(
                "ValueError: {repr} does not appear to be an IPv4 or IPv6 address"
            ));
        }
        host
    } else if hostport.contains('[') || hostport.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    } else if let Some((h, _)) = hostport.split_once(':') {
        if h.is_ascii() {
            h.to_ascii_lowercase()
        } else {
            h.to_lowercase()
        }
    } else if hostport.is_ascii() {
        hostport.to_ascii_lowercase()
    } else {
        hostport.to_lowercase()
    };
    Ok((scheme, hostname))
}

#[inline]
fn is_openai_host(host: &str) -> bool {
    host == "chatgpt.com"
        || host.ends_with(".chatgpt.com")
        || host == "openai.com"
        || host.ends_with(".openai.com")
}

pub fn _is_openai_auth_url(url: Option<String>) -> Result<bool, String> {
    let raw = url.unwrap_or_default();
    let (_, host) = py_urlsplit_scheme_host(&raw)?;
    Ok(is_openai_host(&host))
}

pub fn _unexpected_identity_provider(url: Option<String>) -> Result<bool, String> {
    let raw = url.unwrap_or_default();
    let (scheme, host) = py_urlsplit_scheme_host(&raw)?;
    Ok((scheme == "http" || scheme == "https") && !host.is_empty() && !is_openai_host(&host))
}

const CONTEXT_CLOSED_MARKERS: &[&str] = &[
    "target page, context or browser has been closed",
    "target closed",
    "context closed",
    "browser has been closed",
    "no such window",
    "invalid session id",
    "session deleted because of page crash",
    "nosuchwindowexception",
    "invalidsessionidexception",
    "targetclosederror",
];

pub fn _session_context_closed(value: Option<String>) -> Result<bool, String> {
    let raw = value.unwrap_or_default();
    let text = if raw.is_ascii() {
        raw.to_ascii_lowercase()
    } else {
        raw.to_lowercase()
    };
    Ok(CONTEXT_CLOSED_MARKERS.iter().any(|m| text.contains(m)))
}

pub fn _terminal_session_error(
    status: f64,
    error_marker: Option<String>,
) -> Result<String, String> {
    let raw = error_marker.unwrap_or_default();
    let mut marker = String::with_capacity(raw.len());
    if raw.is_ascii() {
        for b in raw.bytes() {
            if !matches!(b, b'_' | b'-' | b' ') {
                marker.push(b.to_ascii_lowercase() as char);
            }
        }
    } else {
        for ch in raw.to_lowercase().chars() {
            if !matches!(ch, '_' | '-' | ' ') {
                marker.push(ch);
            }
        }
    }
    if marker.contains("oauthaccountnotlinked") {
        return Ok("browser_session_account_not_linked".to_string());
    }
    if marker.contains("accessdenied") {
        return Ok("browser_session_access_denied".to_string());
    }
    if marker.contains("sessionrequired") {
        return Ok("browser_session_unauthorized".to_string());
    }
    if marker.contains("refreshaccesstokenerror") {
        return Ok("browser_session_token_refresh_failed".to_string());
    }
    if marker.contains("oauthcallback")
        || marker.contains("oauthcreateaccount")
        || marker.contains("emailcreateaccount")
    {
        return Ok("browser_session_oauth_callback_failed".to_string());
    }
    if status == 429.0 {
        return Ok("browser_session_rate_limited".to_string());
    }
    if status == 401.0 {
        return Ok("browser_session_unauthorized".to_string());
    }
    if status == 403.0 {
        return Ok("browser_session_forbidden".to_string());
    }
    if status == 404.0 || status == 405.0 {
        return Ok("browser_session_endpoint_unavailable".to_string());
    }
    Ok(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_checks_and_session_errors_match_python() {
        assert!(_is_openai_auth_url(Some("https://auth.openai.com/log-in".into())).unwrap());
        assert!(_is_openai_auth_url(Some("https://CHATGPT.COM/".into())).unwrap());
        assert!(!_is_openai_auth_url(Some("https://accounts.google.com/o/oauth2".into())).unwrap());
        assert!(
            _unexpected_identity_provider(Some("https://accounts.google.com/".into())).unwrap()
        );
        assert!(!_unexpected_identity_provider(Some("https://auth.openai.com/".into())).unwrap());
        assert!(!_unexpected_identity_provider(Some("about:blank".into())).unwrap());

        assert!(_session_context_closed(Some("TargetClosedError: Target closed".into())).unwrap());
        assert!(
            !_session_context_closed(Some("TimeoutError: waiting for selector".into())).unwrap()
        );

        assert_eq!(
            _terminal_session_error(200.0, Some("OAuth_Account-Not Linked".into())).unwrap(),
            "browser_session_account_not_linked"
        );
        assert_eq!(
            _terminal_session_error(429.0, Some(String::new())).unwrap(),
            "browser_session_rate_limited"
        );
        assert_eq!(
            _terminal_session_error(405.0, Some("other".into())).unwrap(),
            "browser_session_endpoint_unavailable"
        );
        assert_eq!(
            _terminal_session_error(200.0, Some("ok".into())).unwrap(),
            ""
        );
    }
}
