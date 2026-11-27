//! Rust port of `is_navigation_retryable` from
//! `sms_tool/registration_drivers/browser_flow/recovery.py`.

#![forbid(unsafe_code)]

const TRANSIENT_MARKERS: &[&str] = &[
    "tls connect error",
    "openssl_internal",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "failed to connect",
    "proxy",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
    "ns_error_abort",
    "navigation aborted",
    "page.goto",
];

pub fn is_navigation_retryable(err: Option<String>) -> Result<bool, String> {
    let raw = err.unwrap_or_default();
    let text = if raw.is_ascii() {
        raw.to_ascii_lowercase()
    } else {
        raw.to_lowercase()
    };
    if TRANSIENT_MARKERS.iter().any(|m| text.contains(m)) {
        return Ok(true);
    }
    Ok(text.contains("navigation") && text.contains("abort"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_transient_and_navigation_abort_errors() {
        assert!(!is_navigation_retryable(None).unwrap());
        assert!(!is_navigation_retryable(Some("account_deactivated".into())).unwrap());
        assert!(is_navigation_retryable(Some("Page.goto: net::ERR_ABORTED".into())).unwrap());
        assert!(
            is_navigation_retryable(Some("Navigation failed: abort requested".into())).unwrap()
        );
        assert!(is_navigation_retryable(Some("curl: (35) TLS connect error".into())).unwrap());
        assert!(!is_navigation_retryable(Some("Navigation succeeded".into())).unwrap());
    }
}
