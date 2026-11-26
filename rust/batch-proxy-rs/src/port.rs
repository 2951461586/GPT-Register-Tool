//! Rust port of proxy endpoint round-robin interleaving in
//! `sms_tool/registration/batch_runner.py`.

use std::collections::{HashMap, HashSet};

fn parse_host_port(raw: &str) -> Result<(String, u16), String> {
    let trimmed = raw.trim_start_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: String = trimmed
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let s = cleaned.as_str();

    let rest = if let Some(idx) = s.find(':') {
        let cand = &s[..idx];
        let valid_scheme = !cand.is_empty()
            && cand.as_bytes()[0].is_ascii_alphabetic()
            && cand
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
        if valid_scheme { &s[idx + 1..] } else { s }
    } else {
        s
    };

    let before_frag = rest.split_once('#').map_or(rest, |(a, _)| a);
    let before_query = before_frag.split_once('?').map_or(before_frag, |(a, _)| a);
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(pos) => &after[..pos],
            None => after,
        }
    } else {
        ""
    };

    if netloc.is_empty() {
        return Ok((String::new(), 0));
    }

    let hostinfo = match netloc.rfind('@') {
        Some(pos) => &netloc[pos + 1..],
        None => netloc,
    };

    if (hostinfo.contains('[') && !hostinfo.contains(']'))
        || (hostinfo.contains(']') && !hostinfo.contains('['))
    {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    }

    let (hostname, port_str) = if let Some(after_open) = hostinfo.strip_prefix('[') {
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
        let tail = &after_open[close + 1..];
        let port = tail.strip_prefix(':').unwrap_or("");
        (host.to_lowercase(), port)
    } else if hostinfo.contains('[') || hostinfo.contains(']') {
        return Err("ValueError: Invalid IPv6 URL".to_string());
    } else if let Some((h, p)) = hostinfo.split_once(':') {
        (h.to_lowercase(), p)
    } else {
        (hostinfo.to_lowercase(), "")
    };

    let port = if port_str.is_empty() {
        0u16
    } else if port_str.bytes().all(|b| b.is_ascii_digit()) {
        match port_str.parse::<u32>() {
            Ok(v) if v <= 65535 => v as u16,
            _ => {
                return Err(format!(
                    "ValueError: Port out of range 0-65535: {port_str:?}"
                ));
            }
        }
    } else {
        return Err(format!(
            "ValueError: Port could not be cast to integer value as {port_str:?}"
        ));
    };

    Ok((hostname, port))
}

fn endpoint_group_str(raw: &str) -> Result<String, String> {
    let (host, port) = parse_host_port(raw)?;
    Ok(format!("{host}:{port}"))
}

pub fn _interleave_proxy_endpoints(proxies: Vec<String>) -> Result<Vec<String>, String> {
    if proxies.is_empty() {
        return Ok(Vec::new());
    }
    let mut seen: HashSet<String> = HashSet::with_capacity(proxies.len());
    let mut unique: Vec<String> = Vec::with_capacity(proxies.len());
    for item in proxies {
        if seen.insert(item.clone()) {
            unique.push(item);
        }
    }

    let mut group_idx: HashMap<String, usize> = HashMap::new();
    let mut groups: Vec<Vec<String>> = Vec::new();
    for proxy in &unique {
        let key = endpoint_group_str(proxy)?;
        if let Some(&idx) = group_idx.get(&key) {
            groups[idx].push(proxy.clone());
        } else {
            group_idx.insert(key, groups.len());
            groups.push(vec![proxy.clone()]);
        }
    }

    if groups.len() <= 1 {
        return Ok(unique);
    }

    let mut ordered: Vec<String> = Vec::with_capacity(unique.len());
    let mut round = 0usize;
    loop {
        let mut added = false;
        for group in &groups {
            if let Some(proxy) = group.get(round) {
                ordered.push(proxy.clone());
                added = true;
            }
        }
        if !added {
            break;
        }
        round += 1;
    }
    Ok(ordered)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interleaves_distinct_endpoints_and_deduplicates() {
        let input = vec![
            "http://u1:p@a.example:8080".to_string(),
            "http://u2:p@a.example:8080".to_string(),
            "http://u3:p@b.example:9090".to_string(),
            "http://u1:p@a.example:8080".to_string(),
            "http://u4:p@b.example:9090".to_string(),
        ];
        assert_eq!(
            _interleave_proxy_endpoints(input).unwrap(),
            vec![
                "http://u1:p@a.example:8080",
                "http://u3:p@b.example:9090",
                "http://u2:p@a.example:8080",
                "http://u4:p@b.example:9090",
            ]
        );
    }
}
