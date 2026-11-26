//! Rust port of region query/template rewriting in `sms_tool/phone/phone_proxy.py`.

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

#[inline]
fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn quote_plus(s: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if b == b' ' {
            out.push('+');
        } else if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(HEX[(b >> 4) as usize] as char);
            out.push(HEX[(b & 0x0f) as usize] as char);
        }
    }
    out
}

fn unquote_plus(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'+' {
            out.push(b' ');
            i += 1;
        } else if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2]))
        {
            out.push((hi << 4) | lo);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub fn _api_url_for_region(api_url: String, country_iso: String) -> Result<String, String> {
    let trimmed_url = py_trim(&api_url);
    let region = py_trim(&country_iso).to_uppercase();
    if trimmed_url.is_empty() || region.is_empty() {
        return Ok(trimmed_url.to_string());
    }

    // Exact CPython urlsplit
    let cleaned: String = trimmed_url
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
    let (before_frag, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, query_raw) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let (netloc, path) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };

    // Validate unbalanced IPv6 brackets in netloc (matches CPython urlsplit)
    if !netloc.is_empty() {
        let hostinfo = match netloc.rfind('@') {
            Some(pos) => &netloc[pos + 1..],
            None => netloc,
        };
        if (hostinfo.contains('[') && !hostinfo.contains(']'))
            || (hostinfo.contains(']') && !hostinfo.contains('['))
        {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        }
        if let Some(after_open) = hostinfo.strip_prefix('[') {
            let Some(close) = after_open.find(']') else {
                return Err("ValueError: Invalid IPv6 URL".to_string());
            };
            let host = &after_open[..close];
            let ip_part = host.split('%').next().unwrap_or(host);
            if ip_part.parse::<std::net::Ipv6Addr>().is_err() {
                return Err(format!(
                    "ValueError: {host:?} does not appear to be an IPv4 or IPv6 address"
                ));
            }
        }
    }

    let mut pairs: Vec<(String, String)> = Vec::new();
    let mut has_region = false;
    if !query_raw.is_empty() {
        for segment in query_raw.split('&') {
            if segment.is_empty() {
                continue;
            }
            let (k_raw, v_raw) = segment.split_once('=').unwrap_or((segment, ""));
            let k = unquote_plus(k_raw);
            let v = unquote_plus(v_raw);
            if k == "region" {
                has_region = true;
            }
            if let Some(existing) = pairs.iter_mut().find(|(ek, _)| ek == &k) {
                existing.1 = v;
            } else {
                pairs.push((k, v));
            }
        }
    }

    if has_region {
        for (k, v) in &mut pairs {
            if k == "region" {
                *v = region.clone();
            }
        }
        let mut encoded_query = String::new();
        for (idx, (k, v)) in pairs.iter().enumerate() {
            if idx > 0 {
                encoded_query.push('&');
            }
            encoded_query.push_str(&quote_plus(k));
            encoded_query.push('=');
            encoded_query.push_str(&quote_plus(v));
        }
        let mut out = String::with_capacity(trimmed_url.len() + region.len() + 8);
        if !netloc.is_empty() || (!scheme.is_empty() && path.starts_with("//")) {
            if !scheme.is_empty() {
                out.push_str(&scheme);
                out.push(':');
            }
            out.push_str("//");
            out.push_str(netloc);
            if !path.is_empty() && !path.starts_with('/') {
                out.push('/');
            }
            out.push_str(path);
        } else {
            if !scheme.is_empty() {
                out.push_str(&scheme);
                out.push(':');
            }
            out.push_str(path);
        }
        if !encoded_query.is_empty() {
            out.push('?');
            out.push_str(&encoded_query);
        }
        if !fragment.is_empty() {
            out.push('#');
            out.push_str(fragment);
        }
        return Ok(out);
    }

    Ok(trimmed_url
        .replace("{region}", &region)
        .replace("{REGION}", &region))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrites_region_query_and_template() {
        assert_eq!(
            _api_url_for_region(
                "https://proxy.example/get?key=abc&region=US&num=1".to_string(),
                "jp".to_string()
            )
            .unwrap(),
            "https://proxy.example/get?key=abc&region=JP&num=1"
        );
        assert_eq!(
            _api_url_for_region(
                "https://proxy.example/{region}/pick?r={REGION}".to_string(),
                "vn".to_string()
            )
            .unwrap(),
            "https://proxy.example/VN/pick?r=VN"
        );
    }
}
