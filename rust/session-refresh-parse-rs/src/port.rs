//! Rust port of `_has_session_cookie` from `sms_tool/session/refresh.py`.

#![forbid(unsafe_code)]

const SESSION_COOKIE_PREFIX: &str = "__Secure-next-auth.session-token";
const SESSION_COOKIE_CHUNK_PREFIX: &str = "__Secure-next-auth.session-token.";

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

pub fn _has_session_cookie(cookie_header: Option<String>) -> Result<bool, String> {
    let raw = cookie_header.unwrap_or_default();
    if raw.is_empty() {
        return Ok(false);
    }
    for item in raw.split(';') {
        if let Some((lhs, _)) = item.split_once('=') {
            let name = py_trim(lhs);
            if name == SESSION_COOKIE_PREFIX || name.starts_with(SESSION_COOKIE_CHUNK_PREFIX) {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_single_and_chunked_session_cookies() {
        assert!(!_has_session_cookie(None).unwrap());
        assert!(!_has_session_cookie(Some("oai-did=abc; __cf_bm=xyz".into())).unwrap());
        assert!(!_has_session_cookie(Some("__Secure-next-auth.session-token".into())).unwrap());
        assert!(
            _has_session_cookie(Some(
                "oai-did=1; __Secure-next-auth.session-token=tok".into()
            ))
            .unwrap()
        );
        assert!(
            _has_session_cookie(Some(
                "a=1;  __Secure-next-auth.session-token.0 = part0; b=2".into()
            ))
            .unwrap()
        );
        assert!(
            !_has_session_cookie(Some("__Secure-next-auth.session-token2=nope".into())).unwrap()
        );
    }
}
