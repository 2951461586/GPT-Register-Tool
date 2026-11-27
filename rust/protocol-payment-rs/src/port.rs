//! Rust port of `sms_tool/payment/protocol_codec.py` hot paths:
//! - Hosted Checkout `rv_timestamp` codec & Stripe.js static build-field extractor
//! - Deep checkout quote extraction (`_quote_from_payload_str`)

#![forbid(unsafe_code)]

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use regex::Regex;
use serde_json::{Map, Value, json};

const PRINTABLE_MIN: u8 = 32;
const PRINTABLE_WIDTH: i32 = 95;
const XOR_BYTE: u8 = 5;
const SHIFT: i32 = 11;
const MAX_OBJECT_SCAN: usize = 8192;
const HEX_UPPER: &[u8; 16] = b"0123456789ABCDEF";

const ZERO_DECIMAL_CURRENCIES: &[&str] = &[
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV",
    "XAF", "XOF", "XPF",
];

const MINOR_NAMES: &[&str] = &[
    "checkout_amount_minor",
    "amount_total",
    "amount_due",
    "unit_amount",
    "taxInclusive",
    "tax_inclusive",
];

struct CodecRegexes {
    rvts_key: Regex,
    rv_ts_field: Regex,
    rv_field: Regex,
    sv_field: Regex,
    assignment: Regex,
    identifier: Regex,
}

fn rx() -> &'static CodecRegexes {
    static CACHE: OnceLock<CodecRegexes> = OnceLock::new();
    CACHE.get_or_init(|| {
        let js_str = r#""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"#;
        let js_id = r"[A-Za-z_$][A-Za-z0-9_$]*";
        let js_val = format!("(?:{js_str}|{js_id})");
        let js_cmt = r"(?:(?:/\*[\s\S]*?\*/|//[^\r\n]*)\s*)*";
        let field_pat = |name: &str| {
            Regex::new(&format!(
                r#"(?s)(?:["']?{name}["']?)\s*:\s*{js_cmt}(?P<value>{js_val})"#
            ))
            .expect("valid field regex")
        };
        CodecRegexes {
            rvts_key: Regex::new(r#"(?:["']rvTs["']|\brvTs\b)\s*:"#).expect("valid regex"),
            rv_ts_field: field_pat("rvTs"),
            rv_field: field_pat("rv"),
            sv_field: field_pat("sv"),
            assignment: Regex::new(&format!(
                r#"(?s)(?:\b(?:var|let|const)\s+)?(?P<name>{js_id})\s*=\s*{js_cmt}(?P<value>{js_val})"#
            ))
            .expect("valid assignment regex"),
            identifier: Regex::new(&format!("^{js_id}$")).expect("valid identifier regex"),
        }
    })
}

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

fn py_str_repr(s: &str, out: &mut String) {
    let use_double = s.contains('\'') && !s.contains('"');
    let quote = if use_double { '"' } else { '\'' };
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\'' if !use_double => out.push_str("\\'"),
            '"' if use_double => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                use std::fmt::Write;
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
}

fn py_repr_into(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("None"),
        Value::Bool(true) => out.push_str("True"),
        Value::Bool(false) => out.push_str("False"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => py_str_repr(s, out),
        Value::Array(arr) => {
            out.push('[');
            for (idx, item) in arr.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_repr_into(item, out);
            }
            out.push(']');
        }
        Value::Object(obj) => {
            out.push('{');
            for (idx, (k, v)) in obj.iter().enumerate() {
                if idx > 0 {
                    out.push_str(", ");
                }
                py_str_repr(k, out);
                out.push_str(": ");
                py_repr_into(v, out);
            }
            out.push('}');
        }
    }
}

fn py_str_exact(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        other => {
            let mut out = String::new();
            py_repr_into(other, &mut out);
            out
        }
    }
}

fn py_str_or_empty(value: &Value) -> String {
    match value {
        Value::Null | Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Number(n) => {
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(a) if a.is_empty() => String::new(),
        Value::Object(o) if o.is_empty() => String::new(),
        other => {
            let mut out = String::new();
            py_repr_into(other, &mut out);
            out
        }
    }
}

// ─── rv_timestamp codec & Stripe.js extractor ────────────────────────────────

fn json_escape_ascii(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                use std::fmt::Write;
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if (c as u32) <= 0x7f => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    use std::fmt::Write;
                    let _ = write!(out, "\\u{:04x}", unit);
                }
            }
        }
    }
    out.push('"');
}

fn uri_quote_rv(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for &b in s.as_bytes() {
        let safe = b.is_ascii_alphanumeric()
            || matches!(
                b,
                b'~' | b'(' | b')' | b'*' | b'!' | b'.' | b'\'' | b'-' | b'_'
            );
        if safe {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(HEX_UPPER[(b >> 4) as usize] as char);
            out.push(HEX_UPPER[(b & 0x0f) as usize] as char);
        }
    }
    out
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn uri_unquote(s: &str) -> String {
    if !s.contains('%') {
        return s.to_string();
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2]))
        {
            out.push((hi << 4) | lo);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn shift_printable_ascii(s: &str, amount: i32) -> Result<String, String> {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        let code = ch as u32;
        if !(PRINTABLE_MIN as u32..(PRINTABLE_MIN as u32 + PRINTABLE_WIDTH as u32)).contains(&code)
        {
            return Err(
                "ValueError: rv_timestamp contains a non-printable intermediate character"
                    .to_string(),
            );
        }
        let shifted = ((code as i32 - PRINTABLE_MIN as i32 + amount).rem_euclid(PRINTABLE_WIDTH))
            as u8
            + PRINTABLE_MIN;
        out.push(shifted as char);
    }
    Ok(out)
}

fn encode_rv_strings(rv_ts: &str, rv: &str, sv: &str) -> Result<String, String> {
    let mut payload = String::with_capacity(rv_ts.len() + rv.len() + sv.len() + 32);
    payload.push_str("{\"rvTs\":");
    json_escape_ascii(rv_ts, &mut payload);
    payload.push_str(",\"rv\":");
    json_escape_ascii(rv, &mut payload);
    payload.push_str(",\"sv\":");
    json_escape_ascii(sv, &mut payload);
    payload.push('}');
    let pad = 3 - (payload.len() % 3);
    for _ in 0..pad {
        payload.push(' ');
    }
    let xored: Vec<u8> = payload.bytes().map(|b| b ^ XOR_BYTE).collect();
    let b64 = B64.encode(&xored);
    let quoted = uri_quote_rv(&b64);
    shift_printable_ascii(&quoted, SHIFT)
}

pub fn encode_rv_timestamp(
    rv_ts: serde_json::Value,
    rv: serde_json::Value,
    sv: serde_json::Value,
) -> Result<String, String> {
    encode_rv_strings(
        &py_str_exact(&rv_ts),
        &py_str_exact(&rv),
        &py_str_exact(&sv),
    )
}

pub fn _decode_rv_timestamp_dict(
    value: serde_json::Value,
) -> Result<Option<serde_json::Value>, String> {
    let Value::String(s) = value else {
        return Ok(None);
    };
    if s.is_empty() {
        return Ok(None);
    }
    let Ok(unshifted) = shift_printable_ascii(&s, -SHIFT) else {
        return Ok(None);
    };
    let unquoted = uri_unquote(&unshifted);
    let Ok(decoded_bytes) = B64.decode(unquoted.as_bytes()) else {
        return Ok(None);
    };
    let xored: String = decoded_bytes
        .into_iter()
        .map(|b| (b ^ XOR_BYTE) as char)
        .collect();
    let trimmed = xored.trim_end_matches(' ');
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(trimmed) else {
        return Ok(None);
    };
    let (Some(Value::String(rv_ts)), Some(Value::String(rv)), Some(Value::String(sv))) =
        (map.get("rvTs"), map.get("rv"), map.get("sv"))
    else {
        return Ok(None);
    };
    Ok(Some(json!({"rvTs": rv_ts, "rv": rv, "sv": sv})))
}

fn decode_js_string_literal(token: &str) -> Option<String> {
    let t = py_trim(token);
    let bytes = t.as_bytes();
    if bytes.len() < 2 {
        return None;
    }
    let q = bytes[0];
    if !matches!(q, b'"' | b'\'' | b'`') || bytes[bytes.len() - 1] != q {
        return None;
    }
    let body = &t[1..t.len() - 1];
    if q == b'`' && body.contains("${") {
        return None;
    }
    let mut out = String::with_capacity(body.len());
    let mut chars = body.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            if (q == b'"' || q == b'\'') && matches!(ch, '\n' | '\r') {
                return None;
            }
            out.push(ch);
            continue;
        }
        let next = chars.next()?;
        match next {
            '\\' => out.push('\\'),
            '\'' => out.push('\''),
            '"' => out.push('"'),
            '`' => out.push('`'),
            'n' => out.push('\n'),
            'r' => out.push('\r'),
            't' => out.push('\t'),
            'b' => out.push('\u{0008}'),
            'f' => out.push('\u{000c}'),
            'a' => out.push('\u{0007}'),
            'v' => out.push('\u{000b}'),
            '\n' => {}
            'x' => {
                let h1 = chars.next()?;
                let h2 = chars.next()?;
                let hex = format!("{h1}{h2}");
                let code = u32::from_str_radix(&hex, 16).ok()?;
                out.push(char::from_u32(code)?);
            }
            'u' => {
                let mut hex = String::with_capacity(4);
                for _ in 0..4 {
                    hex.push(chars.next()?);
                }
                let code = u32::from_str_radix(&hex, 16).ok()?;
                out.push(char::from_u32(code)?);
            }
            other => {
                out.push('\\');
                out.push(other);
            }
        }
    }
    Some(out)
}

fn find_object_end(text: &str, start: usize) -> Option<usize> {
    let mut quote_char: Option<u8> = None;
    let mut escaped = false;
    let mut depth = 0i32;
    for (char_steps, (rel, ch)) in text[start..].char_indices().enumerate() {
        if char_steps >= MAX_OBJECT_SCAN {
            break;
        }
        let b = if ch.is_ascii() { ch as u8 } else { 0 };
        if let Some(q) = quote_char {
            if escaped {
                escaped = false;
            } else if b == b'\\' {
                escaped = true;
            } else if b == q {
                quote_char = None;
            }
            continue;
        }
        match b {
            b'"' | b'\'' | b'`' => quote_char = Some(b),
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(start + rel + ch.len_utf8());
                }
            }
            _ => {}
        }
    }
    None
}

fn extract_rv_fields_str(text: &str) -> Option<(String, String, String)> {
    if text.is_empty() || !text.contains("rvTs") {
        return None;
    }
    let regs = rx();
    let mut seen_starts: HashSet<usize> = HashSet::new();
    let mut assignments: Option<HashMap<String, String>> = None;

    for m in regs.rvts_key.find_iter(text) {
        let key_start = m.start();
        let mut window_start = key_start.saturating_sub(MAX_OBJECT_SCAN * 4);
        while window_start < key_start && !text.is_char_boundary(window_start) {
            window_start += 1;
        }
        let prefix = &text[window_start..key_start];
        let mut found_brace: Option<usize> = None;
        for (char_back, (rel, ch)) in prefix.char_indices().rev().enumerate() {
            if char_back >= MAX_OBJECT_SCAN {
                break;
            }
            if ch == '{' {
                found_brace = Some(window_start + rel);
                break;
            }
        }
        let Some(start) = found_brace else {
            continue;
        };
        if !seen_starts.insert(start) {
            continue;
        }
        let Some(end) = find_object_end(text, start) else {
            continue;
        };
        let candidate = &text[start..end];
        let (Some(c_ts), Some(c_rv), Some(c_sv)) = (
            regs.rv_ts_field.captures(candidate),
            regs.rv_field.captures(candidate),
            regs.sv_field.captures(candidate),
        ) else {
            continue;
        };
        let raw_vals = [
            c_ts.name("value").map(|v| v.as_str())?,
            c_rv.name("value").map(|v| v.as_str())?,
            c_sv.name("value").map(|v| v.as_str())?,
        ];
        let mut out_vals: [Option<String>; 3] = [None, None, None];
        let mut ok = true;
        for (idx, raw) in raw_vals.iter().enumerate() {
            if let Some(lit) = decode_js_string_literal(raw) {
                out_vals[idx] = Some(lit);
                continue;
            }
            let map = assignments.get_or_insert_with(|| {
                let mut m = HashMap::new();
                for cap in regs.assignment.captures_iter(text) {
                    if let (Some(n), Some(v)) = (cap.name("name"), cap.name("value"))
                        && let Some(decoded) = decode_js_string_literal(v.as_str())
                    {
                        m.insert(n.as_str().to_string(), decoded);
                    }
                }
                m
            });
            let cur_trimmed = py_trim(raw);
            if !regs.identifier.is_match(cur_trimmed) {
                ok = false;
                break;
            }
            let mut cur = cur_trimmed.to_string();
            let mut visited = HashSet::new();
            let mut failed = false;
            while regs.identifier.is_match(&cur) {
                if !visited.insert(cur.clone()) {
                    failed = true;
                    break;
                }
                let Some(next) = map.get(&cur) else {
                    failed = true;
                    break;
                };
                cur = next.clone();
            }
            if failed || cur.is_empty() {
                ok = false;
                break;
            }
            out_vals[idx] = Some(cur);
        }
        if ok && let [Some(a), Some(b), Some(c)] = out_vals {
            return Some((a, b, c));
        }
    }
    None
}

pub fn _extract_rv_build_fields_dict(
    text: serde_json::Value,
) -> Result<Option<serde_json::Value>, String> {
    let Value::String(s) = text else {
        return Ok(None);
    };
    Ok(extract_rv_fields_str(&s).map(|(rv_ts, rv, sv)| json!({"rvTs": rv_ts, "rv": rv, "sv": sv})))
}

pub fn rv_timestamp_from_script(text: serde_json::Value) -> Result<Option<String>, String> {
    let Value::String(s) = text else {
        return Ok(None);
    };
    let Some((rv_ts, rv, sv)) = extract_rv_fields_str(&s) else {
        return Ok(None);
    };
    Ok(Some(encode_rv_strings(&rv_ts, &rv, &sv)?))
}

// ─── Deep checkout quote extraction ──────────────────────────────────────────

fn find_string_recursive(payload: &Value, names: &[&str], depth: usize) -> Option<String> {
    if depth > 6 {
        return None;
    }
    match payload {
        Value::Object(map) => {
            for &key in names {
                if let Some(Value::String(s)) = map.get(key) {
                    let trimmed = py_trim(s);
                    if !trimmed.is_empty() {
                        return Some(trimmed.to_string());
                    }
                }
            }
            for v in map.values() {
                if let Some(found) = find_string_recursive(v, names, depth + 1) {
                    return Some(found);
                }
            }
            None
        }
        Value::Array(arr) => {
            for item in arr {
                if let Some(found) = find_string_recursive(item, names, depth + 1) {
                    return Some(found);
                }
            }
            None
        }
        _ => None,
    }
}

fn find_named_recursive<'a>(payload: &'a Value, name: &str, depth: usize) -> Option<&'a Value> {
    if depth > 6 {
        return None;
    }
    match payload {
        Value::Object(map) => {
            if let Some(v) = map.get(name)
                && !v.is_null()
            {
                return Some(v);
            }
            for v in map.values() {
                if let Some(found) = find_named_recursive(v, name, depth + 1) {
                    return Some(found);
                }
            }
            None
        }
        Value::Array(arr) => {
            for item in arr {
                if let Some(found) = find_named_recursive(item, name, depth + 1) {
                    return Some(found);
                }
            }
            None
        }
        _ => None,
    }
}

fn is_valid_decimal_text(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut i = 0;
    if matches!(bytes[i], b'+' | b'-') {
        i += 1;
        if i >= bytes.len() {
            return false;
        }
    }
    let mut digits = 0usize;
    let mut saw_dot = false;
    while i < bytes.len() {
        match bytes[i] {
            b'0'..=b'9' => {
                digits += 1;
                i += 1;
            }
            b'.' if !saw_dot => {
                saw_dot = true;
                i += 1;
            }
            _ => return false,
        }
    }
    digits > 0
}

fn format_minor_int_decimal(val: i64, currency: &str, minor: bool) -> String {
    if !minor || ZERO_DECIMAL_CURRENCIES.contains(&currency) {
        return val.to_string();
    }
    let neg = val < 0;
    let abs = val.unsigned_abs();
    let whole = abs / 100;
    let frac = abs % 100;
    if neg {
        format!("-{whole}.{frac:02}")
    } else {
        format!("{whole}.{frac:02}")
    }
}

fn coerce_decimal_str(value: &Value, currency: &str, minor: bool) -> Option<String> {
    match value {
        Value::Null | Value::Bool(_) | Value::Array(_) | Value::Object(_) => None,
        Value::String(s) => {
            let cleaned = s.replace(',', "").replace(currency, "");
            let trimmed = py_trim(&cleaned);
            if is_valid_decimal_text(trimmed) {
                Some(trimmed.to_string())
            } else {
                None
            }
        }
        Value::Number(n) => {
            if let Some(i) = n.as_i64()
                && !n.is_f64()
            {
                Some(format_minor_int_decimal(i, currency, minor))
            } else {
                n.as_f64().filter(|f| f.is_finite()).map(|_| n.to_string())
            }
        }
    }
}

fn checkout_state_amount_str(state: &Map<String, Value>, currency: &str) -> Option<String> {
    if let Some(Value::Object(total)) = state.get("total") {
        for key in ["total", "subtotal", "amount"] {
            if let Some(Value::Object(bucket)) = total.get(key)
                && let Some(minor_v) = bucket.get("minorUnitsAmount")
                && let Some(parsed) = coerce_decimal_str(minor_v, currency, true)
            {
                return Some(parsed);
            }
        }
        if let Some(minor_v) = total.get("minorUnitsAmount")
            && let Some(parsed) = coerce_decimal_str(minor_v, currency, true)
        {
            return Some(parsed);
        }
    }
    if let Some(Value::Array(items)) = state.get("lineItems")
        && let Some(Value::Object(first)) = items.first()
    {
        for key in ["subtotal", "total", "amount"] {
            if let Some(Value::Object(bucket)) = first.get(key)
                && let Some(minor_v) = bucket.get("minorUnitsAmount")
                && let Some(parsed) = coerce_decimal_str(minor_v, currency, true)
            {
                return Some(parsed);
            }
        }
    }
    None
}

pub fn _quote_from_payload_str(
    payload: serde_json::Value,
    default_currency: Option<serde_json::Value>,
) -> Result<(Option<String>, String), String> {
    let default_str = default_currency
        .as_ref()
        .map(py_str_or_empty)
        .unwrap_or_default();
    let found_cur = find_string_recursive(&payload, &["currency", "currency_code"], 0);
    let currency = py_trim(found_cur.as_deref().unwrap_or(&default_str)).to_uppercase();
    if let Value::Object(map) = &payload {
        if let Some(top) = map.get("amount_total")
            && !top.is_null()
            && let Some(parsed) = coerce_decimal_str(top, &currency, true)
        {
            return Ok((Some(parsed), currency));
        }
        if let Some(Value::Object(state)) = map.get("checkout_state")
            && let Some(parsed) = checkout_state_amount_str(state, &currency)
        {
            return Ok((Some(parsed), currency));
        }
    }
    for &name in MINOR_NAMES {
        if let Some(found) = find_named_recursive(&payload, name, 0)
            && let Some(parsed) = coerce_decimal_str(found, &currency, true)
        {
            return Ok((Some(parsed), currency));
        }
    }
    if let Some(Value::Object(total)) = find_named_recursive(&payload, "total", 0) {
        for name in ["taxInclusive", "tax_inclusive", "total", "amount"] {
            if let Some(v) = total.get(name) {
                let is_int = matches!(v, Value::Number(n) if n.is_i64() && !n.is_f64());
                if let Some(parsed) = coerce_decimal_str(v, &currency, is_int) {
                    return Ok((Some(parsed), currency));
                }
            }
        }
    }
    for name in ["total_amount", "display_amount", "amount", "total"] {
        if let Some(found) = find_named_recursive(&payload, name, 0) {
            if matches!(found, Value::Object(_) | Value::Array(_)) {
                continue;
            }
            if let Some(parsed) = coerce_decimal_str(found, &currency, false) {
                return Ok((Some(parsed), currency));
            }
        }
    }
    Ok((None, currency))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rv_timestamp_roundtrip_and_js_extract() {
        let enc = encode_rv_timestamp(
            json!("2024-01-01 00:00:00 -0000"),
            json!("fixture-rv"),
            json!("fixture-sv"),
        )
        .unwrap();
        let dec = _decode_rv_timestamp_dict(json!(enc)).unwrap().unwrap();
        assert_eq!(dec["rvTs"], "2024-01-01 00:00:00 -0000");
        assert_eq!(dec["rv"], "fixture-rv");
        assert_eq!(dec["sv"], "fixture-sv");

        let js = r#"const TS='2024-01-01 00:00:00 -0000';let B="rv-1";var S=`sv-1`;const x={"rvTs":TS,'rv':B,sv:S};"#;
        let ts = rv_timestamp_from_script(json!(js)).unwrap().unwrap();
        let dec2 = _decode_rv_timestamp_dict(json!(ts)).unwrap().unwrap();
        assert_eq!(dec2["rv"], "rv-1");
        assert_eq!(dec2["sv"], "sv-1");
    }

    #[test]
    fn quotes_nested_checkout_payloads() {
        let quote_payload = json!({"currency": "usd", "amount_total": 2000});
        let (amt, cur) = _quote_from_payload_str(quote_payload, None).unwrap();
        assert_eq!(amt.as_deref(), Some("20.00"));
        assert_eq!(cur, "USD");

        let jpy_payload = json!({"currency": "jpy", "checkout_state": {"total": {"total": {"minorUnitsAmount": 3000}}}});
        let (amt2, cur2) = _quote_from_payload_str(jpy_payload, None).unwrap();
        assert_eq!(amt2.as_deref(), Some("3000"));
        assert_eq!(cur2, "JPY");
    }
}
