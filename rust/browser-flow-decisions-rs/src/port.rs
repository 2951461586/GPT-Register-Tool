//! Rust port of `needs_chat_base_navigation` from
//! `sms_tool/registration_drivers/browser_flow/decisions.py`.

#![forbid(unsafe_code)]

use std::borrow::Cow;

fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(q);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
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
    out.push(q);
    out
}

fn py_urlsplit_host(url: &str) -> Result<String, String> {
    let trimmed = url.trim_start_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: Cow<'_, str> = if trimmed.bytes().any(|b| matches!(b, b'\t' | b'\r' | b'\n')) {
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
    let rest = match s.find(':') {
        Some(i)
            if i > 0
                && s.as_bytes()[0].is_ascii_alphabetic()
                && s.as_bytes()[..i]
                    .iter()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.')) =>
        {
            &s[i + 1..]
        }
        _ => s,
    };
    let rest = rest.split_once('#').map_or(rest, |(a, _)| a);
    let rest = rest.split_once('?').map_or(rest, |(a, _)| a);
    let Some(after) = rest.strip_prefix("//") else {
        return Ok(String::new());
    };
    let netloc = &after[..after.find('/').unwrap_or(after.len())];
    if netloc.contains('[') != netloc.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".into());
    }
    let hp = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    if let Some(inner) = hp.strip_prefix('[') {
        let Some(close) = inner.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".into());
        };
        let host = inner[..close].to_lowercase();
        let addr = host.split_once('%').map_or(host.as_str(), |(a, _)| a);
        if addr.parse::<std::net::Ipv4Addr>().is_ok() {
            return Err("ValueError: An IPv4 address cannot be in brackets".into());
        }
        if addr.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(format!(
                "ValueError: {} does not appear to be an IPv4 or IPv6 address",
                py_repr(&host)
            ));
        }
        Ok(host)
    } else if hp.contains('[') || hp.contains(']') {
        Err("ValueError: Invalid IPv6 URL".into())
    } else {
        let h = hp.split_once(':').map_or(hp, |(h, _)| h);
        Ok(if h.is_ascii() {
            h.to_ascii_lowercase()
        } else {
            h.to_lowercase()
        })
    }
}

pub fn needs_chat_base_navigation(
    chat_base: Option<String>,
    page_url: Option<String>,
) -> Result<bool, String> {
    let host = py_urlsplit_host(&chat_base.unwrap_or_default())?;
    if host.is_empty() {
        return Ok(false);
    }
    let page = page_url.unwrap_or_default();
    if host.is_ascii() && page.is_ascii() {
        let hb = host.as_bytes();
        return Ok(!page
            .as_bytes()
            .windows(hb.len())
            .any(|w| w.eq_ignore_ascii_case(hb)));
    }
    Ok(!page.to_lowercase().contains(&host))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_when_page_url_differs_from_chat_host() {
        assert_eq!(
            needs_chat_base_navigation(
                Some("https://chatgpt.com".into()),
                Some("https://auth.openai.com/log-in".into())
            ),
            Ok(true)
        );
        assert_eq!(
            needs_chat_base_navigation(
                Some("https://CHATGPT.COM/".into()),
                Some("https://chatgpt.com/c/1".into())
            ),
            Ok(false)
        );
        assert_eq!(
            needs_chat_base_navigation(
                Some("  \thttps://chatgpt.com\r\n ".into()),
                Some("https://CHATGPT.COM/".into())
            ),
            Ok(true)
        );
        assert_eq!(
            needs_chat_base_navigation(Some("".into()), Some("about:blank".into())),
            Ok(false)
        );
    }
}
