//! Rust port of hot string and JWT normalizers in `sms_tool/store/normalize.py`.

#![forbid(unsafe_code)]

use serde_json::Value;

pub const KNOWN_EMAIL_DOMAINS: [&str; 5] = [
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "gmail.com",
];

#[inline]
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

#[inline]
fn trim_py_whitespace(s: &str) -> &str {
    s.trim_matches(is_py_space)
}

fn is_py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                true
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(quote);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn py_repr_val(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => py_repr_str(s),
        Value::Array(arr) => {
            let items: Vec<String> = arr.iter().map(py_repr_val).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(map) => {
            let items: Vec<String> = map
                .iter()
                .map(|(k, val)| format!("{}: {}", py_repr_str(k), py_repr_val(val)))
                .collect();
            format!("{{{}}}", items.join(", "))
        }
    }
}

fn py_str_or_empty(v: &Value) -> String {
    if !is_py_truthy(v) {
        return String::new();
    }
    match v {
        Value::String(s) => s.clone(),
        _ => py_repr_val(v),
    }
}

/// Exact matcher for Python `EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")`.
fn email_matches(s: &str) -> bool {
    let Some((local, domain)) = s.split_once('@') else {
        return false;
    };
    if local.is_empty() || domain.len() < 3 {
        return false;
    }
    if local.chars().any(is_py_space) {
        return false;
    }
    if domain.chars().any(|c| c == '@' || is_py_space(c)) {
        return false;
    }
    let bytes = domain.as_bytes();
    bytes
        .iter()
        .enumerate()
        .any(|(i, &b)| b == b'.' && i > 0 && i + 1 < bytes.len())
}

pub fn _normalize_account_email(email: serde_json::Value) -> Result<String, String> {
    let raw = py_str_or_empty(&email);
    let value = trim_py_whitespace(&raw).trim_start_matches('\u{feff}');
    if let Some((local, suffix)) = value.split_once("@+") {
        let suffix_lower = suffix.to_lowercase();
        let suffix_chars = suffix.chars().count();
        for domain in KNOWN_EMAIL_DOMAINS {
            let dom_len = domain.len();
            if suffix_lower.ends_with(domain) && suffix_chars > dom_len {
                let alias: String = suffix.chars().take(suffix_chars - dom_len).collect();
                let repaired = format!("{local}+{alias}@{domain}");
                if email_matches(&repaired) {
                    return Ok(repaired.to_lowercase());
                }
            }
        }
    }
    Ok(value.to_lowercase())
}

pub fn _looks_codex_refresh_token(token: serde_json::Value) -> Result<bool, String> {
    let raw = py_str_or_empty(&token);
    let value = trim_py_whitespace(&raw);
    if value.is_empty() || value == "[REDACTED]" {
        return Ok(false);
    }
    if value.starts_with("rt_") {
        return Ok(true);
    }
    if value.starts_with("M.C_") || value.starts_with("M.R_") {
        return Ok(false);
    }
    let ok = value.chars().count() >= 64
        && value.bytes().filter(|&b| b == b'.').count() == 2
        && !value.chars().any(is_py_space)
        && value
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '.' | '_' | '~' | '-'));
    Ok(ok)
}

fn normalize_account_type_str(raw: &str) -> &'static str {
    let text = trim_py_whitespace(raw).to_lowercase();
    if text.contains("team") || text.contains("business") || text.contains("enterprise") {
        "team"
    } else if text.contains("plus") || text.contains("pro") {
        "plus"
    } else if text.contains("k12") || text.contains("edu") {
        "k12"
    } else if text.contains("free") {
        "free"
    } else {
        ""
    }
}

fn normalize_account_type_val(value: &Value) -> String {
    let raw = py_str_or_empty(value);
    normalize_account_type_str(&raw).to_string()
}

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

/// Decodes ASCII base64url identically to Python `base64.urlsafe_b64decode`.
fn decode_py_urlsafe_b64(part: &str) -> Option<Vec<u8>> {
    if !part.is_ascii() {
        return None;
    }
    let pad = (4 - part.len() % 4) % 4;
    let mut vals: Vec<u8> = Vec::with_capacity(part.len() + pad);
    let mut eq_seen = false;
    let mut eq_count = 0usize;
    for b in part.bytes().chain(std::iter::repeat_n(b'=', pad)) {
        if b == b'=' {
            eq_seen = true;
            eq_count += 1;
            continue;
        }
        if let Some(v) = b64_val(b) {
            if eq_seen {
                break;
            }
            vals.push(v);
        }
    }
    let rem = vals.len() % 4;
    if rem == 1 {
        return None;
    }
    if rem == 2 && eq_count < 2 {
        return None;
    }
    if rem == 3 && eq_count < 1 {
        return None;
    }
    let mut out = Vec::with_capacity((vals.len() * 3) / 4);
    let mut i = 0usize;
    while i + 4 <= vals.len() {
        let n = ((vals[i] as u32) << 18)
            | ((vals[i + 1] as u32) << 12)
            | ((vals[i + 2] as u32) << 6)
            | (vals[i + 3] as u32);
        out.push((n >> 16) as u8);
        out.push((n >> 8) as u8);
        out.push(n as u8);
        i += 4;
    }
    if rem == 2 {
        let n = ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12);
        out.push((n >> 16) as u8);
    } else if rem == 3 {
        let n =
            ((vals[i] as u32) << 18) | ((vals[i + 1] as u32) << 12) | ((vals[i + 2] as u32) << 6);
        out.push((n >> 16) as u8);
        out.push((n >> 8) as u8);
    }
    Some(out)
}

pub fn _jwt_account_type(access_token: serde_json::Value) -> Result<String, String> {
    let raw = py_str_or_empty(&access_token);
    let Some(part) = raw.split('.').nth(1) else {
        return Ok(String::new());
    };
    let Some(decoded) = decode_py_urlsafe_b64(part) else {
        return Ok(String::new());
    };
    let Ok(Value::Object(claims)) = serde_json::from_slice::<Value>(&decoded) else {
        return Ok(String::new());
    };
    let Some(Value::Object(auth)) = claims.get("https://api.openai.com/auth") else {
        return Ok(String::new());
    };
    let chosen = auth
        .get("chatgpt_plan_type")
        .filter(|v| is_py_truthy(v))
        .or_else(|| auth.get("plan_type"))
        .cloned()
        .unwrap_or(Value::Null);
    Ok(normalize_account_type_val(&chosen))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn repairs_corrupted_at_plus_email() {
        assert_eq!(
            _normalize_account_email(json!("\u{feff}User@+taghotmail.com")).unwrap(),
            "user+tag@hotmail.com"
        );
    }

    #[test]
    fn recognizes_codex_refresh_tokens() {
        assert!(_looks_codex_refresh_token(json!("rt_abc123")).unwrap());
        assert!(!_looks_codex_refresh_token(json!("M.C_something")).unwrap());
        assert!(!_looks_codex_refresh_token(json!("[REDACTED]")).unwrap());
        let jwt_rt = format!("{}.{}.{}", "a".repeat(25), "b".repeat(25), "c".repeat(25));
        assert!(_looks_codex_refresh_token(json!(jwt_rt)).unwrap());
    }

    #[test]
    fn decodes_jwt_plan_type() {
        // {"https://api.openai.com/auth":{"chatgpt_plan_type":"plus"}}
        let payload =
            "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9wbGFuX3R5cGUiOiJwbHVzIn19";
        let token = format!("eyJhbGciOiJub25lIn0.{payload}.sig");
        assert_eq!(_jwt_account_type(json!(token)).unwrap(), "plus");
        assert_eq!(_jwt_account_type(json!("not-a-jwt")).unwrap(), "");
    }
}
