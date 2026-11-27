//! Rust port of `_strip_prompt_query` in `sms_tool/registration/account_stages.py`.

#![forbid(unsafe_code)]

const HEX: &[u8; 16] = b"0123456789ABCDEF";

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

fn uses_params(scheme: &str) -> bool {
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

fn extract_hostname(netloc: &str) -> Result<Option<String>, String> {
    if netloc.is_empty() {
        return Ok(None);
    }
    let hostinfo = match netloc.rsplit_once('@') {
        Some((_, right)) => right,
        None => netloc,
    };
    let has_open = hostinfo.contains('[');
    let has_close = hostinfo.contains(']');
    if has_open != has_close {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    if let Some(after_open) = hostinfo.strip_prefix('[') {
        let Some(close) = after_open.find(']') else {
            return Err("ValueError: Invalid IPv6 URL".to_string());
        };
        let host = &after_open[..close];
        let ip_part = host.split('%').next().unwrap_or(host);
        if ip_part.parse::<std::net::Ipv4Addr>().is_ok() {
            return Err("ValueError: An IPv4 address cannot be in brackets".to_string());
        }
        if ip_part.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(format!(
                "ValueError: {host:?} does not appear to be an IPv4 or IPv6 address"
            ));
        }
        let lower = host.to_lowercase();
        return Ok((!lower.is_empty()).then_some(lower));
    }
    if has_open || has_close {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }
    let host = match hostinfo.split_once(':') {
        Some((h, _)) => h,
        None => hostinfo,
    };
    let lower = host.to_lowercase();
    Ok((!lower.is_empty()).then_some(lower))
}

pub fn _strip_prompt_query(url: String) -> Result<String, String> {
    let trimmed = py_trim(&url);
    if trimmed.is_empty() {
        return Ok(String::new());
    }
    let cleaned: String = trimmed
        .trim_matches(|c: char| (c as u32) <= 0x20)
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
    let (netloc, path_raw) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };
    let (path, params) = if uses_params(&scheme) && path_raw.contains(';') {
        if let Some(slash_pos) = path_raw.rfind('/') {
            let tail = &path_raw[slash_pos..];
            if let Some(semi) = tail.find(';') {
                (&path_raw[..slash_pos + semi], &tail[semi + 1..])
            } else {
                (path_raw, "")
            }
        } else if let Some(semi) = path_raw.find(';') {
            (&path_raw[..semi], &path_raw[semi + 1..])
        } else {
            (path_raw, "")
        }
    } else {
        (path_raw, "")
    };

    let hostname = extract_hostname(netloc)?;
    if scheme.is_empty() || hostname.is_none() {
        return Ok(String::new());
    }

    let mut encoded_query = String::new();
    if !query_raw.is_empty() {
        for segment in query_raw.split('&') {
            if segment.is_empty() {
                continue;
            }
            let (k_raw, v_raw) = segment.split_once('=').unwrap_or((segment, ""));
            let k = unquote_plus(k_raw);
            if k == "prompt" {
                continue;
            }
            let v = unquote_plus(v_raw);
            if !encoded_query.is_empty() {
                encoded_query.push('&');
            }
            encoded_query.push_str(&quote_plus(&k));
            encoded_query.push('=');
            encoded_query.push_str(&quote_plus(&v));
        }
    }

    let mut out = String::with_capacity(cleaned.len() + 8);
    out.push_str(&scheme);
    out.push_str("://");
    out.push_str(netloc);
    if !path.is_empty() && !path.starts_with('/') {
        out.push('/');
    }
    out.push_str(path);
    if !params.is_empty() {
        out.push(';');
        out.push_str(params);
    }
    if !encoded_query.is_empty() {
        out.push('?');
        out.push_str(&encoded_query);
    }
    if !fragment.is_empty() {
        out.push('#');
        out.push_str(fragment);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_prompt_param_only() {
        assert_eq!(
            _strip_prompt_query(
                "https://auth.openai.com/oauth/authorize?client_id=abc&prompt=login&state=xyz&prompt=select_account#frag"
                    .to_string()
            )
            .unwrap(),
            "https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz#frag"
        );
        assert_eq!(
            _strip_prompt_query("/relative/path?prompt=login".to_string()).unwrap(),
            ""
        );
    }
}
