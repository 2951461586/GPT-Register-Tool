//! Rust port of `extract_captcha_config` and `_playwright_proxy` from
//! `sms_tool/browser/captcha_solver.py`.

#![forbid(unsafe_code)]

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value};

struct CaptchaRegexes {
    data_sitekey: Regex,
    data_rqdata: Regex,
    js_patterns: [(&'static str, Regex); 4],
    js_rqdata: Regex,
}

fn rx() -> &'static CaptchaRegexes {
    static CACHE: OnceLock<CaptchaRegexes> = OnceLock::new();
    CACHE.get_or_init(|| CaptchaRegexes {
        data_sitekey: Regex::new(r#"(?i)data-sitekey=["']([^"']+)["']"#).expect("valid regex"),
        data_rqdata: Regex::new(r#"(?i)data-rqdata=["']([^"']*)["']"#).expect("valid regex"),
        js_patterns: [
            (
                "hcaptcha",
                Regex::new(r#"(?i)"hcaptcha_site_key"\s*:\s*"([^"]+)""#).expect("valid regex"),
            ),
            (
                "hcaptcha",
                Regex::new(r#"(?i)"site_key"\s*:\s*"([^"]+)""#).expect("valid regex"),
            ),
            (
                "recaptcha",
                Regex::new(r#"(?i)"recaptchaSiteKey"\s*:\s*"([^"]+)""#).expect("valid regex"),
            ),
            (
                "recaptcha",
                Regex::new(r#"(?i)"recaptcha_site_key"\s*:\s*"([^"]+)""#).expect("valid regex"),
            ),
        ],
        js_rqdata: Regex::new(r#"(?i)"(?:hcaptcha_)?rqdata"\s*:\s*"([^"]*)""#)
            .expect("valid regex"),
    })
}

fn make_config(captcha_type: &str, site_key: &str, rqdata: &str) -> Value {
    let mut map = Map::with_capacity(3);
    map.insert("type".to_string(), Value::String(captcha_type.to_string()));
    map.insert("site_key".to_string(), Value::String(site_key.to_string()));
    map.insert("rqdata".to_string(), Value::String(rqdata.to_string()));
    Value::Object(map)
}

pub fn extract_captcha_config(html: String) -> Result<serde_json::Value, String> {
    let html = html.as_str();
    let regs = rx();

    if let Some(caps) = regs.data_sitekey.captures(html)
        && let Some(site_key) = caps.get(1)
    {
        let rqdata = regs
            .data_rqdata
            .captures(html)
            .and_then(|c| c.get(1))
            .map_or("", |m| m.as_str());
        return Ok(make_config("hcaptcha", site_key.as_str(), rqdata));
    }

    for (captcha_type, pat) in &regs.js_patterns {
        if let Some(caps) = pat.captures(html)
            && let Some(site_key) = caps.get(1)
        {
            let rqdata = regs
                .js_rqdata
                .captures(html)
                .and_then(|c| c.get(1))
                .map_or("", |m| m.as_str());
            return Ok(make_config(captcha_type, site_key.as_str(), rqdata));
        }
    }

    Ok(make_config("", "", ""))
}

#[inline]
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}' | '\u{0085}')
}

#[inline]
fn py_trim(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

struct ParsedUrl {
    scheme: String,
    username: Option<String>,
    password: Option<String>,
    hostname: String,
    port: Option<u16>,
}

fn py_urlparse_authority(url: &str) -> Option<ParsedUrl> {
    let trimmed = url.trim_matches(|c: char| (c as u32) <= 0x20);
    let cleaned: String = trimmed
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let s = cleaned.as_str();
    let (scheme, rest) = if let Some(idx) = s.find(':') {
        let cand = &s[..idx];
        let valid_scheme = !cand.is_empty()
            && cand.as_bytes()[0].is_ascii_alphabetic()
            && cand
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'));
        if valid_scheme {
            (cand.to_ascii_lowercase(), &s[idx + 1..])
        } else {
            (String::new(), s)
        }
    } else {
        (String::new(), s)
    };

    let (before_frag, _) = rest.split_once('#').unwrap_or((rest, ""));
    let (before_query, _) = before_frag.split_once('?').unwrap_or((before_frag, ""));
    let netloc = if let Some(after) = before_query.strip_prefix("//") {
        let split_at = after.find('/').unwrap_or(after.len());
        &after[..split_at]
    } else {
        ""
    };

    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return None;
    }

    let (username, password, hostport) = if let Some((ui, hp)) = netloc.rsplit_once('@') {
        if let Some((u, p)) = ui.split_once(':') {
            (Some(u.to_string()), Some(p.to_string()), hp)
        } else {
            (Some(ui.to_string()), None, hp)
        }
    } else {
        (None, None, netloc)
    };

    let (hostname, port) = if hostport.starts_with('[') {
        let close = hostport.find(']')?;
        let host = hostport[1..close].to_lowercase();
        let addr_part = host.split_once('%').map_or(host.as_str(), |(a, _)| a);
        if addr_part.parse::<std::net::Ipv4Addr>().is_ok()
            || addr_part.parse::<std::net::Ipv6Addr>().is_err()
        {
            return None;
        }
        let tail = &hostport[close + 1..];
        let port = if let Some(ps) = tail.strip_prefix(':') {
            if ps.is_empty() {
                None
            } else {
                if !ps.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                let p: u32 = ps.parse().ok()?;
                if p > 65535 {
                    return None;
                }
                Some(p as u16)
            }
        } else {
            None
        };
        (host, port)
    } else if hostport.contains('[') || hostport.contains(']') {
        return None;
    } else if let Some((h, ps)) = hostport.rsplit_once(':') {
        let port = if ps.is_empty() {
            None
        } else {
            if !ps.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let p: u32 = ps.parse().ok()?;
            if p > 65535 {
                return None;
            }
            Some(p as u16)
        };
        (h.to_lowercase(), port)
    } else {
        (hostport.to_lowercase(), None)
    };

    Some(ParsedUrl {
        scheme,
        username,
        password,
        hostname,
        port,
    })
}

pub fn _playwright_proxy(
    proxy_url: serde_json::Value,
) -> Result<Option<serde_json::Value>, String> {
    let raw = match &proxy_url {
        Value::Null | Value::Bool(false) => return Ok(None),
        Value::Number(n)
            if n.as_i64() == Some(0) || n.as_u64() == Some(0) || n.as_f64() == Some(0.0) =>
        {
            return Ok(None);
        }
        Value::Array(a) if a.is_empty() => return Ok(None),
        Value::Object(o) if o.is_empty() => return Ok(None),
        Value::String(s) => s.as_str(),
        _ => return Err("AttributeError: object has no attribute 'strip'".to_string()),
    };
    let trimmed = py_trim(raw);
    if trimmed.is_empty() {
        return Ok(None);
    }
    let Some(parsed) = py_urlparse_authority(trimmed) else {
        return Ok(None);
    };
    if parsed.hostname.is_empty() {
        return Ok(None);
    }
    let scheme_base = if parsed.scheme.is_empty() {
        "http".to_string()
    } else {
        parsed.scheme.replace("socks5h", "socks5")
    };
    let mut server = format!("{scheme_base}://{}", parsed.hostname);
    if let Some(p) = parsed.port
        && p != 0
    {
        server.push(':');
        server.push_str(&p.to_string());
    }
    let mut result = Map::with_capacity(4);
    result.insert("server".to_string(), Value::String(server));
    result.insert(
        "bypass".to_string(),
        Value::String("127.0.0.1,localhost".to_string()),
    );
    if let Some(u) = parsed.username
        && !u.is_empty()
    {
        result.insert("username".to_string(), Value::String(u));
    }
    if let Some(p) = parsed.password
        && !p.is_empty()
    {
        result.insert("password".to_string(), Value::String(p));
    }
    Ok(Some(Value::Object(result)))
}

fn push_py_json_str(out: &mut String, s: &str, ensure_ascii: bool) {
    use std::fmt::Write;
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
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ensure_ascii && (c as u32) >= 0x80 => {
                let code = c as u32;
                if code <= 0xffff {
                    let _ = write!(out, "\\u{code:04x}");
                } else {
                    let mut units = [0u16; 2];
                    for u in c.encode_utf16(&mut units) {
                        let _ = write!(out, "\\u{u:04x}");
                    }
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn _build_hcaptcha_bridge_html(
    frame_id: String,
    wrapper_url: String,
    site_key: String,
    rqdata: String,
    merchant_id: String,
    locale: String,
) -> Result<String, String> {
    let mut out = String::with_capacity(3072 + wrapper_url.len() + rqdata.len());
    out.push_str(
        r#"<!doctype html>
<html>
  <head><meta charset="utf-8"><title>hCaptcha Bridge</title></head>
  <body>
    <iframe id="captchaFrame" src=""#,
    );
    out.push_str(&wrapper_url);
    out.push_str(
        r#"" style="width:420px;height:720px;border:0"></iframe>
    <script>
      const frameID = "#,
    );
    push_py_json_str(&mut out, &frame_id, true);
    out.push_str(
        r#";
      const initPayload = {"tag": "INITIALIZE_HCAPTCHA_INVISIBLE", "message": {"sitekey": "#,
    );
    push_py_json_str(&mut out, &site_key, false);
    out.push_str(
        r#"}};
      const executePayload = {"tag": "EXECUTE_HCAPTCHA_INVISIBLE", "message": {"sitekey": "#,
    );
    push_py_json_str(&mut out, &site_key, false);
    out.push_str(r#", "rqdata": "#);
    push_py_json_str(&mut out, &rqdata, false);
    out.push_str(r#", "data": {"merchant_id": "#);
    push_py_json_str(&mut out, &merchant_id, false);
    out.push_str(r#", "locale": "#);
    push_py_json_str(&mut out, &locale, false);
    out.push_str(
        r#", "flow": "passive_captcha", "captcha_vendor": "hcaptcha"}}};
      const signalPayloads = [{"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE", "message": {"type": "mouse", "eventName": "mousemove", "coordinates": {"x": 168, "y": 132}}}, {"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE", "message": {"type": "pointer", "eventName": "pointermove", "coordinates": {"x": 214, "y": 176}}}, {"tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE", "message": {"type": "keyboard", "eventName": "keydown"}}];
      let initialized = false;
      let executed = false;

      function postToBridge(path, payload) {
        fetch(path, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload || {}),
          keepalive: true,
        }).catch(() => {});
      }

      function postToChild(source, origin, payload) {
        source.postMessage({
          type: "stripe-third-party-parent-to-child",
          frameID,
          payload,
        }, origin);
      }

      function initialize(source, origin) {
        if (initialized) return;
        initialized = true;
        postToBridge("/event", {type: "invisible_initialize"});
        postToChild(source, origin, initPayload);
      }

      function execute(source, origin) {
        if (executed) return;
        executed = true;
        signalPayloads.forEach((payload, idx) => setTimeout(() => postToChild(source, origin, payload), 50 * idx));
        setTimeout(() => {
          postToBridge("/event", {type: "invisible_execute"});
          postToChild(source, origin, executePayload);
        }, 180);
      }

      window.addEventListener("message", (event) => {
        const data = event.data || {};
        if (data.type === "stripe-third-party-frame-ready" && data.frameID === frameID) {
          postToBridge("/event", {type: "frame_ready", origin: event.origin});
          initialize(event.source, event.origin);
          return;
        }
        if (data.type !== "stripe-third-party-child-to-parent" || data.frameID !== frameID) return;
        const payload = data.payload || {};
        postToBridge("/event", {type: "child_payload", tag: payload.tag || ""});
        if (payload.tag === "LOAD_HCAPTCHA_INVISIBLE") {
          execute(event.source, event.origin);
          return;
        }
        if (payload.tag === "RESPONSE_HCAPTCHA_INVISIBLE") {
          const value = payload.value || {};
          postToBridge("/result", {
            response: value.response || "",
            ekey: value.key || "",
            duration: value.duration || 0,
            raw: payload,
          });
          return;
        }
        if (payload.tag === "ERROR_HCAPTCHA_INVISIBLE") {
          postToBridge("/error", {error: (payload.value || {}).error || "unknown_error", raw: payload});
        }
      });
    </script>
  </body>
</html>
"#,
    );
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_hcaptcha_and_recaptcha() {
        let html = r#"<div data-sitekey="abc-123" data-rqdata="rq-xyz"></div>"#;
        assert_eq!(
            extract_captcha_config(html.to_string()).unwrap(),
            json!({"type": "hcaptcha", "site_key": "abc-123", "rqdata": "rq-xyz"})
        );
        let js_html =
            r#"<script>var cfg = {"recaptchaSiteKey": "6Ltest", "rqdata": "r1"};</script>"#;
        assert_eq!(
            extract_captcha_config(js_html.to_string()).unwrap(),
            json!({"type": "recaptcha", "site_key": "6Ltest", "rqdata": "r1"})
        );
        assert_eq!(
            extract_captcha_config("<html><body>hello</body></html>".to_string()).unwrap(),
            json!({"type": "", "site_key": "", "rqdata": ""})
        );
    }

    #[test]
    fn converts_playwright_proxy() {
        assert_eq!(_playwright_proxy(json!(null)).unwrap(), None);
        assert_eq!(
            _playwright_proxy(json!("socks5h://u1:p2@proxy.example:1080")).unwrap(),
            Some(json!({
                "server": "socks5://proxy.example:1080",
                "bypass": "127.0.0.1,localhost",
                "username": "u1",
                "password": "p2"
            }))
        );
    }
}
