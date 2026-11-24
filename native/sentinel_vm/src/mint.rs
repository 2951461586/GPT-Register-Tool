//! 组合入口：challenge + p + profile → `t`（以及后续的 `so`）。
//!
//! 这里只做编排：解码程序 → 建伪环境 → 跑 VM → 取输出。

use base64::Engine;

use crate::env;
use crate::token::Profile;
use crate::value::{from_json, Value};
use crate::vm::Vm;

/// `base64(XOR(JSON, key))` → 指令数组
pub fn load_program(encoded: &str, key: &str) -> Result<Vec<Vec<Value>>, String> {
    let raw = base64::engine::general_purpose::STANDARD
        .decode(encoded.as_bytes())
        .or_else(|_| base64::engine::general_purpose::STANDARD_NO_PAD.decode(encoded.as_bytes()))
        .map_err(|e| format!("base64: {e}"))?;
    let k = key.as_bytes();
    let plain: Vec<u8> = raw
        .iter()
        .enumerate()
        .map(|(i, b)| b ^ k[i % k.len()])
        .collect();
    let text = String::from_utf8(plain).map_err(|e| format!("utf8: {e}"))?;
    let parsed: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("json: {e}"))?;
    let rows = parsed.as_array().ok_or("program 不是数组")?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let items = row.as_array().ok_or("指令不是数组")?;
        out.push(items.iter().map(from_json).collect::<Vec<Value>>());
    }
    Ok(out)
}

/// 用 profile 建一个干净环境（真实指纹进载荷）
pub fn new_vm(profile: &Profile) -> Vm {
    let envp = env::EnvProfile {
        browser_family: profile.browser_family.clone(),
        user_agent: profile.user_agent.clone(),
        language: profile.language.clone(),
        languages: profile.languages.clone(),
        platform: profile.platform.clone(),
        vendor: profile.vendor.clone(),
        hardware_concurrency: profile.hardware_concurrency,
        device_memory: profile.device_memory,
        js_heap_size_limit: profile.js_heap_size_limit,
        max_touch_points: profile.max_touch_points,
        screen_width: profile.screen_width,
        screen_height: profile.screen_height,
        device_pixel_ratio: profile.device_pixel_ratio,
        page_url: profile.page_url.clone(),
        cookie: profile.cookie.clone(),
        time_origin_ms: if profile.time_origin_explicit {
            profile.page_time_origin_ms
        } else {
            0.0
        },
    };
    let window = env::build_window_for(&envp);
    Vm::new(window)
}

/// 跑 dx 程序，产出 `t`（turnstile token，base64 字符串）
pub fn mint_t(dx: &str, p: &str, profile: &Profile) -> Result<String, String> {
    let mut machine = new_vm(profile);
    mint_t_in(&mut machine, dx, p)
}

/// 复用已有 VM（连续跑 collector / snapshot 时用）
pub fn mint_t_in(machine: &mut Vm, dx: &str, p: &str) -> Result<String, String> {
    let prog = load_program(dx, p)?;
    machine.done = false;
    machine.out = None;
    machine.err = None;
    machine.run_program(prog, p).map_err(|e| e.0)?;
    Ok(machine.out.clone().unwrap_or_default())
}

/// 用 oracle 默认环境跑 dx（仅供与 Python `profile={}` 对拍）。
pub fn mint_t_oracle(dx: &str, p: &str) -> Result<String, String> {
    let mut machine = Vm::new(env::build_window("", "", 1920, 1080));
    mint_t_in(&mut machine, dx, p)
}
