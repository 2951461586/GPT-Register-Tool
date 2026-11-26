//! Rust port of dotenv line parsing and quoted-value escape decoding in
//! `sms_tool/kernel/env_loader.py`.

use serde_json::{Map, Value};

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn is_valid_env_key(key: &str) -> bool {
    let bytes = key.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_alphabetic() || first == b'_') {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|&b| b.is_ascii_alphanumeric() || b == b'_')
}

fn decode_value_str(raw: &str) -> String {
    let trimmed = py_trim(raw);
    let bytes = trimmed.as_bytes();
    if bytes.len() < 2 {
        return trimmed.to_string();
    }
    let first = bytes[0];
    let last = bytes[bytes.len() - 1];
    if (first != b'\'' && first != b'"') || last != first {
        return trimmed.to_string();
    }
    let inner = &trimmed[1..trimmed.len() - 1];
    if first == b'\'' {
        return inner.replace("\\'", "'");
    }
    let mut out = String::with_capacity(inner.len());
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            if let Some(next_ch) = chars.next() {
                match next_ch {
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    other => out.push(other),
                }
            } else {
                out.push('\\');
            }
        } else {
            out.push(ch);
        }
    }
    out
}

pub fn _parse_lines(lines: Vec<String>) -> Result<serde_json::Value, String> {
    let mut values: Map<String, Value> = Map::new();
    for raw_line in lines {
        let mut line = py_trim(&raw_line);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export") {
            if rest.is_empty() {
                line = "";
            } else if rest.chars().next().is_some_and(py_is_space) {
                line = rest.trim_start_matches(py_is_space);
            }
        }
        let Some((key_raw, raw_value)) = line.split_once('=') else {
            continue;
        };
        let key = py_trim(key_raw);
        if !is_valid_env_key(key) {
            continue;
        }
        values.insert(key.to_string(), Value::String(decode_value_str(raw_value)));
    }
    Ok(Value::Object(values))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decodes_quotes_and_parses_dotenv_lines() {
        assert_eq!(decode_value_str("  \"hello\\nworld\"  "), "hello\nworld");
        assert_eq!(decode_value_str("'it\\'s ok'"), "it's ok");
        let parsed = _parse_lines(vec![
            "# comment".to_string(),
            "export API_KEY = \"abc\\t123\"".to_string(),
            "BAD-KEY=1".to_string(),
            "PLAIN=value".to_string(),
        ])
        .unwrap();
        assert_eq!(parsed, json!({"API_KEY": "abc\t123", "PLAIN": "value"}));
    }
}
