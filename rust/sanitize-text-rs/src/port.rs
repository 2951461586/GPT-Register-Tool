//! Rust port of `sanitize_text`, `sanitize_log_text`, and `sanitize`
//! from `sms_tool/kernel/sanitizer.py`, reading rules from `sensitive_policy.json`.

#![forbid(unsafe_code)]

use std::borrow::Cow;
use std::collections::HashSet;
use std::fmt::Write as _;
use std::sync::OnceLock;

use regex::{Regex, RegexSet};
use serde::Deserialize;
use serde_json::{Map, Number, Value};

const POLICY_JSON: &str = include_str!("../../../sensitive_policy.json");

#[derive(Deserialize)]
struct RawPattern {
    #[serde(default)]
    pattern: String,
    #[serde(default)]
    replacement: String,
}

#[derive(Deserialize)]
struct RawPolicy {
    #[serde(default)]
    redacted_value: String,
    #[serde(default)]
    text_patterns: Vec<RawPattern>,
    #[serde(default)]
    log_text_patterns: Vec<RawPattern>,
    #[serde(default)]
    sensitive_keys: Vec<String>,
    #[serde(default)]
    safe_key_suffixes: Vec<String>,
    #[serde(default)]
    sensitive_key_fragments: Vec<String>,
    #[serde(default)]
    safe_key_paths: Vec<String>,
}

struct LogRule {
    re: Regex,
    rep: String,
    neg_lb: Option<Regex>,
    needs_at: bool,
}

struct CompiledPolicy {
    redacted: String,
    set: RegexSet,
    rules: Vec<(Regex, String)>,
    log_rules: Vec<LogRule>,
    keys: HashSet<String>,
    safe_suffixes: Vec<String>,
    fragments: Vec<String>,
    safe_paths: Vec<String>,
}

fn compiled_policy() -> Result<&'static CompiledPolicy, String> {
    static CACHE: OnceLock<Result<CompiledPolicy, String>> = OnceLock::new();
    CACHE
        .get_or_init(build_policy)
        .as_ref()
        .map_err(Clone::clone)
}

/// Rewrite Python `\s` (includes `\x1c-\x1f`) into an equivalent Rust regex.
fn py_regex(pat: &str) -> String {
    let (mut out, mut cls, mut esc) = (String::with_capacity(pat.len() + 16), false, false);
    for ch in pat.chars() {
        if esc {
            esc = false;
            if ch == 's' {
                out.push_str(if cls {
                    r"\s\x1c-\x1f"
                } else {
                    r"[\s\x1c-\x1f]"
                });
            } else {
                out.push('\\');
                out.push(ch);
            }
        } else if ch == '\\' {
            esc = true;
        } else {
            if ch == '[' {
                cls = true;
            } else if ch == ']' {
                cls = false;
            }
            out.push(ch);
        }
    }
    if esc {
        out.push('\\');
    }
    out
}

/// Split leading `[flags](?<![class])rest` so it runs on linear-time SIMD `regex`.
fn split_neg_lb(pat: &str) -> Option<(String, String)> {
    let mut flags = "";
    let mut rest = pat;
    if let Some(s) = pat.strip_prefix("(?")
        && let Some(e) = s.find(')')
        && !s[..e].starts_with('<')
    {
        flags = &pat[..e + 3];
        rest = &pat[e + 3..];
    }
    let after = rest.strip_prefix("(?<![")?;
    let bytes = after.as_bytes();
    let (mut i, mut esc) = (0, false);
    while i < bytes.len() {
        if esc {
            esc = false;
        } else if bytes[i] == b'\\' {
            esc = true;
        } else if bytes[i] == b']' && bytes.get(i + 1) == Some(&b')') {
            return Some((
                format!("{flags}[{}]", &after[..i]),
                format!("{flags}{}", &after[i + 2..]),
            ));
        }
        i += 1;
    }
    None
}

fn build_policy() -> Result<CompiledPolicy, String> {
    let raw: RawPolicy =
        serde_json::from_str(POLICY_JSON).map_err(|e| format!("ValueError: {e}"))?;
    let redacted = if raw.redacted_value.is_empty() {
        "[REDACTED]".into()
    } else {
        raw.redacted_value
    };
    let dollar = Regex::new(r"\$(\d+)").map_err(|e| format!("ValueError: {e}"))?;
    let norm = |rep: &str| {
        let s = if rep.is_empty() {
            redacted.as_str()
        } else {
            rep
        };
        dollar.replace_all(s, "$${$1}").into_owned()
    };
    let mut pats = Vec::with_capacity(raw.text_patterns.len());
    let mut rules = Vec::with_capacity(raw.text_patterns.len());
    for item in &raw.text_patterns {
        let compat = py_regex(&item.pattern);
        let re = Regex::new(&compat).map_err(|e| format!("ValueError: {e}"))?;
        rules.push((re, norm(&item.replacement)));
        pats.push(compat);
    }
    let set = RegexSet::new(&pats).map_err(|e| format!("ValueError: {e}"))?;
    let mut log_rules = Vec::new();
    for item in &raw.log_text_patterns {
        if item.pattern.is_empty() {
            continue;
        }
        let compat = py_regex(&item.pattern);
        let rep = norm(&item.replacement);
        let needs_at = item.pattern.contains('@') && !item.pattern.contains("[@");
        if let Ok(re) = Regex::new(&compat) {
            log_rules.push(LogRule {
                re,
                rep,
                neg_lb: None,
                needs_at,
            });
        } else if let Some((lb, inner)) = split_neg_lb(&compat) {
            let re = Regex::new(&inner).map_err(|e| format!("ValueError: {e}"))?;
            let lb_re = Regex::new(&lb).map_err(|e| format!("ValueError: {e}"))?;
            log_rules.push(LogRule {
                re,
                rep,
                neg_lb: Some(lb_re),
                needs_at,
            });
        } else {
            return Err(format!(
                "ValueError: unsupported log pattern `{}`",
                item.pattern
            ));
        }
    }
    Ok(CompiledPolicy {
        redacted,
        set,
        rules,
        log_rules,
        keys: raw
            .sensitive_keys
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect(),
        safe_suffixes: raw.safe_key_suffixes,
        fragments: raw.sensitive_key_fragments,
        safe_paths: raw
            .safe_key_paths
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect(),
    })
}

fn sanitize_str(text: String, policy: &CompiledPolicy) -> String {
    if text.is_empty() {
        return text;
    }
    let matches = policy.set.matches(&text);
    if !matches.matched_any() {
        return text;
    }
    let mut cur: Cow<'_, str> = Cow::Borrowed(&text);
    for idx in matches.iter() {
        if let Some((re, rep)) = policy.rules.get(idx)
            && let Cow::Owned(r) = re.replace_all(&cur, rep.as_str())
        {
            cur = Cow::Owned(r);
        }
    }
    match cur {
        Cow::Borrowed(_) => text,
        Cow::Owned(s) => s,
    }
}

fn apply_log_rule(text: &str, rule: &LogRule) -> Option<String> {
    if rule.needs_at && !text.contains('@') {
        return None;
    }
    let Some(lb_re) = &rule.neg_lb else {
        return match rule.re.replace_all(text, rule.rep.as_str()) {
            Cow::Borrowed(_) => None,
            Cow::Owned(s) => Some(s),
        };
    };
    let (mut out, mut last, mut pos, mut buf) = (String::new(), 0, 0, [0u8; 4]);
    while pos < text.len()
        && let Some(caps) = rule.re.captures_at(text, pos)
        && let Some(m) = caps.get(0)
    {
        let prev = text[..m.start()].chars().next_back();
        if prev.is_some_and(|c| lb_re.is_match(c.encode_utf8(&mut buf))) {
            let step = text[m.start()..].chars().next().map_or(1, char::len_utf8);
            pos = m.start() + step;
            continue;
        }
        if out.is_empty() {
            out.reserve(text.len());
        }
        out.push_str(&text[last..m.start()]);
        caps.expand(&rule.rep, &mut out);
        last = m.end();
        pos = m.end().max(m.start() + 1);
    }
    if last == 0 && out.is_empty() {
        return None;
    }
    out.push_str(&text[last..]);
    Some(out)
}

fn fmt_num(n: &Number) -> String {
    if let Some(i) = n.as_i64() {
        return i.to_string();
    }
    if let Some(u) = n.as_u64() {
        return u.to_string();
    }
    let Some(x) = n.as_f64() else {
        return n.to_string();
    };
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x.is_sign_positive() { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    if !(1e-4..1e16).contains(&x.abs()) {
        let s = format!("{x:e}");
        if let Some((m, e)) = s.split_once('e')
            && let Ok(p) = e.parse::<i32>()
        {
            return format!("{m}e{}{:02}", if p < 0 { '-' } else { '+' }, p.abs());
        }
        return s;
    }
    let s = x.to_string();
    if s.contains(['.', 'e', 'E']) {
        s
    } else {
        format!("{s}.0")
    }
}

fn write_py_str(s: &str, out: &mut String) {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    out.push(q);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(q);
            }
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(q);
}

fn write_seq(items: &[Value], open: char, close: char, trail_one: bool, out: &mut String) {
    out.push(open);
    for (i, v) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        write_py_repr(v, out);
    }
    if trail_one && items.len() == 1 {
        out.push(',');
    }
    out.push(close);
}

fn write_py_repr(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("None"),
        Value::Bool(b) => out.push_str(if *b { "True" } else { "False" }),
        Value::Number(n) => out.push_str(&fmt_num(n)),
        Value::String(s) => write_py_str(s, out),
        Value::Array(items) => write_seq(items, '[', ']', false, out),
        Value::Object(map) => {
            if map.len() == 1
                && let Some((k, inner)) = map.iter().next()
                && write_marker(k, inner, out)
            {
                return;
            }
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_str(k, out);
                out.push_str(": ");
                write_py_repr(val, out);
            }
            out.push('}');
        }
    }
}

fn write_marker(key: &str, inner: &Value, out: &mut String) -> bool {
    match (key, inner) {
        ("$num", Value::String(t)) => match t.as_str() {
            "NaN" => out.push_str("nan"),
            "Infinity" => out.push_str("inf"),
            "-Infinity" => out.push_str("-inf"),
            "-0" => out.push_str("-0.0"),
            _ => return false,
        },
        ("$int", Value::String(s)) => out.push_str(s),
        ("$tuple", Value::Array(a)) => write_seq(a, '(', ')', true, out),
        ("$set", Value::Array(a)) if a.is_empty() => out.push_str("set()"),
        ("$set", Value::Array(a)) => write_seq(a, '{', '}', false, out),
        ("$bytes", Value::Array(a)) => {
            out.push_str("b'");
            for b in a.iter().map(|v| v.as_u64().unwrap_or(0) as u8) {
                if matches!(b, 0x20..=0x7e) && !matches!(b, b'\\' | b'\'') {
                    out.push(b as char);
                } else {
                    let esc = match b {
                        b'\\' => r"\\",
                        b'\'' => r"\'",
                        b'\n' => r"\n",
                        b'\r' => r"\r",
                        b'\t' => r"\t",
                        _ => "",
                    };
                    if esc.is_empty() {
                        let _ = write!(out, "\\x{b:02x}");
                    } else {
                        out.push_str(esc);
                    }
                }
            }
            out.push('\'');
        }
        ("$dict", Value::Array(pairs)) => {
            out.push('{');
            for (i, pair) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                if let Some(kv) = pair.as_array()
                    && kv.len() == 2
                {
                    write_py_repr(&kv[0], out);
                    out.push_str(": ");
                    write_py_repr(&kv[1], out);
                }
            }
            out.push('}');
        }
        _ => return false,
    }
    true
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => {
            let mut out = String::new();
            write_py_repr(other, &mut out);
            out
        }
    }
}

fn py_str_or_empty(v: Value) -> String {
    match v {
        Value::String(s) => s,
        Value::Null | Value::Bool(false) => String::new(),
        Value::Bool(true) => "True".into(),
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            String::new()
        }
        Value::Number(n) => fmt_num(&n),
        Value::Array(ref a) if a.is_empty() => String::new(),
        Value::Object(ref m) if m.is_empty() => String::new(),
        Value::Object(ref m) if m.len() == 1 => {
            let falsy = m.iter().next().is_some_and(|(k, x)| match k.as_str() {
                "$num" => x.as_str() == Some("-0"),
                "$int" => x.as_str() == Some("0"),
                "$tuple" | "$set" | "$bytes" | "$dict" => x.as_array().is_some_and(Vec::is_empty),
                _ => false,
            });
            if falsy { String::new() } else { py_str(&v) }
        }
        other => py_str(&other),
    }
}

fn is_sensitive(lowered: &str, cur_path: &str, p: &CompiledPolicy) -> bool {
    let exempt = !cur_path.is_empty()
        && p.safe_paths.iter().any(|e| {
            cur_path == e
                || (cur_path.len() > e.len()
                    && cur_path.ends_with(e.as_str())
                    && cur_path.as_bytes()[cur_path.len() - e.len() - 1] == b'.')
        });
    if exempt {
        return false;
    }
    p.keys.contains(lowered)
        || (!p
            .safe_suffixes
            .iter()
            .any(|s| lowered.ends_with(s.as_str()))
            && p.fragments.iter().any(|f| lowered.contains(f.as_str())))
}

fn sanitize_value(value: Value, key: &str, path: &str, policy: &CompiledPolicy) -> Value {
    let lowered = key.to_lowercase();
    let cur_path: Cow<'_, str> = match (path.is_empty(), lowered.is_empty()) {
        (true, _) => Cow::Borrowed(&lowered),
        (false, true) => Cow::Owned(format!("{path}.")),
        (false, false) => Cow::Owned(format!("{path}.{lowered}")),
    };
    if is_sensitive(&lowered, &cur_path, policy) {
        return match value {
            Value::Null => Value::Null,
            Value::String(ref s) if s.is_empty() => value,
            _ => Value::String(policy.redacted.clone()),
        };
    }
    match value {
        Value::String(s) => Value::String(sanitize_str(s, policy)),
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|x| sanitize_value(x, "", path, policy))
                .collect(),
        ),
        Value::Object(map) => sanitize_object(map, path, &cur_path, policy),
        other => other,
    }
}

fn sanitize_object(
    map: Map<String, Value>,
    path: &str,
    cur_path: &str,
    policy: &CompiledPolicy,
) -> Value {
    if map.len() == 1
        && let Some((k, _)) = map.iter().next()
    {
        match k.as_str() {
            "$num" | "$int" | "$set" | "$bytes" => return Value::Object(map),
            "$tuple" => {
                if let Some((_, Value::Array(items))) = map.into_iter().next() {
                    return Value::Array(
                        items
                            .into_iter()
                            .map(|x| sanitize_value(x, "", path, policy))
                            .collect(),
                    );
                }
                return Value::Array(Vec::new());
            }
            "$dict" => {
                let mut out = Map::new();
                if let Some((_, Value::Array(pairs))) = map.into_iter().next() {
                    for pair in pairs {
                        if let Value::Array(mut kv) = pair
                            && kv.len() == 2
                        {
                            let v = kv.pop().unwrap_or(Value::Null);
                            let ks = py_str(&kv.pop().unwrap_or(Value::Null));
                            out.insert(ks.clone(), sanitize_value(v, &ks, cur_path, policy));
                        }
                    }
                }
                return Value::Object(out);
            }
            _ => {}
        }
    }
    let mut out = Map::with_capacity(map.len());
    for (k, v) in map {
        let sv = sanitize_value(v, &k, cur_path, policy);
        out.insert(k, sv);
    }
    Value::Object(out)
}

/// Port of `sanitize_text` (`sms_tool/kernel/sanitizer.py:75`).
pub fn sanitize_text(value: serde_json::Value) -> Result<String, String> {
    Ok(sanitize_str(py_str_or_empty(value), compiled_policy()?))
}

/// Port of `sanitize_log_text` (`sms_tool/kernel/sanitizer.py:82`).
pub fn sanitize_log_text(value: serde_json::Value) -> Result<String, String> {
    let policy = compiled_policy()?;
    let mut cur = sanitize_str(py_str_or_empty(value), policy);
    if !cur.is_empty() {
        for rule in &policy.log_rules {
            if let Some(r) = apply_log_rule(&cur, rule) {
                cur = r;
            }
        }
    }
    Ok(cur)
}

/// Port of `sanitize` (`sms_tool/kernel/sanitizer.py:147`).
pub fn sanitize(
    value: serde_json::Value,
    key: Option<String>,
    path: Option<String>,
) -> Result<serde_json::Value, String> {
    let k = key.as_deref().unwrap_or("");
    let p = path.as_deref().unwrap_or("");
    Ok(sanitize_value(value, k, p, compiled_policy()?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn redacts_text_log_and_nested_values() {
        let a = sanitize_log_text(json!("user.name@example.com http://a:p@ss@h:80")).expect("log");
        assert_eq!(a, "us***@example.com http://[REDACTED]@h:80");
        assert_eq!(
            sanitize_log_text(json!("a@b.co1@c.com")).expect("lb"),
            "a***@b.co1@c.com"
        );
        let ws = sanitize_text(json!("http://u:p\x1f@h:80 Bearer\x1ftok")).expect("ws");
        assert_eq!(ws, "http://u:p\x1f@h:80 Bearer\x1f[REDACTED]");
        let input = json!({"access_token": "s", "token_count": 5, "proxy_affinity": {"session_id": "N1"}, "other": {"session_id": "S"}});
        let want = json!({"access_token": "[REDACTED]", "token_count": 5, "proxy_affinity": {"session_id": "N1"}, "other": {"session_id": "[REDACTED]"}});
        assert_eq!(sanitize(input, None, None).expect("san"), want);
    }
}
