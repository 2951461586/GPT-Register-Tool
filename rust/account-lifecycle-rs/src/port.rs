//! Rust port of `_email_core` in `sms_tool/accounts/account_lifecycle.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;

fn non_alnum_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"[^\p{L}\p{N}]+").expect("valid regex"))
}

pub fn _email_core(email: String) -> Result<String, String> {
    if email.is_ascii() {
        let mut out = String::with_capacity(email.len());
        for b in email.bytes() {
            if b.is_ascii_alphanumeric() {
                out.push(b.to_ascii_lowercase() as char);
            }
        }
        return Ok(out);
    }
    let lowered = email.to_lowercase();
    Ok(non_alnum_re().replace_all(&lowered, "").into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_email_core() {
        assert_eq!(
            _email_core("SampleAccount66@+oai01hotmail.com".to_string()).unwrap(),
            "sampleaccount66oai01hotmailcom"
        );
        assert_eq!(_email_core("ΣΊΣΥΦΟΣ".to_string()).unwrap(), "σίσυφος");
        assert_eq!(_email_core("कि ٣٤".to_string()).unwrap(), "क٣٤");
    }
}
