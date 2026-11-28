//! Rust port of `_redact_capability_error` from `sms_tool/payment/capability.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;

struct RedactRegexes {
    proxy_auth: Regex,
    bearer: Regex,
}

fn rx() -> &'static RedactRegexes {
    static CACHE: OnceLock<RedactRegexes> = OnceLock::new();
    CACHE.get_or_init(|| RedactRegexes {
        proxy_auth: Regex::new(r"(?i)((?:https?|socks5h?|socks4a?)://)[^/@\s]+:[^/@\s]+@")
            .expect("valid proxy_auth regex"),
        bearer: Regex::new(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+").expect("valid bearer regex"),
    })
}

pub fn _redact_capability_error(text: Option<String>) -> Result<String, String> {
    let raw = text.unwrap_or_default();
    if raw.is_empty() {
        return Ok(String::new());
    }
    let regs = rx();
    let step1 = regs.proxy_auth.replace_all(&raw, "${1}***:***@");
    let step2 = regs.bearer.replace_all(&step1, "Bearer [REDACTED]");
    let mut out = String::with_capacity(step2.len().min(500));
    for ch in step2.chars().take(500) {
        out.push(ch);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_proxy_and_bearer_and_truncates() {
        let input =
            "failed via http://alice:secret@proxy.example:8080 with Bearer eyJabc.def-123/xyz";
        let out = _redact_capability_error(Some(input.to_string())).unwrap();
        assert_eq!(
            out,
            "failed via http://***:***@proxy.example:8080 with Bearer [REDACTED]"
        );
        let long = "a".repeat(600);
        assert_eq!(_redact_capability_error(Some(long)).unwrap().len(), 500);
    }
}
