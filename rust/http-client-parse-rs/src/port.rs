//! Rust port of `is_transient_transport_error` from
//! `sms_tool/transport/http_client.py`.

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
];

pub fn is_transient_transport_error(err: Option<String>) -> Result<bool, String> {
    let raw = err.unwrap_or_default();
    let text = if raw.is_ascii() {
        raw.to_ascii_lowercase()
    } else {
        raw.to_lowercase()
    };
    Ok(TRANSIENT_MARKERS.iter().any(|m| text.contains(m)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_transient_transport_markers() {
        assert!(!is_transient_transport_error(None).unwrap());
        assert!(!is_transient_transport_error(Some("account_deactivated".into())).unwrap());
        assert!(
            is_transient_transport_error(Some(
                "curl: (35) TLS connect error: OPENSSL_internal".into()
            ))
            .unwrap()
        );
        assert!(is_transient_transport_error(Some("NS_ERROR_ABORT on navigation".into())).unwrap());
        assert!(is_transient_transport_error(Some("Proxy tunnel timed out".into())).unwrap());
    }
}
