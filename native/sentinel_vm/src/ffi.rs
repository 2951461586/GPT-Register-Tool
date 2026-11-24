//! C ABI exports for Python ctypes integration (`sentinel_vm.dll` / `.so` / `.dylib`).
//!
//! Contract:
//! * Every string-returning symbol allocates a UTF-8 NUL-terminated `*mut c_char`;
//!   the caller must release it via [`srs_free`].
//! * Errors are returned as strings prefixed with `ERR:` and Rust panics are caught
//!   inside [`guard`] so they never unwind across the FFI boundary.

use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::panic::{catch_unwind, AssertUnwindSafe};

use serde_json::{json, Value as JsonValue};

use crate::mint;
use crate::so;
use crate::token::{enforcement_token, requirements_token, Clock, Profile, ProfileOptions, Rng, TokenCtx};
use crate::value::{obj_get, to_string, visible_keys, Value};

fn to_c(s: String) -> *mut c_char {
    match CString::new(s) {
        Ok(c) => c.into_raw(),
        Err(_) => CString::new("ERR: result contains NUL byte").unwrap().into_raw(),
    }
}

unsafe fn read_str(ptr: *const c_char) -> Result<String, String> {
    if ptr.is_null() {
        return Err("ERR: null pointer".into());
    }
    CStr::from_ptr(ptr)
        .to_str()
        .map(|s| s.to_string())
        .map_err(|e| format!("ERR: invalid UTF-8: {e}"))
}

fn now_epoch_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64() * 1000.0)
        .unwrap_or(0.0)
}

fn profile_from_json(fp_json: &str, rng: &mut Rng) -> Result<Profile, String> {
    let now_ms = now_epoch_ms();
    let trimmed = fp_json.trim();
    if trimmed.is_empty() || trimmed == "null" {
        return Ok(Profile::new(&ProfileOptions::default(), now_ms, rng));
    }
    let fp: JsonValue =
        serde_json::from_str(trimmed).map_err(|e| format!("ERR: invalid fingerprint JSON: {e}"))?;
    Profile::from_fingerprint(&fp, now_ms, rng)
        .map_err(|e| format!("ERR: profile build failed: {e}"))
}

fn guard<F: FnOnce() -> String>(f: F) -> *mut c_char {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(s) => to_c(s),
        Err(_) => to_c("ERR: Rust panic caught".into()),
    }
}

fn json_truthy(v: Option<&JsonValue>) -> bool {
    match v {
        None | Some(JsonValue::Null) => false,
        Some(JsonValue::Bool(b)) => *b,
        Some(JsonValue::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(JsonValue::String(s)) => !s.is_empty(),
        Some(JsonValue::Array(a)) => !a.is_empty(),
        Some(JsonValue::Object(o)) => !o.is_empty(),
    }
}

fn json_str(v: Option<&JsonValue>) -> String {
    match v {
        None | Some(JsonValue::Null) => String::new(),
        Some(JsonValue::String(s)) => s.clone(),
        Some(JsonValue::Bool(b)) => b.to_string(),
        Some(JsonValue::Number(n)) => n.to_string(),
        Some(_) => String::new(),
    }
}

/// Library version string.
#[no_mangle]
pub extern "C" fn srs_version() -> *mut c_char {
    to_c(env!("CARGO_PKG_VERSION").to_string())
}

/// Release a string returned by this library.
#[no_mangle]
pub unsafe extern "C" fn srs_free(ptr: *mut c_char) {
    if !ptr.is_null() {
        drop(CString::from_raw(ptr));
    }
}

/// Execute turnstile `dx` bytecode against `p` and return `t`.
#[no_mangle]
pub unsafe extern "C" fn srs_mint_t(
    dx: *const c_char,
    p: *const c_char,
    fp_json: *const c_char,
) -> *mut c_char {
    let dx = match read_str(dx) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let p = match read_str(p) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let fp = read_str(fp_json).unwrap_or_else(|_| "null".to_string());
    guard(move || {
        let mut rng = Rng::system();
        match profile_from_json(&fp, &mut rng) {
            Ok(profile) => match mint::mint_t(&dx, &p, &profile) {
                Ok(t) => t,
                Err(e) => format!("ERR: mint_t failed: {e}"),
            },
            Err(e) => e,
        }
    })
}

/// Execute session-observer collector + synthetic behavior + snapshot to mint `so`.
#[no_mangle]
pub unsafe extern "C" fn srs_mint_so(
    collector_dx: *const c_char,
    snapshot_dx: *const c_char,
    p: *const c_char,
    fp_json: *const c_char,
) -> *mut c_char {
    let collector = match read_str(collector_dx) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let snapshot = match read_str(snapshot_dx) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let p = match read_str(p) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let fp = read_str(fp_json).unwrap_or_else(|_| "null".to_string());
    guard(move || {
        let mut rng = Rng::system();
        match profile_from_json(&fp, &mut rng) {
            Ok(profile) => match so::mint_so_full(&collector, &snapshot, &p, &profile, &mut rng) {
                Ok(out) => out.so,
                Err(e) => format!("ERR: mint_so failed: {e}"),
            },
            Err(e) => e,
        }
    })
}

/// Generate a requirements PoW token (`gAAAAAC...~S`).
#[no_mangle]
pub unsafe extern "C" fn srs_requirements_token(fp_json: *const c_char) -> *mut c_char {
    let fp = read_str(fp_json).unwrap_or_else(|_| "null".to_string());
    guard(move || {
        let mut rng = Rng::system();
        let profile = match profile_from_json(&fp, &mut rng) {
            Ok(p) => p,
            Err(e) => return e,
        };
        let parsed: Option<JsonValue> = serde_json::from_str(&fp).ok();
        let sid = parsed
            .as_ref()
            .and_then(|v| v.get("session_id"))
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let now_ms = now_epoch_ms();
        let mut ctx = TokenCtx::with_clock(now_ms, Rng::system(), Clock::system());
        let seed = uuid::Uuid::new_v4().to_string();
        match requirements_token(&profile, &sid, &seed, &mut ctx) {
            Ok(tok) => tok,
            Err(e) => format!("ERR: requirements_token failed: {e}"),
        }
    })
}

/// Solve a full server Sentinel challenge (PoW + Turnstile VM + optional SO VM).
///
/// `opts_json` carries:
/// `flow`, `device_id`, `challenge_proof` (optional XOR key), `session_id` (optional),
/// and fingerprint fields accepted by `Profile::from_fingerprint`.
///
/// Returns compact JSON: `{"p": ..., "t": ..., "c": ..., "id": ..., "flow": ..., ("_so": ...)}`.
#[no_mangle]
pub unsafe extern "C" fn srs_solve_challenge(
    challenge_json: *const c_char,
    opts_json: *const c_char,
) -> *mut c_char {
    let ch_raw = match read_str(challenge_json) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    let opts_raw = match read_str(opts_json) {
        Ok(v) => v,
        Err(e) => return to_c(e),
    };
    guard(move || solve_challenge_impl(&ch_raw, &opts_raw))
}

fn solve_challenge_impl(ch_raw: &str, opts_raw: &str) -> String {
    let challenge: JsonValue = match serde_json::from_str::<JsonValue>(ch_raw) {
        Ok(v) if v.is_object() => v,
        Ok(_) => return "ERR: sentinel_runner_invalid_challenge".into(),
        Err(e) => return format!("ERR: sentinel_runner_invalid_challenge: {e}"),
    };
    let opts: JsonValue = match serde_json::from_str::<JsonValue>(opts_raw) {
        Ok(v) if v.is_object() => v,
        Ok(_) => return "ERR: invalid opts JSON".into(),
        Err(e) => return format!("ERR: invalid opts JSON: {e}"),
    };

    let flow = json_str(opts.get("flow")).trim().to_string();
    let device_id = json_str(opts.get("device_id")).trim().to_string();
    let c_value = json_str(challenge.get("token")).trim().to_string();
    if flow.is_empty() {
        return "ERR: sentinel_runner_invalid_flow".into();
    }
    if device_id.is_empty() {
        return "ERR: sentinel_runner_missing_device_id".into();
    }
    if c_value.is_empty() {
        return "ERR: sentinel_runner_invalid_challenge".into();
    }

    let mut rng = Rng::system();
    let now_ms = now_epoch_ms();
    let profile = match Profile::from_fingerprint(&opts, now_ms, &mut rng) {
        Ok(p) => p,
        Err(e) => return format!("ERR: profile build failed: {e}"),
    };

    let sid = {
        let s = json_str(opts.get("session_id")).trim().to_string();
        if s.is_empty() {
            uuid::Uuid::new_v4().to_string()
        } else {
            s
        }
    };
    let mut ctx = TokenCtx::with_clock(now_ms, Rng::system(), Clock::system());

    // 1. Enforcement PoW token (`p`), or JSON `null` when proofofwork.required is false.
    let p_val: JsonValue = match enforcement_token(&challenge, &profile, &sid, &mut ctx) {
        Ok(Some(tok)) => JsonValue::String(tok),
        Ok(None) => JsonValue::Null,
        Err(e) => return format!("ERR: enforcement_token failed: {e}"),
    };

    // XOR key for turnstile.dx and so.*.dx: bound to the fetching requirements proof.
    let mut xor_key = json_str(opts.get("challenge_proof")).trim().to_string();
    let turnstile_req = json_truthy(challenge.pointer("/turnstile/required"));
    let turnstile_dx = challenge
        .pointer("/turnstile/dx")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let so_req = json_truthy(challenge.pointer("/so/required"));
    let collector_dx = challenge
        .pointer("/so/collector_dx")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let snapshot_dx = challenge
        .pointer("/so/snapshot_dx")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");

    if xor_key.is_empty() && ((turnstile_req && turnstile_dx.is_some()) || (so_req && collector_dx.is_some())) {
        let seed = uuid::Uuid::new_v4().to_string();
        match requirements_token(&profile, &sid, &seed, &mut ctx) {
            Ok(tok) => xor_key = tok,
            Err(e) => return format!("ERR: requirements_token failed: {e}"),
        }
    }

    // 2. Turnstile VM (`t`), or JSON `null` when turnstile.required is false.
    let t_val: JsonValue = if turnstile_req {
        match turnstile_dx {
            Some(dx) => match mint::mint_t(dx, &xor_key, &profile) {
                Ok(t) => JsonValue::String(t),
                Err(e) => return format!("ERR: mint_t failed: {e}"),
            },
            None => JsonValue::String(String::new()),
        }
    } else {
        JsonValue::Null
    };

    // 3. Optional Session Observer VM (`_so`).
    let so_blob: Option<String> = if so_req {
        if let Some(collector) = collector_dx {
            match so::mint_so_full(collector, snapshot_dx, &xor_key, &profile, &mut rng) {
                Ok(out) if !out.so.is_empty() => Some(out.so),
                _ => None,
            }
        } else {
            None
        }
    } else {
        None
    };

    let mut payload = serde_json::Map::new();
    payload.insert("p".to_string(), p_val);
    payload.insert("t".to_string(), t_val);
    payload.insert("c".to_string(), JsonValue::String(c_value));
    payload.insert("id".to_string(), JsonValue::String(device_id));
    payload.insert("flow".to_string(), JsonValue::String(flow));
    if let Some(so_val) = so_blob {
        payload.insert("_so".to_string(), JsonValue::String(so_val));
    }
    JsonValue::Object(payload).to_string()
}

/// Inspect the constructed VM environment for a fingerprint JSON (used by offline consistency tests).
#[no_mangle]
pub unsafe extern "C" fn srs_probe_env(fp_json: *const c_char) -> *mut c_char {
    let fp = read_str(fp_json).unwrap_or_else(|_| "null".to_string());
    guard(move || probe_env_impl(&fp))
}

fn probe_env_impl(fp: &str) -> String {
    let mut rng = Rng::system();
    let profile = match profile_from_json(fp, &mut rng) {
        Ok(p) => p,
        Err(e) => return e,
    };
    let vm = mint::new_vm(&profile);
    let win = &vm.window;
    let nav = match obj_get(win, "navigator") {
        Some(Value::Obj(o)) => Some(o),
        _ => None,
    };
    let perf = match obj_get(win, "performance") {
        Some(Value::Obj(o)) => Some(o),
        _ => None,
    };
    let doc = match obj_get(win, "document") {
        Some(Value::Obj(o)) => Some(o),
        _ => None,
    };
    let nav_has = |k: &str| nav.as_ref().and_then(|o| obj_get(o, k)).is_some();
    let nav_str = |k: &str| nav.as_ref().and_then(|o| obj_get(o, k)).map(|v| to_string(&v)).unwrap_or_default();
    let nav_num = |k: &str| match nav.as_ref().and_then(|o| obj_get(o, k)) {
        Some(Value::Num(n)) => n as i64,
        _ => 0,
    };
    json!({
        "browserFamily": profile.browser_family,
        "hasUserAgentData": nav_has("userAgentData"),
        "hasDeviceMemory": nav_has("deviceMemory"),
        "hasGpu": nav_has("gpu"),
        "hasLogin": nav_has("login"),
        "hasChrome": obj_get(win, "chrome").is_some(),
        "hasSafari": obj_get(win, "safari").is_some(),
        "hasInstallTrigger": obj_get(win, "InstallTrigger").is_some(),
        "hasMemory": perf.as_ref().and_then(|o| obj_get(o, "memory")).is_some(),
        "vendor": nav_str("vendor"),
        "platform": nav_str("platform"),
        "maxTouchPoints": nav_num("maxTouchPoints"),
        "navKeys": nav.as_ref().map(visible_keys).unwrap_or_default(),
        "docKeys": doc.as_ref().map(visible_keys).unwrap_or_default(),
        "winKeys": visible_keys(win),
    })
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine as _;

    #[test]
    fn solve_offline_challenge_matches_sdk_shape() {
        let ch = json!({
            "token": "selfcheck-challenge",
            "proofofwork": {"required": false},
            "turnstile": {"required": false},
            "so": {"required": false},
        });
        let opts = json!({
            "flow": "authorize_continue",
            "device_id": "00000000-0000-4000-8000-000000000000",
            "browser_family": "chrome",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0",
            "lang": "en-US",
            "timezone": "America/New_York",
            "timezone_offset_minutes": -300,
        });
        let out = solve_challenge_impl(&ch.to_string(), &opts.to_string());
        let parsed: JsonValue = serde_json::from_str(&out).expect("valid json");
        assert_eq!(parsed["c"], "selfcheck-challenge");
        assert_eq!(parsed["flow"], "authorize_continue");
        assert_eq!(parsed["id"], "00000000-0000-4000-8000-000000000000");
        assert!(parsed["p"].is_null());
        assert!(parsed["t"].is_null());
    }

    #[test]
    fn solve_pow_challenge_gates_heap_and_install_trigger_by_family() {
        let ch = json!({
            "token": "parity-challenge",
            "proofofwork": {"required": true, "seed": "1699999999.4242", "difficulty": "f"},
            "turnstile": {"required": false},
            "so": {"required": false},
        });
        for (family, expect_heap, expect_trigger) in [
            ("chrome", true, 0i64),
            ("firefox", false, 1i64),
            ("safari", false, 0i64),
        ] {
            let opts = json!({
                "flow": "authorize_continue",
                "device_id": "33333333-3333-4333-8333-333333333333",
                "browser_family": family,
                "user_agent": "UA",
                "lang": "en-US",
                "lang_full": "en-US,en;q=0.9",
                "screen": "1920x1080",
                "timezone": "UTC",
                "timezone_name": "Coordinated Universal Time",
                "timezone_offset_minutes": 0,
                "js_heap_size_limit": 3221225472i64,
                "script_src": "https://sentinel.openai.com/sentinel/20260810913b/sdk.js",
                "session_id": "sid-1",
            });
            let out = solve_challenge_impl(&ch.to_string(), &opts.to_string());
            let parsed: JsonValue = serde_json::from_str(&out).expect("valid json");
            let p_str = parsed["p"].as_str().expect("p string");
            assert!(p_str.starts_with("gAAAAAB") && p_str.ends_with("~S"));
            let body = &p_str[7..p_str.len() - 2];
            let raw = base64::engine::general_purpose::STANDARD.decode(body).expect("b64");
            let arr: Vec<JsonValue> = serde_json::from_slice(&raw).expect("array");
            assert_eq!(arr.len(), 25);
            assert_eq!(arr[2].is_i64(), expect_heap, "family={family}");
            assert_eq!(arr[5], "https://sentinel.openai.com/sentinel/20260810913b/sdk.js");
            assert_eq!(arr[24].as_i64(), Some(expect_trigger), "family={family}");
        }
    }

    #[test]
    fn probe_env_enforces_family_consistency() {
        let chrome: JsonValue = serde_json::from_str(&probe_env_impl(
            &json!({"browser_family": "chrome", "navigator_vendor": "Google Inc."}).to_string(),
        ))
        .unwrap();
        assert_eq!(chrome["hasChrome"], true);
        assert_eq!(chrome["hasMemory"], true);
        assert_eq!(chrome["hasDeviceMemory"], true);
        assert_eq!(chrome["hasUserAgentData"], true);
        assert_eq!(chrome["hasGpu"], true);
        assert_eq!(chrome["hasInstallTrigger"], false);

        let firefox: JsonValue = serde_json::from_str(&probe_env_impl(
            &json!({"browser_family": "firefox", "navigator_vendor": ""}).to_string(),
        ))
        .unwrap();
        assert_eq!(firefox["hasChrome"], false);
        assert_eq!(firefox["hasMemory"], false);
        assert_eq!(firefox["hasDeviceMemory"], false);
        assert_eq!(firefox["hasUserAgentData"], false);
        assert_eq!(firefox["hasGpu"], false);
        assert_eq!(firefox["hasInstallTrigger"], true);
        assert_eq!(firefox["vendor"], "");

        let safari: JsonValue = serde_json::from_str(&probe_env_impl(
            &json!({"browser_family": "safari", "navigator_vendor": "Apple Computer, Inc.", "max_touch_points": 5})
                .to_string(),
        ))
        .unwrap();
        assert_eq!(safari["hasChrome"], false);
        assert_eq!(safari["hasMemory"], false);
        assert_eq!(safari["hasDeviceMemory"], false);
        assert_eq!(safari["hasUserAgentData"], false);
        assert_eq!(safari["hasSafari"], true);
        assert_eq!(safari["maxTouchPoints"], 5);
    }
}
