//! Rust port of `_addr_type` and `_host_matches_blocked`
//! from `sms_tool/proxy/bridge.py`.

#![forbid(unsafe_code)]

const ATYP_IPV4: i64 = 0x01;
const ATYP_DOMAIN: i64 = 0x03;
const ATYP_IPV6: i64 = 0x04;

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// Mirror BSD/Winsock `inet_addr` / `socket.inet_aton` parsing rules.
fn parse_bsd_ipv4(s: &str) -> bool {
    if s == "255.255.255.255" {
        return true;
    }
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut parts = [0u64; 4];
    let mut count = 0usize;
    let mut i = 0usize;
    loop {
        if count >= 4 || i >= bytes.len() || !bytes[i].is_ascii_digit() {
            return false;
        }
        let base: u64 = if bytes[i] == b'0' {
            i += 1;
            if i < bytes.len() && (bytes[i] == b'x' || bytes[i] == b'X') {
                i += 1;
                if i >= bytes.len() || !bytes[i].is_ascii_hexdigit() {
                    return false;
                }
                16
            } else {
                8
            }
        } else {
            10
        };
        let mut val: u64 = 0;
        while i < bytes.len() {
            let b = bytes[i];
            let digit = match base {
                10 if b.is_ascii_digit() => Some((b - b'0') as u64),
                8 if matches!(b, b'0'..=b'7') => Some((b - b'0') as u64),
                16 if b.is_ascii_digit() => Some((b - b'0') as u64),
                16 if matches!(b, b'a'..=b'f') => Some((b - b'a' + 10) as u64),
                16 if matches!(b, b'A'..=b'F') => Some((b - b'A' + 10) as u64),
                _ => None,
            };
            match digit {
                Some(d) => {
                    val = match val.checked_mul(base).and_then(|v| v.checked_add(d)) {
                        Some(next) if next <= u32::MAX as u64 => next,
                        _ => return false,
                    };
                    i += 1;
                }
                None => break,
            }
        }
        parts[count] = val;
        count += 1;
        if i < bytes.len() && bytes[i] == b'.' {
            i += 1;
            continue;
        }
        break;
    }
    // Winsock `inet_addr` allows trailing ASCII space/control whitespace after the address.
    if i < bytes.len() && !matches!(bytes[i], b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c) {
        return false;
    }
    let combined: u64 = match count {
        1 => parts[0],
        2 => {
            if parts[0] > 0xff || parts[1] > 0x00ff_ffff {
                return false;
            }
            (parts[0] << 24) | parts[1]
        }
        3 => {
            if parts[0] > 0xff || parts[1] > 0xff || parts[2] > 0xffff {
                return false;
            }
            (parts[0] << 24) | (parts[1] << 16) | parts[2]
        }
        4 => {
            if parts[0] > 0xff || parts[1] > 0xff || parts[2] > 0xff || parts[3] > 0xff {
                return false;
            }
            (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        }
        _ => return false,
    };
    combined != u32::MAX as u64
}

pub fn _addr_type(host: String) -> Result<i64, String> {
    if host.contains('\0') {
        return Err("ValueError: embedded null character".to_string());
    }
    if parse_bsd_ipv4(&host) {
        return Ok(ATYP_IPV4);
    }
    if host.parse::<std::net::Ipv6Addr>().is_ok() {
        return Ok(ATYP_IPV6);
    }
    Ok(ATYP_DOMAIN)
}

pub fn _host_matches_blocked(host: Option<String>, rules: Vec<String>) -> Result<bool, String> {
    let raw = host.unwrap_or_default();
    let trimmed = py_trim(&raw);
    if trimmed.is_empty() || rules.is_empty() {
        return Ok(false);
    }
    let lowered = if trimmed.is_ascii() {
        trimmed.to_ascii_lowercase()
    } else {
        trimmed.to_lowercase()
    };
    let candidate = lowered.trim_end_matches('.');
    if candidate.is_empty() {
        return Ok(false);
    }
    for rule in &rules {
        if candidate == rule.as_str() {
            return Ok(true);
        }
        if candidate.len() > rule.len()
            && candidate.ends_with(rule.as_str())
            && candidate.as_bytes()[candidate.len() - rule.len() - 1] == b'.'
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_ipv4_ipv6_domain_and_blocklist() {
        assert_eq!(_addr_type("127.0.0.1".into()).unwrap(), ATYP_IPV4);
        assert_eq!(_addr_type("255.255.255.255".into()).unwrap(), ATYP_IPV4);
        assert_eq!(_addr_type("127.1".into()).unwrap(), ATYP_IPV4);
        assert_eq!(_addr_type("::1".into()).unwrap(), ATYP_IPV6);
        assert_eq!(_addr_type("chatgpt.com".into()).unwrap(), ATYP_DOMAIN);
        let rules = vec!["gvt1.com".to_string(), "dl.google.com".to_string()];
        assert!(_host_matches_blocked(Some("DL.GOOGLE.COM.".into()), rules.clone()).unwrap());
        assert!(_host_matches_blocked(Some("rr8.gvt1.com".into()), rules.clone()).unwrap());
        assert!(!_host_matches_blocked(Some("notgvt1.com".into()), rules).unwrap());
    }
}
