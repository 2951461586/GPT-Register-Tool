//! Rust port of hot fingerprint header helpers in `sms_tool/transport/auth_headers.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;

const ACCEPT_ENCODING_ZSTD: &str = "gzip, deflate, br, zstd";
const ACCEPT_ENCODING_NO_ZSTD: &str = "gzip, deflate, br";

#[inline]
fn is_py_ws(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

fn browser_version_re() -> Result<&'static Regex, String> {
    static RE: OnceLock<Result<Regex, String>> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\A[a-z]+(\d+)").map_err(|e| format!("ValueError: {e}")))
        .as_ref()
        .map_err(Clone::clone)
}

fn browser_family_inner(impersonate: &str) -> &'static str {
    let lower = impersonate.to_lowercase();
    if lower.starts_with("firefox") {
        "firefox"
    } else if lower.starts_with("chrome") {
        "chrome"
    } else if lower.starts_with("safari") {
        "safari"
    } else if lower.starts_with("edge") {
        "edge"
    } else {
        "chrome"
    }
}

/// Exact Python `int(s)` base-10 parser (supports sign, underscores, Unicode Nd digits).
/// Returns `Ok(Some(i128))` for fits-in-i128, `Ok(None)` for valid huge int (positive/negative sign bool).
fn parse_py_int_base10(raw: &str) -> Option<(bool, Option<i128>)> {
    let s = raw.trim_matches(is_py_ws);
    if s.is_empty() {
        return None;
    }
    let (neg, body) = if let Some(r) = s.strip_prefix('-') {
        (true, r)
    } else if let Some(r) = s.strip_prefix('+') {
        (false, r)
    } else {
        (false, s)
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') {
        return None;
    }
    let mut digits = String::with_capacity(body.len());
    let mut prev_us = false;
    for ch in body.chars() {
        if ch == '_' {
            if prev_us {
                return None;
            }
            prev_us = true;
            continue;
        }
        prev_us = false;
        let d = unicode_decimal_digit(ch)?;
        digits.push((b'0' + d) as char);
    }
    if digits.is_empty() {
        return None;
    }
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        return Some((false, Some(0)));
    }
    let parsed = if neg {
        format!("-{trimmed}").parse::<i128>().ok()
    } else {
        trimmed.parse::<i128>().ok()
    };
    Some((neg, parsed))
}

#[inline]
fn unicode_decimal_digit(ch: char) -> Option<u8> {
    if ch.is_ascii_digit() {
        return Some((ch as u32 - '0' as u32) as u8);
    }
    // Match Unicode General Category Nd decimal digits (Python `int()` & `\d`).
    let cp = ch as u32;
    const RANGES: &[u32] = &[
        0x0660, 0x06F0, 0x07C0, 0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66, 0x0CE6,
        0x0D66, 0x0DE6, 0x0E50, 0x0ED0, 0x0F20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0,
        0x1A80, 0x1A90, 0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0,
        0xAA50, 0xABF0, 0xFF10, 0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0,
        0x11450, 0x114D0, 0x11650, 0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0,
        0x11F50, 0x16A60, 0x16AC0, 0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140,
        0x1E2F0, 0x1E4F0, 0x1E950, 0x1FBF0,
    ];
    for &base in RANGES {
        if cp >= base && cp < base + 10 {
            return Some((cp - base) as u8);
        }
    }
    None
}

fn safari_version_gte(impersonate: &str, floor_major: i128, floor_minor: i128) -> bool {
    let lower = impersonate.trim_matches(is_py_ws).to_lowercase();
    let token = lower.strip_prefix("safari").unwrap_or(&lower);
    let token = token.strip_suffix("_ios").unwrap_or(token);
    if token.is_empty() {
        return false;
    }
    let (major_s, minor_s) = if let Some((maj, min)) = token.split_once('_') {
        (maj, if min.is_empty() { "0" } else { min })
    } else if token.chars().count() > 2 {
        let split_byte = token
            .char_indices()
            .nth(2)
            .map(|(idx, _)| idx)
            .unwrap_or(token.len());
        (&token[..split_byte], &token[split_byte..])
    } else {
        (token, "0")
    };
    let Some((maj_neg, maj_opt)) = parse_py_int_base10(major_s) else {
        return false;
    };
    let Some((min_neg, min_opt)) = parse_py_int_base10(minor_s) else {
        return false;
    };
    match (maj_opt, min_opt) {
        (Some(maj), Some(min)) => (maj, min) >= (floor_major, floor_minor),
        (None, _) => !maj_neg,
        (Some(maj), None) => {
            if maj != floor_major {
                maj > floor_major
            } else {
                !min_neg
            }
        }
    }
}

/// Port of `_browser_version` (`sms_tool/transport/auth_headers.py:380`).
pub fn _browser_version(impersonate: String) -> Result<String, String> {
    if impersonate.is_ascii() {
        let bytes = impersonate.as_bytes();
        let mut i = 0;
        while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
            i += 1;
        }
        if i == 0 || i >= bytes.len() || !bytes[i].is_ascii_digit() {
            return Ok(String::new());
        }
        let start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        return Ok(impersonate[start..i].to_string());
    }
    let lower = impersonate.to_lowercase();
    let re = browser_version_re()?;
    Ok(re
        .captures(&lower)
        .and_then(|c| c.get(1))
        .map(|m| m.as_str().to_string())
        .unwrap_or_default())
}

/// Port of `_full_version_list` (`sms_tool/transport/auth_headers.py:939`).
pub fn _full_version_list(sec_ch_ua: String, full_version: String) -> Result<String, String> {
    let text = sec_ch_ua.trim_matches(is_py_ws);
    if text.is_empty() {
        return Ok(String::new());
    }
    let real_build = full_version.trim_matches(is_py_ws);
    let mut parts: Vec<String> = Vec::new();
    for chunk in text.split(',') {
        let Some((brand_raw, version_raw)) = chunk.split_once(";v=") else {
            continue;
        };
        let brand = brand_raw.trim_matches(is_py_ws);
        let major = version_raw.trim_matches(is_py_ws).trim_matches('"');
        if brand.is_empty() || major.is_empty() {
            continue;
        }
        let brand_name = brand.trim_matches('"').to_lowercase();
        let full = if major.bytes().filter(|&b| b == b'.').count() == 3 {
            major.to_string()
        } else if !real_build.is_empty()
            && matches!(brand_name.as_str(), "chromium" | "google chrome")
            && real_build.starts_with(major)
            && real_build.as_bytes().get(major.len()) == Some(&b'.')
        {
            real_build.to_string()
        } else {
            format!("{major}.0.0.0")
        };
        parts.push(format!("{brand};v=\"{full}\""));
    }
    Ok(parts.join(", "))
}

/// Port of `accept_encoding_for` (`sms_tool/transport/auth_headers.py:1075`).
pub fn accept_encoding_for(impersonate: String) -> Result<String, String> {
    let family = browser_family_inner(&impersonate);
    if family == "safari" {
        return Ok(if safari_version_gte(&impersonate, 26, 3) {
            ACCEPT_ENCODING_ZSTD
        } else {
            ACCEPT_ENCODING_NO_ZSTD
        }
        .to_string());
    }
    let floor_major: i128 = match family {
        "firefox" => 126,
        "chrome" | "edge" => 123,
        _ => return Ok(ACCEPT_ENCODING_NO_ZSTD.to_string()),
    };
    let major = _browser_version(impersonate)?;
    let supported = match parse_py_int_base10(&major) {
        None => true,
        Some((_, Some(v))) => v >= floor_major,
        Some((neg, None)) => !neg,
    };
    Ok(if supported {
        ACCEPT_ENCODING_ZSTD
    } else {
        ACCEPT_ENCODING_NO_ZSTD
    }
    .to_string())
}

fn netloc_has_invalid_brackets(netloc: &str) -> bool {
    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if !has_open && !has_close {
        return false;
    }
    if has_open != has_close {
        return true;
    }
    let Some(open) = netloc.find('[') else {
        return true;
    };
    let Some(close) = netloc.find(']') else {
        return true;
    };
    if close < open {
        return true;
    }
    // Reject extra '[' or ']' or invalid chars inside brackets (Python 3.12 urlsplit).
    if netloc[open + 1..].contains('[') || netloc[close + 1..].contains(']') {
        return true;
    }
    false
}

/// Port of `origin_from_referer` (`sms_tool/transport/auth_headers.py:1114`).
pub fn origin_from_referer(referer: String) -> Result<String, String> {
    if referer.is_empty() {
        return Ok(String::new());
    }
    // Python 3.12 `urllib.parse.urlsplit`: strips leading/trailing C0 control/space (<= 0x20),
    // then removes '\t', '\r', '\n'.
    let trimmed = referer.trim_matches(|c: char| (c as u32) <= 0x20);
    if trimmed.is_empty() {
        return Ok(String::new());
    }
    let cleaned_owned;
    let s = if trimmed.contains(['\t', '\r', '\n']) {
        cleaned_owned = trimmed
            .chars()
            .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
            .collect::<String>();
        cleaned_owned.as_str()
    } else {
        trimmed
    };
    let Some(colon) = s.find(':') else {
        return Ok(String::new());
    };
    let cand = &s[..colon];
    if cand.is_empty()
        || !cand.as_bytes()[0].is_ascii_alphabetic()
        || !cand
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
    {
        return Ok(String::new());
    }
    let rest = &s[colon + 1..];
    let Some(after_slashes) = rest.strip_prefix("//") else {
        return Ok(String::new());
    };
    let end = after_slashes
        .find(['/', '?', '#'])
        .unwrap_or(after_slashes.len());
    let netloc = &after_slashes[..end];
    if netloc.is_empty() || netloc_has_invalid_brackets(netloc) {
        return Ok(String::new());
    }
    let scheme = cand.to_ascii_lowercase();
    Ok(format!("{scheme}://{netloc}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_version_list_expands_brands() {
        let out = _full_version_list(
            r#""Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146""#.into(),
            "146.0.7680.178".into(),
        )
        .expect("ok");
        assert_eq!(
            out,
            r#""Chromium";v="146.0.7680.178", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.178""#
        );
    }

    #[test]
    fn accept_encoding_and_origin_match_expected() {
        assert_eq!(
            accept_encoding_for("firefox144".into()).expect("ok"),
            ACCEPT_ENCODING_ZSTD
        );
        assert_eq!(
            accept_encoding_for("safari18_4".into()).expect("ok"),
            ACCEPT_ENCODING_NO_ZSTD
        );
        assert_eq!(
            accept_encoding_for("safari26_3".into()).expect("ok"),
            ACCEPT_ENCODING_ZSTD
        );
        assert_eq!(
            origin_from_referer("https://auth.openai.com/log-in?foo=1".into()).expect("ok"),
            "https://auth.openai.com"
        );
    }
}
