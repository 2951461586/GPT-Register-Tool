//! Rust port of browser profile command-line token matching in
//! `sms_tool/browser/profile_reclaim.py`.

const PROFILE_FLAG_PREFIXES: [&str; 6] = [
    "--user-data-dir=",
    "--profile=",
    "-profile=",
    "--user-data-dir",
    "--profile",
    "-profile",
];

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

fn normalize_path(raw: &str) -> String {
    let trimmed = raw.trim_matches(py_is_space);
    let replaced = trimmed.replace('\\', "/");
    replaced.trim_end_matches('/').to_lowercase()
}

pub fn _references_profile(command_line: String, target: String) -> Result<bool, String> {
    if target.is_empty() {
        return Ok(false);
    }
    let normalized = normalize_path(&command_line);
    for token in normalized.split(|c: char| py_is_space(c) || c == '"') {
        if token.is_empty() {
            continue;
        }
        if token == target {
            return Ok(true);
        }
        for prefix in PROFILE_FLAG_PREFIXES {
            if let Some(rest) = token.strip_prefix(prefix) {
                let candidate = rest.trim_matches('=').trim_end_matches('/');
                if candidate == target {
                    return Ok(true);
                }
            }
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_standalone_and_flagged_profile_tokens() {
        let cmd = r#""C:\Program Files\Camoufox\camoufox.exe" --no-remote -profile "F:\runtime\browser_profiles\camoufox\user@example.com""#;
        let target = "f:/runtime/browser_profiles/camoufox/user@example.com";
        assert!(_references_profile(cmd.to_string(), target.to_string()).unwrap());
        assert!(
            !_references_profile(
                cmd.to_string(),
                "f:/runtime/browser_profiles/camoufox/user".to_string()
            )
            .unwrap()
        );
        assert!(
            _references_profile(
                "chrome.exe --user-data-dir=C:\\tmp\\prof1/".to_string(),
                "c:/tmp/prof1".to_string()
            )
            .unwrap()
        );
    }
}
