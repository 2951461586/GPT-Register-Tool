//! Rust port of `_validate_api_base` in `sms_tool/pay_link/extract_server.py`.

#![forbid(unsafe_code)]

fn validate_netloc_brackets(netloc: &str) -> bool {
    if netloc.is_empty() {
        return true;
    }
    let hostinfo = match netloc.rsplit_once('@') {
        Some((_, right)) => right,
        None => netloc,
    };
    let has_open = hostinfo.contains('[');
    let has_close = hostinfo.contains(']');
    if has_open != has_close {
        return false;
    }
    if has_open {
        let Some(after_open) = hostinfo.strip_prefix('[') else {
            return false;
        };
        let Some(close) = after_open.find(']') else {
            return false;
        };
        let host = &after_open[..close];
        let ip_part = host.split('%').next().unwrap_or(host);
        if ip_part.parse::<std::net::Ipv4Addr>().is_ok() {
            return false;
        }
        if ip_part.parse::<std::net::Ipv6Addr>().is_err() {
            return false;
        }
    }
    true
}

pub fn _validate_api_base(api_base: String) -> Result<bool, String> {
    let cleaned: String = api_base
        .trim_start_matches(|c: char| (c as u32) <= 0x20)
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let s = cleaned.as_str();
    let Some(idx) = s.find(':') else {
        return Ok(false);
    };
    let cand = &s[..idx];
    let valid_scheme = !cand.is_empty()
        && cand.as_bytes()[0].is_ascii_alphabetic()
        && cand
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
    if !valid_scheme {
        return Ok(false);
    }
    let scheme = cand.to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return Ok(false);
    }
    let rest = &s[idx + 1..];
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let Some(after_slashes) = before_query.strip_prefix("//") else {
        return Ok(false);
    };
    let netloc = match after_slashes.find('/') {
        Some(pos) => &after_slashes[..pos],
        None => after_slashes,
    };
    if netloc.is_empty() {
        return Ok(false);
    }
    Ok(validate_netloc_brackets(netloc))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_http_and_https_bases() {
        assert!(_validate_api_base("https://pay.example.com".into()).unwrap());
        assert!(_validate_api_base("http://127.0.0.1:8080/api".into()).unwrap());
        assert!(_validate_api_base("https://[::1]:8443".into()).unwrap());
        assert!(!_validate_api_base("ftp://pay.example.com".into()).unwrap());
        assert!(!_validate_api_base("https:///no-host".into()).unwrap());
        assert!(!_validate_api_base("https://[bad".into()).unwrap());
        assert!(!_validate_api_base("".into()).unwrap());
    }
}
