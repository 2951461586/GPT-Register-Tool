//! 伪 window：直接从 Python oracle 导出的 `src/env_dump.json` 重建。
//!
//! 背景：环境字段（screen/navigator/performance/Math…）会直接进入 sentinel 的
//! token 载荷，任何一处差异都会让 `t` 变化。所以这里不再手写字段，而是由
//! `tools/dump_env.py` 把 Python VM 的 `ctx.window` 递归导出成 JSON，本模块按
//! **导出顺序** 原样重建：
//!
//! * `{"$o": [[k, v], ...]}` — 有序键值对（serde_json 的 map 会排序，所以键序
//!   必须放在数组里）
//! * `{"$fn": path, "$b": behaviour}` — 环境函数，Rust 侧按 `$b` 派发
//! * `{"$self": true}` — 回指 window（self/top/parent/globalThis）
//! * `{"$k": "d"|"p"}` — `d` = Python dict（可 JSON 序列化），`p` = stub 实例
//! * `{"$wild": ...}` — Python 侧带 `__getattr__` 兜底的 stub
//!
//! 重新生成：
//! `python tools/dump_env.py`（固定 profile={}、device_id=x 的 oracle 环境）
use crate::value::*;
use std::rc::Rc;

const ENV_DUMP: &str = include_str!("env_dump.json");

fn s(v: &str) -> Value {
    Value::Str(Rc::from(v))
}

/// 无接收者的环境函数
pub fn b(name: &str) -> Value {
    Value::Env(Rc::new(EnvFn { b: Rc::from(name), this: None }))
}

/// 绑定到某个对象的环境函数（= Python 的 bound method）
pub fn bm(name: &str, this: &ObjRef) -> Value {
    Value::Env(Rc::new(EnvFn {
        b: Rc::from(name),
        this: Some(Value::Obj(this.clone())),
    }))
}

/// ElementStub 的事件属性（顺序与 Python 侧一致）
const ELEMENT_EVENTS: [&str; 12] = [
    "oninput", "onchange", "onclick", "onkeydown", "onkeyup", "onpaste", "onfocus", "onblur",
    "onsubmit", "onmousemove", "onmouseover", "onscroll",
];

/// DocumentStub / ElementStub 方法表（行为名与 dump_env.py 的 `$b` 一致）
const ELEMENT_METHODS: [(&str, &str); 14] = [
    ("appendChild", "ElementStub.appendChild"),
    ("removeChild", "ElementStub.removeChild"),
    ("insertBefore", "ElementStub.insertBefore"),
    ("getAttribute", "ElementStub.getAttribute"),
    ("setAttribute", "ElementStub.setAttribute"),
    ("getContext", "ElementStub.getContext"),
    ("toDataURL", "ElementStub.toDataURL"),
    ("toBlob", "ElementStub.toBlob"),
    ("getBoundingClientRect", "ElementStub.getBoundingClientRect"),
    ("getClientRects", "ElementStub.getClientRects"),
    ("addEventListener", "ElementStub.addEventListener"),
    ("removeEventListener", "ElementStub.removeEventListener"),
    ("querySelector", "ElementStub.querySelector"),
    ("querySelectorAll", "ElementStub.querySelectorAll"),
];

/// `ElementStub(tag)`：document.createElement / createElementNS 等返回的节点。
pub fn element(tag: &str) -> ObjRef {
    let obj = new_obj(vec![]);
    let mut pairs: Vec<(String, Value)> = Vec::new();
    pairs.push(("tagName".into(), s(&tag.to_uppercase())));
    pairs.push(("style".into(), Value::Obj(new_obj(vec![]))));
    pairs.push(("children".into(), Value::Arr(new_arr(vec![]))));
    pairs.push(("parentNode".into(), Value::Null));
    pairs.push(("value".into(), s("")));
    pairs.push(("checked".into(), Value::Bool(false)));
    pairs.push(("disabled".into(), Value::Bool(false)));
    for ev in ELEMENT_EVENTS {
        pairs.push((ev.to_string(), Value::Null));
    }
    for (name, behaviour) in ELEMENT_METHODS {
        pairs.push((name.to_string(), bm(behaviour, &obj)));
    }
    pairs.push((HIDDEN_KIND.to_string(), s("p")));
    *obj.borrow_mut() = pairs;
    obj
}

/// `_Ctx2D()`
pub fn ctx2d() -> ObjRef {
    let obj = new_obj(vec![]);
    let mut pairs: Vec<(String, Value)> = Vec::new();
    pairs.push(("canvas".into(), Value::Null));
    pairs.push(("fillStyle".into(), s("")));
    pairs.push(("strokeStyle".into(), s("")));
    pairs.push(("lineWidth".into(), Value::Num(1.0)));
    pairs.push(("font".into(), s("10px sans-serif")));
    pairs.push(("textAlign".into(), s("start")));
    pairs.push(("textBaseline".into(), s("alphabetic")));
    pairs.push(("globalAlpha".into(), Value::Num(1.0)));
    pairs.push(("globalCompositeOperation".into(), s("source-over")));
    pairs.push(("measureText".into(), bm("Ctx2D.measureText", &obj)));
    pairs.push(("getImageData".into(), bm("Ctx2D.getImageData", &obj)));
    pairs.push(("createImageData".into(), bm("Ctx2D.createImageData", &obj)));
    pairs.push(("createLinearGradient".into(), bm("Ctx2D.gradient", &obj)));
    pairs.push(("createRadialGradient".into(), bm("Ctx2D.gradient", &obj)));
    pairs.push((HIDDEN_KIND.to_string(), s("p")));
    pairs.push((HIDDEN_WILD.to_string(), s("wild_none")));
    *obj.borrow_mut() = pairs;
    obj
}

/// `_WebGL(canvas)`
pub fn webgl(canvas: &Value) -> ObjRef {
    let obj = new_obj(vec![]);
    let mut pairs: Vec<(String, Value)> = Vec::new();
    pairs.push(("canvas".into(), canvas.clone()));
    pairs.push(("VENDOR".into(), Value::Num(7936.0)));
    pairs.push(("RENDERER".into(), Value::Num(7937.0)));
    pairs.push(("UNMASKED_VENDOR_WEBGL".into(), Value::Num(37445.0)));
    pairs.push(("UNMASKED_RENDERER_WEBGL".into(), Value::Num(37446.0)));
    pairs.push(("getExtension".into(), bm("WebGL.getExtension", &obj)));
    pairs.push(("getParameter".into(), bm("WebGL.getParameter", &obj)));
    pairs.push((
        "getSupportedExtensions".into(),
        bm("WebGL.getSupportedExtensions", &obj),
    ));
    pairs.push((HIDDEN_KIND.to_string(), s("p")));
    pairs.push((HIDDEN_WILD.to_string(), s("wild_obj")));
    *obj.borrow_mut() = pairs;
    obj
}

/// `StorageStub()`：localStorage / sessionStorage
pub fn storage() -> ObjRef {
    let obj = new_obj(vec![]);
    let mut pairs: Vec<(String, Value)> = Vec::new();
    pairs.push(("_d".into(), Value::Obj(new_obj(vec![]))));
    for (name, behaviour) in [
        ("getItem", "StorageStub.getItem"),
        ("setItem", "StorageStub.setItem"),
        ("removeItem", "StorageStub.removeItem"),
        ("clear", "StorageStub.clear"),
        ("key", "StorageStub.key"),
    ] {
        pairs.push((name.to_string(), bm(behaviour, &obj)));
    }
    pairs.push(("length".into(), Value::Num(0.0)));
    pairs.push((HIDDEN_KIND.to_string(), s("p")));
    *obj.borrow_mut() = pairs;
    obj
}

/// `SetStub()` / `MapStub()` / `WeakSetStub()` / `WeakMapStub()`
pub fn set_like() -> ObjRef {
    let obj = new_obj(vec![]);
    let mut pairs: Vec<(String, Value)> = Vec::new();
    pairs.push(("_s".into(), Value::Arr(new_arr(vec![]))));
    for (name, behaviour) in [
        ("add", "SetStub.add"),
        ("has", "SetStub.has"),
        ("delete", "SetStub.delete"),
        ("clear", "SetStub.clear"),
        ("values", "SetStub.values"),
        ("keys", "SetStub.keys"),
        ("forEach", "SetStub.forEach"),
    ] {
        pairs.push((name.to_string(), bm(behaviour, &obj)));
    }
    pairs.push(("size".into(), Value::Num(0.0)));
    pairs.push((HIDDEN_KIND.to_string(), s("p")));
    *obj.borrow_mut() = pairs;
    obj
}

// ------------------------------------------------------------ dump 重建

fn build_node(node: &serde_json::Value, root: &ObjRef, this: Option<&ObjRef>) -> Value {
    match node {
        serde_json::Value::Null => Value::Null,
        serde_json::Value::Bool(v) => Value::Bool(*v),
        serde_json::Value::Number(n) => Value::Num(n.as_f64().unwrap_or(f64::NAN)),
        serde_json::Value::String(v) => s(v),
        serde_json::Value::Array(items) => {
            Value::Arr(new_arr(items.iter().map(|x| build_node(x, root, None)).collect()))
        }
        serde_json::Value::Object(map) => {
            if let Some(special) = map.get("$num").and_then(|v| v.as_str()) {
                return Value::Num(match special {
                    "NaN" => f64::NAN,
                    "Infinity" => f64::INFINITY,
                    _ => f64::NEG_INFINITY,
                });
            }
            if map.contains_key("$self") {
                return Value::Obj(root.clone());
            }
            if map.contains_key("$null") {
                return Value::Null;
            }
            if let Some(behaviour) = map.get("$b").and_then(|v| v.as_str()) {
                if !map.contains_key("$o") {
                    return match this {
                        Some(obj) => bm(behaviour, obj),
                        None => b(behaviour),
                    };
                }
            }
            let obj = new_obj(vec![]);
            let mut pairs: Vec<(String, Value)> = Vec::new();
            if let Some(items) = map.get("$o").and_then(|v| v.as_array()) {
                for pair in items {
                    let Some(kv) = pair.as_array() else { continue };
                    if kv.len() != 2 {
                        continue;
                    }
                    let Some(k) = kv[0].as_str() else { continue };
                    pairs.push((k.to_string(), build_node(&kv[1], root, Some(&obj))));
                }
            }
            let kind = map.get("$k").and_then(|v| v.as_str()).unwrap_or("d");
            pairs.push((HIDDEN_KIND.to_string(), s(kind)));
            if let Some(behaviour) = map.get("$b").and_then(|v| v.as_str()) {
                pairs.push((HIDDEN_CALL.to_string(), bm(behaviour, &obj)));
            }
            if let Some(wild) = map.get("$wild").and_then(|v| v.as_str()) {
                pairs.push((HIDDEN_WILD.to_string(), s(wild)));
            }
            *obj.borrow_mut() = pairs;
            Value::Obj(obj)
        }
    }
}

fn from_dump() -> Option<ObjRef> {
    let parsed: serde_json::Value = serde_json::from_str(ENV_DUMP).ok()?;
    let root = new_obj(vec![]);
    let items = parsed.get("$o")?.as_array()?;
    let mut pairs: Vec<(String, Value)> = Vec::with_capacity(items.len() + 1);
    for pair in items {
        let kv = pair.as_array()?;
        if kv.len() != 2 {
            continue;
        }
        let k = kv[0].as_str()?;
        pairs.push((k.to_string(), build_node(&kv[1], &root, Some(&root))));
    }
    let kind = parsed.get("$k").and_then(|v| v.as_str()).unwrap_or("d");
    pairs.push((HIDDEN_KIND.to_string(), s(kind)));
    *root.borrow_mut() = pairs;
    Some(root)
}

/// 构建伪 window。
///
/// **参数当前不参与构建**：`env_dump.json` 来自 oracle 的 `profile={}` /
/// `device_id=x`，字节级一致要求环境常量（1920x1080、en-US、Chrome/152 UA…）
/// 与 oracle 完全相同。等环境改成按 profile 重新生成 dump（或补一套参数覆盖
/// 逻辑）之后再把 4 个参数接回来。
pub fn build_window(ua: &str, lang: &str, width: i64, height: i64) -> ObjRef {
    match from_dump() {
        Some(w) => w,
        None => {
            eprintln!("[env] src/env_dump.json 解析失败：退回手写环境（字段较少）");
            build_window_manual(ua, lang, width, height)
        }
    }
}

/// 环境里与指纹相关的字段（对应 Python `VM._env` 里从 `profile` 取的那些键）。
///
/// `Default::default()` == oracle 用的 `profile={}`，即 [`build_window`] 的默认环境。
#[derive(Clone, Debug)]
pub struct EnvProfile {
    pub browser_family: String,
    pub user_agent: String,
    pub language: String,
    pub languages: Vec<String>,
    pub platform: String,
    pub vendor: String,
    pub hardware_concurrency: i64,
    pub device_memory: i64,
    pub js_heap_size_limit: i64,
    pub max_touch_points: i64,
    pub screen_width: i64,
    pub screen_height: i64,
    pub device_pixel_ratio: f64,
    pub page_url: String,
    pub cookie: String,
    pub time_origin_ms: f64,
}

impl Default for EnvProfile {
    fn default() -> Self {
        EnvProfile {
            browser_family: "chrome".to_string(),
            user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                         (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
                .to_string(),
            language: "en-US".to_string(),
            languages: vec!["en-US".to_string(), "en".to_string()],
            platform: "Win32".to_string(),
            vendor: "Google Inc.".to_string(),
            hardware_concurrency: 8,
            device_memory: 8,
            js_heap_size_limit: 4_294_967_296,
            max_touch_points: 0,
            screen_width: 1920,
            screen_height: 1080,
            device_pixel_ratio: 1.0,
            page_url: String::new(),
            cookie: String::new(),
            time_origin_ms: 0.0,
        }
    }
}

/// 在 dump 环境上按指纹覆盖字段（镜像 Python `VM._env` 里 `p = self.profile` 的用法）。
///
/// 注意：这只改字段值，不会改变键序；`__oai_so_sx0/sy0/lx/ly` 也按 Python 的
/// `randint(a, b) -> a` 下界重新算。同时按 `browser_family` 门控 Chromium 专属 API，
/// 避免 Firefox / Safari 暴露 `performance.memory` / `navigator.deviceMemory` / `window.chrome`。
pub fn apply_profile(window: &ObjRef, p: &EnvProfile) {
    let family = p.browser_family.trim().to_ascii_lowercase();
    let is_firefox = family == "firefox";
    let is_safari = family == "safari";
    let is_chromium = !is_firefox && !is_safari;

    let (sw, sh) = (p.screen_width as f64, p.screen_height as f64);
    if let Some(Value::Obj(nav)) = obj_get(window, "navigator") {
        obj_set(&nav, "userAgent", s(&p.user_agent));
        obj_set(&nav, "language", s(&p.language));
        obj_set(
            &nav,
            "languages",
            Value::Arr(new_arr(p.languages.iter().map(|l| s(l)).collect())),
        );
        obj_set(&nav, "platform", s(&p.platform));
        let vendor = if is_firefox { "" } else { p.vendor.as_str() };
        obj_set(&nav, "vendor", s(vendor));
        obj_set(&nav, "hardwareConcurrency", Value::Num(p.hardware_concurrency as f64));
        obj_set(&nav, "maxTouchPoints", Value::Num(p.max_touch_points as f64));
        if is_chromium {
            obj_set(&nav, "deviceMemory", Value::Num(p.device_memory as f64));
            if obj_get(&nav, "gpu").is_none() {
                obj_set(&nav, "gpu", Value::Obj(new_obj(vec![(HIDDEN_KIND.to_string(), s("p"))])));
            }
        } else {
            obj_remove(&nav, "deviceMemory");
            obj_remove(&nav, "userAgentData");
            obj_remove(&nav, "gpu");
            obj_remove(&nav, "login");
        }
    }
    if let Some(Value::Obj(perf)) = obj_get(window, "performance") {
        if p.time_origin_ms > 0.0 {
            obj_set(&perf, "timeOrigin", Value::Num(p.time_origin_ms));
        }
        if is_chromium {
            if let Some(Value::Obj(mem)) = obj_get(&perf, "memory") {
                if p.js_heap_size_limit > 0 {
                    obj_set(&mem, "jsHeapSizeLimit", Value::Num(p.js_heap_size_limit as f64));
                }
            }
        } else {
            obj_remove(&perf, "memory");
        }
    }
    if let Some(Value::Obj(doc)) = obj_get(window, "document") {
        if !p.cookie.is_empty() {
            obj_set(&doc, "cookie", s(&p.cookie));
        }
        if !p.page_url.is_empty() {
            obj_set(&doc, "URL", s(&p.page_url));
            obj_set(&doc, "documentURI", s(&p.page_url));
        }
    }
    if !p.page_url.is_empty() {
        if let Some(Value::Obj(loc)) = obj_get(window, "location") {
            obj_set(&loc, "href", s(&p.page_url));
        }
    }
    if is_firefox {
        obj_remove(window, "chrome");
        obj_remove(window, "safari");
        let trigger = new_obj(vec![
            ("install".into(), b("noop")),
            (HIDDEN_KIND.to_string(), s("p")),
        ]);
        obj_set(window, "InstallTrigger", Value::Obj(trigger));
    } else if is_safari {
        obj_remove(window, "chrome");
        obj_remove(window, "InstallTrigger");
        let safari = new_obj(vec![
            ("pushNotification".into(), Value::Obj(new_obj(vec![]))),
            (HIDDEN_KIND.to_string(), s("p")),
        ]);
        obj_set(window, "safari", Value::Obj(safari));
    } else {
        obj_remove(window, "InstallTrigger");
        obj_remove(window, "safari");
    }
    if let Some(Value::Obj(screen)) = obj_get(window, "screen") {
        obj_set(&screen, "width", Value::Num(sw));
        obj_set(&screen, "height", Value::Num(sh));
        obj_set(&screen, "availWidth", Value::Num(sw));
        obj_set(&screen, "availHeight", Value::Num(sh));
    }
    obj_set(window, "innerWidth", Value::Num(sw));
    obj_set(window, "innerHeight", Value::Num(sh));
    obj_set(window, "outerWidth", Value::Num(sw));
    obj_set(window, "outerHeight", Value::Num(sh + 80.0));
    obj_set(window, "devicePixelRatio", Value::Num(p.device_pixel_ratio));
    for (key, value) in [
        ("__oai_so_sx0", (sw / 4.0).floor()),
        ("__oai_so_sy0", (sh / 4.0).floor()),
        ("__oai_so_lx", (sw / 3.0).floor()),
        ("__oai_so_ly", (sh / 3.0).floor()),
    ] {
        obj_set(window, key, Value::Num(value));
    }
}

/// 按指纹构造伪 window（`build_window` + [`apply_profile`]）。
pub fn build_window_for(profile: &EnvProfile) -> ObjRef {
    let w = build_window(&profile.user_agent, &profile.language, profile.screen_width, profile.screen_height);
    apply_profile(&w, profile);
    w
}

/// 手写兜底（仅在 env_dump.json 无法解析时使用；字段比 dump 少）。
pub fn build_window_manual(ua: &str, lang: &str, width: i64, height: i64) -> ObjRef {
    let root = new_obj(vec![]);
    let mut w: Vec<(String, Value)> = Vec::new();

    let document = new_obj(vec![
        ("cookie".into(), s("oai-did=x")),
        ("referrer".into(), s("https://auth.openai.com/")),
        ("title".into(), s("")),
        ("readyState".into(), s("complete")),
        ("visibilityState".into(), s("visible")),
        ("hidden".into(), Value::Bool(false)),
        ("URL".into(), s("https://auth.openai.com/")),
        ("documentURI".into(), s("https://auth.openai.com/")),
        ("scripts".into(), Value::Arr(new_arr(vec![Value::Obj(element("script"))]))),
        ("currentScript".into(), Value::Obj(element("script"))),
        ("documentElement".into(), Value::Obj(element("html"))),
        ("body".into(), Value::Obj(element("body"))),
        ("head".into(), Value::Obj(element("head"))),
        ("createElement".into(), bm("DocStub.createElement", &new_obj(vec![]))),
        ("getElementById".into(), b("DocStub.getElementById")),
        ("querySelector".into(), b("DocStub.querySelector")),
        ("querySelectorAll".into(), b("DocStub.querySelectorAll")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let navigator = new_obj(vec![
        ("userAgent".into(), s(ua)),
        ("appVersion".into(), s("5.0")),
        ("appName".into(), s("Netscape")),
        ("platform".into(), s("Win32")),
        ("vendor".into(), s("Google Inc.")),
        ("language".into(), s(lang)),
        ("languages".into(), Value::Arr(new_arr(vec![s(lang), s("en")]))),
        ("hardwareConcurrency".into(), Value::Num(8.0)),
        ("deviceMemory".into(), Value::Num(8.0)),
        ("maxTouchPoints".into(), Value::Num(0.0)),
        ("webdriver".into(), Value::Bool(false)),
        ("onLine".into(), Value::Bool(true)),
        ("cookieEnabled".into(), Value::Bool(true)),
        ("plugins".into(), Value::Arr(new_arr(vec![]))),
        ("mimeTypes".into(), Value::Arr(new_arr(vec![]))),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let screen = new_obj(vec![
        ("width".into(), Value::Num(width as f64)),
        ("height".into(), Value::Num(height as f64)),
        ("availWidth".into(), Value::Num(width as f64)),
        ("availHeight".into(), Value::Num(height as f64)),
        ("colorDepth".into(), Value::Num(24.0)),
        ("pixelDepth".into(), Value::Num(24.0)),
        ("orientation".into(), Value::Obj(new_obj(vec![
            ("type".into(), s("landscape-primary")),
            ("angle".into(), Value::Num(0.0)),
        ]))),
        (HIDDEN_KIND.to_string(), s("d")),
    ]);

    let performance = new_obj(vec![
        ("memory".into(), Value::Obj(new_obj(vec![(
            "jsHeapSizeLimit".into(),
            Value::Num(4_294_967_296.0),
        )]))),
        ("now".into(), b("performance.now")),
        ("timeOrigin".into(), Value::Num(1_699_999_999_984.0)),
        ("getEntriesByType".into(), b("PerfStub.getEntriesByType")),
        ("getEntriesByName".into(), b("PerfStub.getEntriesByName")),
        ("mark".into(), b("PerfStub.mark")),
        ("measure".into(), b("PerfStub.measure")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let math = new_obj(vec![
        ("random".into(), b("MathStub.random")),
        ("floor".into(), b("MathStub.floor")),
        ("ceil".into(), b("MathStub.ceil")),
        ("round".into(), b("MathStub.round")),
        ("abs".into(), b("MathStub.abs")),
        ("max".into(), b("MathStub.max")),
        ("min".into(), b("MathStub.min")),
        ("imul".into(), b("MathStub.imul")),
        ("pow".into(), b("MathStub.pow")),
        ("sqrt".into(), b("MathStub.sqrt")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let object = new_obj(vec![
        ("create".into(), b("ObjectStub.create")),
        ("keys".into(), b("ObjectStub.keys")),
        ("values".into(), b("ObjectStub.values")),
        ("assign".into(), b("ObjectStub.assign")),
        ("getPrototypeOf".into(), b("ObjectStub.getPrototypeOf")),
        ("defineProperty".into(), b("ObjectStub.defineProperty")),
        ("freeze".into(), b("ObjectStub.freeze")),
        ("entries".into(), b("ObjectStub.entries")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let reflect = new_obj(vec![
        ("get".into(), b("ReflectStub.get")),
        ("set".into(), b("ReflectStub.set")),
        ("has".into(), b("ReflectStub.has")),
        ("deleteProperty".into(), b("ReflectStub.deleteProperty")),
        ("ownKeys".into(), b("ReflectStub.ownKeys")),
        ("apply".into(), b("ReflectStub.apply")),
        ("construct".into(), b("ReflectStub.construct")),
        ("getPrototypeOf".into(), b("ReflectStub.getPrototypeOf")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    let crypto = new_obj(vec![
        ("getRandomValues".into(), b("CryptoStub.getRandomValues")),
        ("randomUUID".into(), b("CryptoStub.randomUUID")),
        (HIDDEN_KIND.to_string(), s("p")),
    ]);

    w.push(("Reflect".into(), Value::Obj(reflect)));
    w.push(("Math".into(), Value::Obj(math)));
    w.push(("Object".into(), Value::Obj(object)));
    w.push(("performance".into(), Value::Obj(performance)));
    w.push(("navigator".into(), Value::Obj(navigator)));
    w.push(("document".into(), Value::Obj(document)));
    w.push(("screen".into(), Value::Obj(screen)));
    w.push(("localStorage".into(), Value::Obj(storage())));
    w.push(("sessionStorage".into(), Value::Obj(storage())));
    w.push(("crypto".into(), Value::Obj(crypto)));
    w.push(("btoa".into(), b("b64e")));
    w.push(("atob".into(), b("b64d")));
    w.push(("innerWidth".into(), Value::Num(width as f64)));
    w.push(("innerHeight".into(), Value::Num(height as f64)));
    w.push(("outerWidth".into(), Value::Num(width as f64)));
    w.push(("outerHeight".into(), Value::Num(height as f64)));
    w.push(("devicePixelRatio".into(), Value::Num(1.0)));
    w.push(("window".into(), Value::Obj(root.clone())));
    w.push(("self".into(), Value::Obj(root.clone())));
    w.push(("top".into(), Value::Obj(root.clone())));
    w.push(("parent".into(), Value::Obj(root.clone())));
    w.push(("globalThis".into(), Value::Obj(root.clone())));
    w.push((
        "location".into(),
        Value::Obj(new_obj(vec![
            ("href".into(), s("https://auth.openai.com/")),
            ("origin".into(), s("https://auth.openai.com")),
            ("protocol".into(), s("https:")),
            ("host".into(), s("auth.openai.com")),
            ("hostname".into(), s("auth.openai.com")),
            ("pathname".into(), s("/")),
            ("search".into(), s("")),
            ("hash".into(), s("")),
        ])),
    ));
    w.push((HIDDEN_KIND.to_string(), s("d")));
    *root.borrow_mut() = w;
    root
}
