//! Rust port of `_jwt_segment` and `_safe_url` in `sms_tool/codex/oauth.py`.

#![forbid(unsafe_code)]

use serde_json::{Map, Value};

#[inline]
fn b64_val(b: u8) -> Option<u8> {
    match b {
        b'A'..=b'Z' => Some(b - b'A'),
        b'a'..=b'z' => Some(b - b'a' + 26),
        b'0'..=b'9' => Some(b - b'0' + 52),
        b'+' | b'-' => Some(62),
        b'/' | b'_' => Some(63),
        _ => None,
    }
}

fn py_urlsafe_b64decode_ascii(segment: &str) -> Option<Vec<u8>> {
    if !segment.is_ascii() {
        return None;
    }
    let pad_len = (4 - (segment.len() % 4)) % 4;
    let mut buf: Vec<u8> = Vec::with_capacity(segment.len() + pad_len);
    buf.extend_from_slice(segment.as_bytes());
    buf.resize(segment.len() + pad_len, b'=');

    let mut out = Vec::with_capacity(buf.len() * 3 / 4);
    let mut quad = [0u8; 4];
    let mut qlen = 0usize;
    let mut eq_seen = 0usize;

    for &b in &buf {
        if b == b'=' {
            if qlen >= 2 {
                eq_seen += 1;
                if qlen + eq_seen >= 4 {
                    break;
                }
            }
            continue;
        }
        if eq_seen > 0 {
            continue;
        }
        if let Some(v) = b64_val(b) {
            quad[qlen] = v;
            qlen += 1;
            if qlen == 4 {
                out.push((quad[0] << 2) | (quad[1] >> 4));
                out.push((quad[1] << 4) | (quad[2] >> 2));
                out.push((quad[2] << 6) | quad[3]);
                qlen = 0;
            }
        }
    }

    match (qlen, eq_seen) {
        (0, _) => Some(out),
        (2, e) if e >= 2 => {
            out.push((quad[0] << 2) | (quad[1] >> 4));
            Some(out)
        }
        (3, e) if e >= 1 => {
            out.push((quad[0] << 2) | (quad[1] >> 4));
            out.push((quad[1] << 4) | (quad[2] >> 2));
            Some(out)
        }
        _ => None,
    }
}

pub fn _jwt_segment(segment: String) -> Result<serde_json::Value, String> {
    let Some(decoded) = py_urlsafe_b64decode_ascii(&segment) else {
        return Ok(Value::Object(Map::new()));
    };
    let Ok(text) = std::str::from_utf8(&decoded) else {
        return Ok(Value::Object(Map::new()));
    };
    Ok(serde_json::from_str::<Value>(text).unwrap_or_else(|_| Value::Object(Map::new())))
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

fn uses_netloc(scheme: &str) -> bool {
    matches!(
        scheme,
        "" | "ftp"
            | "http"
            | "gopher"
            | "nntp"
            | "telnet"
            | "imap"
            | "wais"
            | "file"
            | "mms"
            | "https"
            | "shttp"
            | "snews"
            | "prospero"
            | "rtsp"
            | "rtsps"
            | "rtspu"
            | "rsync"
            | "svn"
            | "svn+ssh"
            | "sftp"
            | "nfs"
            | "git"
            | "git+ssh"
            | "ws"
            | "wss"
            | "itms-services"
    )
}

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

pub fn _safe_url(url: String) -> Result<String, String> {
    let cleaned: String = url
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
    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let (netloc, path_raw) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => (&after[..pos], &after[pos..]),
            None => (after, ""),
        }
    } else {
        ("", before_query)
    };
    if !validate_netloc_brackets(netloc) {
        return Ok(url.chars().take(200).collect());
    }
    let path = if uses_params(&scheme) && path_raw.contains(';') {
        if let Some(slash_pos) = path_raw.rfind('/') {
            let tail = &path_raw[slash_pos..];
            if let Some(semi) = tail.find(';') {
                &path_raw[..slash_pos + semi]
            } else {
                path_raw
            }
        } else if let Some(semi) = path_raw.find(';') {
            &path_raw[..semi]
        } else {
            path_raw
        }
    } else {
        path_raw
    };

    // Exact CPython urlunsplit((scheme, netloc, path, "", ""))
    let mut out = String::with_capacity(scheme.len() + netloc.len() + path.len() + 4);
    let mut url_part = String::with_capacity(netloc.len() + path.len() + 4);
    if !netloc.is_empty() || (!scheme.is_empty() && uses_netloc(&scheme) && !path.starts_with("//"))
    {
        let mut p = path;
        if !p.is_empty() && !p.starts_with('/') {
            url_part.push_str("//");
            url_part.push_str(netloc);
            url_part.push('/');
            url_part.push_str(p);
        } else {
            if p.starts_with("//") {
                p = path;
            }
            url_part.push_str("//");
            url_part.push_str(netloc);
            url_part.push_str(p);
        }
    } else {
        url_part.push_str(path);
    }
    if !scheme.is_empty() {
        out.push_str(&scheme);
        out.push(':');
    }
    out.push_str(&url_part);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decodes_segment_and_redacts_url_query() {
        assert_eq!(
            _jwt_segment("eyJzdWIiOiJ1MSIsImV4cCI6MTkwMH0".to_string()).unwrap(),
            json!({"sub": "u1", "exp": 1900})
        );
        assert_eq!(
            _safe_url("https://auth.openai.com/oauth/authorize?code=secret#frag".to_string())
                .unwrap(),
            "https://auth.openai.com/oauth/authorize"
        );
    }
}
