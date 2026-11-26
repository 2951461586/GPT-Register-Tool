//! Rust port of `parse_method_selection` in `sms_tool/payment/qualification.py`.

#![forbid(unsafe_code)]

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

pub fn parse_method_selection(value: String) -> Result<Vec<String>, String> {
    let mut methods: Vec<String> = Vec::new();
    for item in value.split([',', ';', '\n', ' ']) {
        let trimmed = py_trim(item);
        if trimmed.is_empty() {
            continue;
        }
        let lowered = if trimmed.is_ascii() {
            trimmed.to_ascii_lowercase()
        } else {
            trimmed.to_lowercase()
        };
        let method = if lowered.contains('-') {
            lowered.replace('-', "_")
        } else {
            lowered
        };
        if !method.is_empty() && method != "auto" && !methods.contains(&method) {
            methods.push(method);
        }
    }
    Ok(methods)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_deduplicates_method_selections() {
        assert_eq!(
            parse_method_selection("GCash; mo-mo, auto\npaypal gcash".to_string()).unwrap(),
            vec!["gcash", "mo_mo", "paypal"]
        );
        assert!(
            parse_method_selection("auto".to_string())
                .unwrap()
                .is_empty()
        );
    }
}
