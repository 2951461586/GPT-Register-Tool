//! Rust port of `_session_token_from_cookie_header` in `sms_tool/session/builder.py`.

#![forbid(unsafe_code)]

use std::collections::BTreeMap;

const PREFIXES: [&str; 2] = [
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
];

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

#[inline]
fn parse_chunk_index(suffix: &str) -> Option<(usize, String)> {
    if suffix.is_empty() || !suffix.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let trimmed = suffix.trim_start_matches('0');
    let canonical = if trimmed.is_empty() { "0" } else { trimmed };
    Some((canonical.len(), canonical.to_string()))
}

pub fn _session_token_from_cookie_header(cookie_header: String) -> Result<String, String> {
    let mut chunks: BTreeMap<(usize, String), &str> = BTreeMap::new();
    for part in cookie_header.split(';') {
        let Some((name, value)) = py_trim(part).split_once('=') else {
            continue;
        };
        if value.is_empty() {
            continue;
        }
        for prefix in PREFIXES {
            if name == prefix {
                return Ok(value.to_string());
            }
            if let Some(rest) = name.strip_prefix(prefix)
                && let Some(suffix) = rest.strip_prefix('.')
                && let Some(key) = parse_chunk_index(suffix)
            {
                chunks.insert(key, value);
            }
        }
    }
    if chunks.is_empty() {
        return Ok(String::new());
    }
    let total_len: usize = chunks.values().map(|v| v.len()).sum();
    let mut out = String::with_capacity(total_len);
    for value in chunks.values() {
        out.push_str(value);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reassembles_chunked_and_direct_session_tokens() {
        let hdr = "a=1; __Secure-next-auth.session-token.1=BBB; __Secure-next-auth.session-token.0=AAA; __Secure-next-auth.session-token.10=CCC";
        assert_eq!(
            _session_token_from_cookie_header(hdr.to_string()).unwrap(),
            "AAABBBCCC"
        );
        let direct = "__Secure-next-auth.session-token.0=AAA; next-auth.session-token=DIRECT";
        assert_eq!(
            _session_token_from_cookie_header(direct.to_string()).unwrap(),
            "DIRECT"
        );
    }
}
