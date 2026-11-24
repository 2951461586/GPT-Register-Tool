//! JS 值模型：数组/对象用 Rc<RefCell<..>> 共享可变（push/pop/localStorage 需要）。
//!
//! 与 Python oracle（sentinel_vm.py）的 `jsstr` / `jsnum` / `jsadd` / `js_eq` /
//! `_js_json` 一一对应，浮点格式化按 ECMAScript `Number::toString` 规则
//! （见 [`num_to_string`]）。
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

pub type ObjRef = Rc<RefCell<Vec<(String, Value)>>>;
pub type ArrRef = Rc<RefCell<Vec<Value>>>;

/// 环境函数（window/document/Math/... 上的内建）：按行为名派发。
///
/// `this` 是 Python 侧“绑定方法”的接收者——索引时取到的方法必须记住它属于
/// 哪个对象（`element.appendChild` 要改 self.children）。
#[derive(Debug)]
pub struct EnvFn {
    pub b: Rc<str>,
    pub this: Option<Value>,
}

#[derive(Debug)]
pub enum Func {
    /// op 30 定义的闭包：参数槽 + 指令体 + 返回寄存器
    Closure {
        params: Vec<u64>,
        body: Vec<Vec<Value>>,
        ret: u64,
    },
    /// 绑定方法：this + 方法名（数组/字符串方法、fn.call/apply）
    Bound { this: Value, name: String },
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Native {
    Op0, Op1, Op2, Op3, Op4, Op5, Op6, Op7, Op8, Op11, Op12, Op13, Op14, Op15, Op17,
    Op18, Op19, Op20, Op21, Op22, Op23, Op24, Op25, Op26, Op27, Op28, Op29, Op30,
    Op33, Op34, Op35,
}

#[derive(Clone, Debug)]
pub enum Value {
    Undef,
    Null,
    Bool(bool),
    Num(f64),
    Str(Rc<str>),
    Arr(ArrRef),
    Obj(ObjRef),
    Func(Rc<Func>),
    Env(Rc<EnvFn>),
    Native(Native),
    /// op 12：整个寄存器表（Python `self.r`）。
    Regs,
}

/// 对象上的隐藏键（不出现在 `Object.keys` / JSON 里）。
pub const HIDDEN_CALL: &str = "$call";
pub const HIDDEN_WILD: &str = "$wild";
pub const HIDDEN_KIND: &str = "$k";
pub const HIDDEN_BEHAVIOUR: &str = "$b";

pub fn hidden(key: &str) -> bool {
    key.starts_with('$')
}

impl Value {
    pub fn is_undefined(&self) -> bool {
        matches!(self, Value::Undef)
    }

    /// Python `callable()`
    pub fn is_callable(&self) -> bool {
        matches!(self, Value::Func(_) | Value::Env(_) | Value::Native(_))
    }
}

// ---------------------------------------------------------------- 数字 → 字符串

/// ECMAScript `Number::toString`（= Python `_jsnum_str`）。
pub fn num_to_string(n: f64) -> String {
    if n.is_nan() {
        return "NaN".into();
    }
    if n.is_infinite() {
        return if n > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    if n == 0.0 {
        return "0".into();
    }
    let a = n.abs();
    if (1e-6..1e21).contains(&a) {
        // Rust 的 Display 给出最短往返十进制并展开指数，与 CPython repr 展开一致
        return format!("{}", n);
    }
    // 指数形式：mantissa e±exp（正指数带 '+'）
    let sci = format!("{:e}", n);
    let (mant, exp) = match sci.split_once('e') {
        Some((m, e)) => (m.to_string(), e.parse::<i32>().unwrap_or(0)),
        None => (sci, 0),
    };
    let mant = mant.strip_suffix(".0").unwrap_or(&mant).to_string();
    format!("{}e{}{}", mant, if exp >= 0 { '+' } else { '-' }, exp.abs())
}

// ------------------------------------------------------------------ jsstr

/// Python `jsstr(v)`
pub fn to_string(v: &Value) -> String {
    match v {
        Value::Undef => "undefined".into(),
        Value::Null => "null".into(),
        Value::Bool(b) => if *b { "true".into() } else { "false".into() },
        Value::Num(n) => num_to_string(*n),
        Value::Str(s) => s.to_string(),
        Value::Arr(a) => {
            let items: Vec<String> = a.borrow().iter().map(to_string).collect();
            items.join(",")
        }
        Value::Obj(_) | Value::Regs => "[object Object]".into(),
        Value::Func(_) | Value::Env(_) | Value::Native(_) => "function () { [native code] }".into(),
    }
}

// ------------------------------------------------------------------ jsnum

/// Python `jsnum(v)`
pub fn to_number(v: &Value) -> f64 {
    match v {
        Value::Num(n) => *n,
        Value::Bool(b) => if *b { 1.0 } else { 0.0 },
        Value::Null => 0.0,
        Value::Undef => f64::NAN,
        Value::Str(s) => {
            let t = s.trim();
            if t.is_empty() {
                return 0.0;
            }
            let lowered = t.to_ascii_lowercase();
            match lowered.as_str() {
                "inf" | "infinity" | "+inf" | "+infinity" => return f64::INFINITY,
                "-inf" | "-infinity" => return f64::NEG_INFINITY,
                _ => {}
            }
            t.parse::<f64>().unwrap_or(f64::NAN)
        }
        _ => f64::NAN,
    }
}

// ------------------------------------------------------------------ jsadd

/// JS 的 `+`（Python `jsadd`）：两边都是数字才相加，否则字符串拼接。
pub fn jsadd(a: &Value, b: &Value) -> Value {
    if let (Value::Num(x), Value::Num(y)) = (a, b) {
        return Value::Num(x + y);
    }
    Value::Str(Rc::from(format!("{}{}", to_string(a), to_string(b)).as_str()))
}

// --------------------------------------------------------------- truthy

/// Python 真值（`bool(v)`）。
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Undef => true,
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Num(n) => *n != 0.0 && !n.is_nan(),
        Value::Str(s) => !s.is_empty(),
        Value::Arr(a) => !a.borrow().is_empty(),
        Value::Obj(o) => !o.borrow().is_empty(),
        _ => true,
    }
}

// ------------------------------------------------------------------ js_eq

/// JS 全等 `===`（Python `js_eq`）：类型不同一律 false，对象比引用。
pub fn strict_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Undef, Value::Undef) | (Value::Null, Value::Null) => true,
        (Value::Bool(x), Value::Bool(y)) => x == y,
        (Value::Num(x), Value::Num(y)) => x == y,
        (Value::Str(x), Value::Str(y)) => x == y,
        (Value::Arr(x), Value::Arr(y)) => Rc::ptr_eq(x, y),
        (Value::Obj(x), Value::Obj(y)) => Rc::ptr_eq(x, y),
        (Value::Func(x), Value::Func(y)) => Rc::ptr_eq(x, y),
        (Value::Native(x), Value::Native(y)) => x == y,
        (Value::Env(x), Value::Env(y)) => Rc::ptr_eq(x, y),
        (Value::Regs, Value::Regs) => true,
        _ => false,
    }
}

/// Python `js_lt`
pub fn js_lt(a: &Value, b: &Value) -> bool {
    if let (Value::Str(x), Value::Str(y)) = (a, b) {
        return x < y;
    }
    let (x, y) = (to_number(a), to_number(b));
    if x.is_nan() || y.is_nan() {
        return false;
    }
    x < y
}

pub fn key_of(n: f64) -> u64 {
    n.to_bits()
}

// ------------------------------------------------------------ 对象工具

pub fn obj_get(o: &ObjRef, key: &str) -> Option<Value> {
    o.borrow().iter().find(|(k, _)| k == key).map(|(_, v)| v.clone())
}

pub fn obj_set(o: &ObjRef, key: &str, val: Value) {
    let mut b = o.borrow_mut();
    if let Some(slot) = b.iter_mut().find(|(k, _)| k == key) {
        slot.1 = val;
    } else {
        b.push((key.to_string(), val));
    }
}

pub fn obj_remove(o: &ObjRef, key: &str) {
    o.borrow_mut().retain(|(k, _)| k != key);
}

pub fn new_obj(pairs: Vec<(String, Value)>) -> ObjRef {
    Rc::new(RefCell::new(pairs))
}

pub fn new_arr(items: Vec<Value>) -> ArrRef {
    Rc::new(RefCell::new(items))
}

pub fn obj_kind(o: &ObjRef) -> Option<String> {
    obj_get(o, HIDDEN_KIND).map(|v| to_string(&v))
}

/// 可见键（跳过 `$` 隐藏键），保持插入顺序。
pub fn visible_keys(o: &ObjRef) -> Vec<String> {
    o.borrow()
        .iter()
        .filter(|(k, _)| !hidden(k))
        .map(|(k, _)| k.clone())
        .collect()
}

pub fn keys_of(v: &Value) -> Vec<Value> {
    match v {
        Value::Obj(o) => visible_keys(o)
            .into_iter()
            .map(|k| Value::Str(Rc::from(k.as_str())))
            .collect(),
        Value::Arr(a) => (0..a.borrow().len()).map(|i| Value::Num(i as f64)).collect(),
        _ => vec![],
    }
}

pub fn hash_map() -> HashMap<u64, Value> {
    HashMap::new()
}

// ------------------------------------------------------------ JSON 解析/序列化

/// JSON 解析后的 serde 值 → Value（对象保持顺序：serde_json 的 Map 默认是 BTreeMap，
/// 这里用 preserve_order feature 才不会乱序）
pub fn from_json(v: &serde_json::Value) -> Value {
    match v {
        serde_json::Value::Null => Value::Null,
        serde_json::Value::Bool(b) => Value::Bool(*b),
        serde_json::Value::Number(n) => Value::Num(n.as_f64().unwrap_or(f64::NAN)),
        serde_json::Value::String(s) => Value::Str(Rc::from(s.as_str())),
        serde_json::Value::Array(a) => Value::Arr(new_arr(a.iter().map(from_json).collect())),
        serde_json::Value::Object(o) => {
            let mut pairs = Vec::with_capacity(o.len());
            for (k, v) in o {
                pairs.push((k.clone(), from_json(v)));
            }
            Value::Obj(new_obj(pairs))
        }
    }
}

fn json_escape(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `JSON.stringify` 的单个值；`None` = 该值要被丢掉（函数/undefined）。
pub fn js_json_opt(v: &Value) -> Option<String> {
    match v {
        Value::Undef => None,
        Value::Null => Some("null".into()),
        Value::Bool(b) => Some(if *b { "true".into() } else { "false".into() }),
        Value::Num(n) => Some(if n.is_finite() { num_to_string(*n) } else { "null".into() }),
        Value::Str(s) => {
            let mut out = String::with_capacity(s.len() + 2);
            json_escape(s, &mut out);
            Some(out)
        }
        Value::Arr(a) => {
            let mut out = String::from("[");
            for (i, x) in a.borrow().iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&js_json_opt(x).unwrap_or_else(|| "null".into()));
            }
            out.push(']');
            Some(out)
        }
        Value::Obj(o) => {
            // Python 侧的非 dict 对象（stub 实例）序列化成 null
            if obj_kind(o).as_deref() == Some("p") {
                return Some("null".into());
            }
            let mut out = String::from("{");
            let mut first = true;
            for (k, val) in o.borrow().iter() {
                if hidden(k) {
                    continue;
                }
                let Some(text) = js_json_opt(val) else { continue };
                if !first {
                    out.push(',');
                }
                first = false;
                json_escape(k, &mut out);
                out.push(':');
                out.push_str(&text);
            }
            out.push('}');
            Some(out)
        }
        Value::Func(_) | Value::Env(_) | Value::Native(_) => None,
        Value::Regs => Some("null".into()),
    }
}

pub fn stringify(v: &Value) -> String {
    js_json_opt(v).unwrap_or_else(|| "null".into())
}

// ------------------------------------------------------------------ base64

/// Python `b64e`：非 latin-1 文本走 UTF-8，其余按字节（ord & 0xff）编码。
pub fn b64_encode(text: &str) -> String {
    use base64::Engine;
    let wide = text.chars().any(|c| c as u32 > 255);
    let bytes: Vec<u8> = if wide {
        text.as_bytes().to_vec()
    } else {
        text.chars().map(|c| (c as u32 & 0xff) as u8).collect()
    };
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

/// 兼容旧名。
pub fn b64_encode_latin1(text: &str) -> String {
    b64_encode(text)
}

/// Python `b64d`：base64 → latin-1 文本。
pub fn b64_decode(text: &str) -> String {
    use base64::Engine;
    let filtered: String = text
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '+' || *c == '/' || *c == '=')
        .collect();
    let mut body = filtered.trim_end_matches('=').to_string();
    let rem = body.len() % 4;
    if rem > 0 {
        body.extend(std::iter::repeat('=').take(4 - rem));
    }
    match base64::engine::general_purpose::STANDARD.decode(body.as_bytes()) {
        Ok(raw) => raw.iter().map(|b| *b as char).collect(),
        Err(_) => String::new(),
    }
}
