//! Rust port of `_impersonate_version` from `sms_tool/sentinel/runner.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;

fn unicode_ver_re() -> Result<&'static Regex, String> {
    static RE: OnceLock<Result<Regex, String>> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\A[a-z]+(\d+)").map_err(|e| format!("ValueError: {e}")))
        .as_ref()
        .map_err(Clone::clone)
}

pub fn _impersonate_version(impersonate: Option<String>) -> Result<String, String> {
    let raw = impersonate.unwrap_or_default();
    if raw.is_ascii() {
        let bytes = raw.as_bytes();
        let mut i = 0;
        while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
            i += 1;
        }
        if i == 0 {
            return Ok(String::new());
        }
        let ver_start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        return Ok(if i > ver_start {
            raw[ver_start..i].to_string()
        } else {
            String::new()
        });
    }
    let lower = raw.to_lowercase();
    let re = unicode_ver_re()?;
    Ok(re
        .captures(&lower)
        .and_then(|c| c.get(1))
        .map_or_else(String::new, |m| m.as_str().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_leading_impersonate_major_version() {
        assert_eq!(
            _impersonate_version(Some("chrome152".into())),
            Ok("152".into())
        );
        assert_eq!(
            _impersonate_version(Some("safari18_4_ios".into())),
            Ok("18".into())
        );
        assert_eq!(
            _impersonate_version(Some("Firefox135".into())),
            Ok("135".into())
        );
        assert_eq!(_impersonate_version(Some("123".into())), Ok(String::new()));
        assert_eq!(_impersonate_version(None), Ok(String::new()));
    }
}
