//! Sentinel 指令虚拟机（Rust 版）：寄存器表 + 36 个 handler + 执行循环。
//!
//! 语义逐条对齐 Python oracle `sentinel_vm.py`（包括错误文本——异常信息会进
//! token 载荷，所以必须一字不差）：
//!
//! * 指令 = `[opcode, arg...]`，`opcode` 本身是一个**寄存器号**，程序会先把
//!   handler 函数复制到别的寄存器里再调用（所以 handler 必须是可存储的值）。
//! * 未生效的寄存器读出来是 `undefined`；op13/op17 会把异常文本写回寄存器。
//! * `Vm::trace` 打开后按 `tools/py_writes.py` 的格式记录每一步与每次 set，
//!   两个实现逐行 diff 即可定位第一处语义分叉。
use crate::env::b;
use crate::value::*;
use std::collections::HashMap;
use std::rc::Rc;

#[derive(Debug, Clone)]
pub struct JsError(pub String);

pub type R<T> = Result<T, JsError>;

const OPS: [(u64, Native); 33] = [
    (0, Native::Op0),
    (1, Native::Op1),
    (2, Native::Op2),
    (3, Native::Op3),
    (4, Native::Op4),
    (5, Native::Op5),
    (6, Native::Op6),
    (7, Native::Op7),
    (8, Native::Op8),
    (11, Native::Op11),
    (12, Native::Op12),
    (13, Native::Op13),
    (14, Native::Op14),
    (15, Native::Op15),
    (17, Native::Op17),
    (18, Native::Op18),
    (19, Native::Op19),
    (20, Native::Op20),
    (21, Native::Op21),
    (22, Native::Op22),
    (23, Native::Op23),
    (24, Native::Op24),
    (25, Native::Op25),
    (26, Native::Op26),
    (27, Native::Op27),
    (28, Native::Op28),
    (29, Native::Op29),
    (30, Native::Op30),
    (33, Native::Op33),
    (34, Native::Op34),
    (35, Native::Op35),
    // 占位（Python 侧未注册 31/32）
    (u64::MAX, Native::Op25),
    (u64::MAX - 1, Native::Op25),
];

fn s(text: &str) -> Value {
    Value::Str(Rc::from(text))
}

fn not_callable(v: &Value) -> JsError {
    let kind = match v {
        Value::Undef => "Undef",
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Num(_) => "float",
        Value::Str(_) => "str",
        Value::Arr(_) => "list",
        Value::Obj(_) => "dict",
        Value::Regs => "dict",
        Value::Func(_) | Value::Env(_) | Value::Native(_) => "function",
    };
    JsError(format!("'{}' object is not callable", kind))
}

/// Python `type(x).__name__`（用于属性赋值失败的 AttributeError 文本）
fn py_type_name(v: &Value) -> &'static str {
    match v {
        Value::Undef => "Undef",
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Num(_) => "float",
        Value::Str(_) => "str",
        Value::Arr(_) => "list",
        Value::Obj(_) => "dict",
        Value::Regs => "dict",
        Value::Func(_) | Value::Env(_) | Value::Native(_) => "function",
    }
}

pub struct Vm {
    pub regs: HashMap<u64, Value>,
    pub out: Option<String>,
    pub err: Option<String>,
    pub done: bool,
    pub steps: usize,
    pub window: ObjRef,
    pub listeners: HashMap<String, Vec<Value>>,
    pub clock_now: f64,
    pub perf_now: f64,
    pub random: f64,
    pub trace: bool,
    pub trace_lines: Vec<String>,
}

impl Vm {
    pub fn new(window: ObjRef) -> Self {
        let mut vm = Vm {
            regs: HashMap::new(),
            out: None,
            err: None,
            done: false,
            steps: 0,
            window,
            listeners: HashMap::new(),
            clock_now: 1_700_000_000_000.0,
            perf_now: 15.5,
            random: 0.5,
            trace: false,
            trace_lines: Vec::new(),
        };
        vm.install();
        vm
    }

    /// 覆盖被 pin 死的“随机/时钟”来源（对应 `tools/pyoracle.py` 里的三个常量）。
    ///
    /// 默认值即 oracle 的 pin：`Date.now()=1700000000000`、`performance.now()=15.5`、
    /// `Math.random()=0.5`。要让两边同时换一组 pin 再来对比时用这个方法。
    pub fn set_pins(&mut self, clock_now_ms: f64, perf_now: f64, random: f64) {
        self.clock_now = clock_now_ms;
        self.perf_now = perf_now;
        self.random = random;
    }

    fn install(&mut self) {
        for (k, op) in OPS {
            if k != u64::MAX && k != u64::MAX - 1 {
                self.regs.insert(key_of(k as f64), Value::Native(op));
            }
        }
        self.regs.insert(key_of(9.0), Value::Null);
        self.regs.insert(key_of(16.0), Value::Null);
        let w = self.window.clone();
        self.regs.insert(key_of(10.0), Value::Obj(w));
    }

    pub fn get(&self, k: f64) -> Value {
        self.regs.get(&key_of(k)).cloned().unwrap_or(Value::Undef)
    }

    pub fn set(&mut self, k: f64, v: Value) {
        if self.trace {
            self.trace_lines
                .push(format!("{}\tset\t{}\t{}", self.steps, num_to_string(k), to_string(&v)));
        }
        self.regs.insert(key_of(k), v);
    }

    /// 把程序塞进寄存器 9（队列）并从那里开始跑。
    pub fn run_program(&mut self, prog: Vec<Vec<Value>>, key: &str) -> R<()> {
        self.set(16.0, s(key));
        let rows: Vec<Value> = prog
            .iter()
            .map(|ins| Value::Arr(new_arr(ins.clone())))
            .collect();
        self.set(9.0, Value::Arr(new_arr(rows)));
        self.run_queue()
    }

    /// 只装载队列（不改 key、不立即执行）；供 trace 工具复用。
    pub fn load_queue(&mut self, prog: Vec<Vec<Value>>) {
        let rows: Vec<Value> = prog
            .iter()
            .map(|ins| Value::Arr(new_arr(ins.clone())))
            .collect();
        self.set(9.0, Value::Arr(new_arr(rows)));
    }

    /// 公开的执行入口。
    pub fn run(&mut self) -> R<()> {
        self.run_queue()
    }

    // ------------------------------------------------------------ 执行循环

    fn run_queue(&mut self) -> R<()> {
        loop {
            if self.done {
                break;
            }
            let q = self.get(9.0);
            let Value::Arr(arr) = q else { break };
            let next = {
                let mut b = arr.borrow_mut();
                if b.is_empty() {
                    None
                } else {
                    Some(b.remove(0))
                }
            };
            let Some(instr) = next else { break };
            self.steps += 1;
            if self.steps > 500_000 {
                return Err(JsError("step budget".into()));
            }
            let ins = match &instr {
                Value::Arr(a) => a.borrow().clone(),
                _ => continue,
            };
            let op = ins.first().cloned().unwrap_or(Value::Undef);
            if self.trace {
                let args: Vec<String> = ins.get(1..).unwrap_or(&[]).iter().map(to_string).collect();
                self.trace_lines
                    .push(format!("{}\top={}\tins={}", self.steps, to_string(&op), args.join(",")));
            }
            let f = self.get(to_number(&op));
            if !f.is_callable() {
                return Err(JsError(format!(
                    "op {} not callable at step {} ({:?})",
                    to_string(&op),
                    self.steps,
                    ins.get(1..).unwrap_or(&[])
                        .iter()
                        .map(to_string)
                        .collect::<Vec<_>>()
                )));
            }
            self.call(&f, &ins[1..])?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ 调用

    pub fn call(&mut self, f: &Value, args: &[Value]) -> R<Value> {
        match f {
            Value::Native(id) => self.call_op(*id, args),
            Value::Env(ef) => {
                let ef = ef.clone();
                self.call_env(&ef, args)
            }
            Value::Func(func) => {
                let func = func.clone();
                match &*func {
                    Func::Closure { params, body, ret } => {
                        let saved_queue = self.get(9.0);
                        for (i, p) in params.iter().enumerate() {
                            let v = args.get(i).cloned().unwrap_or(Value::Undef);
                            self.set(f64::from_bits(*p), v);
                        }
                        let rows: Vec<Value> = body
                            .iter()
                            .map(|ins| Value::Arr(new_arr(ins.clone())))
                            .collect();
                        self.set(9.0, Value::Arr(new_arr(rows)));
                        self.run_queue()?;
                        self.set(9.0, saved_queue);
                        Ok(self.get(f64::from_bits(*ret)))
                    }
                    Func::Bound { this, name } => {
                        let this = this.clone();
                        let name = name.clone();
                        self.call_member(&this, &name, args)
                    }
                }
            }
            Value::Obj(o) => match obj_get(o, HIDDEN_CALL) {
                Some(h) => self.call(&h, args),
                None => Err(not_callable(f)),
            },
            _ => Err(not_callable(f)),
        }
    }

    // ------------------------------------------------------------ handler

    fn call_op(&mut self, id: Native, args: &[Value]) -> R<Value> {
        match id {
            // 嵌套程序：t 是内联的 base64 串（不是寄存器）
            Native::Op0 => {
                if let Some(t) = args.first() {
                    let key = to_string(&self.get(16.0));
                    let raw = b64_decode(&to_string(t));
                    let plain = xor_str(&raw, &key);
                    match serde_json::from_str::<serde_json::Value>(&plain) {
                        Ok(j) => {
                            let prog = from_json(&j);
                            let saved = self.get(9.0);
                            let Value::Arr(arr) = prog else {
                                return Ok(Value::Undef);
                            };
                            self.set(9.0, Value::Arr(new_arr(arr.borrow().clone())));
                            self.run_queue()?;
                            self.set(9.0, saved);
                        }
                        Err(e) => return Err(JsError(format!("JSONDecodeError: {}", e))),
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op1 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let a = to_string(&self.get(*n));
                    let b = to_string(&self.get(*e));
                    let out = xor_str(&a, &b);
                    self.set(*n, s(&out));
                }
                Ok(Value::Undef)
            }
            Native::Op2 => {
                if let Some(Value::Num(n)) = args.first() {
                    let v = args.get(1).cloned().unwrap_or(Value::Undef);
                    self.set(*n, v);
                }
                Ok(Value::Undef)
            }
            Native::Op3 => {
                let v = args.first().cloned().unwrap_or(Value::Undef);
                self.out = Some(b64_encode(&to_string(&v)));
                self.done = true;
                Ok(Value::Undef)
            }
            Native::Op4 => {
                let v = args.first().cloned().unwrap_or(Value::Undef);
                self.err = Some(b64_encode(&to_string(&v)));
                self.done = true;
                Ok(Value::Undef)
            }
            Native::Op5 => {
                // op_add：列表 push，否则 jsadd
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let cur = self.get(*n);
                    let val = self.get(*e);
                    match cur {
                        Value::Arr(a) => a.borrow_mut().push(val),
                        other => {
                            let sum = jsadd(&other, &val);
                            self.set(*n, sum);
                        }
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op6 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e)), Some(Value::Num(r))) =
                    (args.first(), args.get(1), args.get(2))
                {
                    let obj = self.get(*e);
                    let key = self.get(*r);
                    let v = self.index(&obj, &key)?;
                    self.set(*n, v);
                }
                Ok(Value::Undef)
            }
            Native::Op7 => {
                if let Some(Value::Num(n)) = args.first() {
                    let callee = self.get(*n);
                    let call_args: Vec<Value> = args[1..].iter().map(|a| self.get(to_number(a))).collect();
                    self.call(&callee, &call_args)?;
                }
                Ok(Value::Undef)
            }
            Native::Op8 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let v = self.get(*e);
                    self.set(*n, v);
                }
                Ok(Value::Undef)
            }
            Native::Op11 => {
                // op_script_src：在 document.scripts 里找第一个匹配正则的 src
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let pattern = to_string(&self.get(*e));
                    let mut found = Value::Null;
                    let doc = obj_get(&self.window, "document").unwrap_or(Value::Undef);
                    if let Value::Obj(doc) = doc {
                        let scripts = obj_get(&doc, "scripts").unwrap_or(Value::Undef);
                        if let Value::Arr(list) = scripts {
                            for sc in list.borrow().iter() {
                                let src = match sc {
                                    Value::Obj(o) => {
                                        to_string(&obj_get(o, "src").unwrap_or(Value::Undef))
                                    }
                                    _ => String::new(),
                                };
                                if let Some(m) = mini_regex::search(&pattern, &src) {
                                    found = s(&m);
                                    break;
                                }
                            }
                        }
                    }
                    self.set(*n, found);
                }
                Ok(Value::Undef)
            }
            Native::Op12 => {
                if let Some(Value::Num(n)) = args.first() {
                    self.set(*n, Value::Regs);
                }
                Ok(Value::Undef)
            }
            Native::Op13 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let callee = self.get(*e);
                    let raw: Vec<Value> = args[2..].to_vec();
                    if let Err(err) = self.call(&callee, &raw) {
                        self.set(*n, s(&err.0));
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op14 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let text = to_string(&self.get(*e));
                    match serde_json::from_str::<serde_json::Value>(&text) {
                        Ok(j) => {
                            let v = from_json(&j);
                            self.set(*n, v);
                        }
                        Err(err) => return Err(JsError(format!("JSONDecodeError: {}", err))),
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op15 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let v = self.get(*e);
                    if v.is_undefined() || v.is_callable() {
                        self.set(*n, Value::Undef);
                    } else {
                        let text = stringify(&v);
                        self.set(*n, s(&text));
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op17 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let callee = self.get(*e);
                    let call_args: Vec<Value> =
                        args[2..].iter().map(|a| self.get(to_number(a))).collect();
                    match self.call(&callee, &call_args) {
                        Ok(v) => self.set(*n, v),
                        Err(err) => self.set(*n, s(&err.0)),
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op18 => {
                if let Some(Value::Num(n)) = args.first() {
                    let dec = b64_decode(&to_string(&self.get(*n)));
                    self.set(*n, s(&dec));
                }
                Ok(Value::Undef)
            }
            Native::Op19 => {
                if let Some(Value::Num(n)) = args.first() {
                    let enc = b64_encode(&to_string(&self.get(*n)));
                    self.set(*n, s(&enc));
                }
                Ok(Value::Undef)
            }
            Native::Op20 => {
                if args.len() >= 3 {
                    let (n, e, r) = (&args[0], &args[1], &args[2]);
                    let a = self.get(to_number(n));
                    let b = self.get(to_number(e));
                    if strict_eq(&a, &b) {
                        let callee = self.get(to_number(r));
                        let raw: Vec<Value> = args[3..].to_vec();
                        self.call(&callee, &raw)?;
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op21 => {
                if args.len() >= 4 {
                    let n = to_number(&args[0]);
                    let e = to_number(&args[1]);
                    let r = to_number(&args[2]);
                    let diff = (to_number(&self.get(n)) - to_number(&self.get(e))).abs();
                    if diff > to_number(&self.get(r)) {
                        let callee = self.get(to_number(&args[3]));
                        let raw: Vec<Value> = args[4..].to_vec();
                        self.call(&callee, &raw)?;
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op22 => {
                // op_schedule：临时把队列换成 e，跑完恢复
                if args.len() >= 2 {
                    let prog = match &args[1] {
                        Value::Num(reg) => self.get(*reg),
                        other => other.clone(),
                    };
                    let old = self.get(9.0);
                    let rows = match prog {
                        Value::Arr(a) => a.borrow().clone(),
                        Value::Undef | Value::Null => vec![],
                        other => return Err(JsError(format!("'{}' object is not iterable", py_type_name(&other)))),
                    };
                    self.set(9.0, Value::Arr(new_arr(rows)));
                    let res = self.run_queue();
                    self.set(9.0, old);
                    res?;
                }
                Ok(Value::Undef)
            }
            Native::Op23 => {
                if args.len() >= 2 {
                    let check = self.get(to_number(&args[0]));
                    if !matches!(check, Value::Undef) {
                        let callee = self.get(to_number(&args[1]));
                        let raw: Vec<Value> = args[2..].to_vec();
                        self.call(&callee, &raw)?;
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op24 => {
                if args.len() >= 3 {
                    let obj = self.get(to_number(&args[1]));
                    let key = self.get(to_number(&args[2]));
                    let v = self.index(&obj, &key)?;
                    self.set(to_number(&args[0]), v);
                }
                Ok(Value::Undef)
            }
            Native::Op25 | Native::Op26 | Native::Op28 => Ok(Value::Null),
            Native::Op27 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let cur = self.get(*n);
                    let val = self.get(*e);
                    match cur {
                        Value::Arr(a) => {
                            let mut b = a.borrow_mut();
                            if let Some(pos) = b.iter().position(|x| strict_eq(x, &val)) {
                                b.remove(pos);
                            }
                        }
                        other => {
                            let out = to_number(&other) - to_number(&val);
                            self.set(*n, Value::Num(out));
                        }
                    }
                }
                Ok(Value::Undef)
            }
            Native::Op29 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e)), Some(Value::Num(r))) =
                    (args.first(), args.get(1), args.get(2))
                {
                    let a = self.get(*e);
                    let b = self.get(*r);
                    self.set(*n, Value::Bool(js_lt(&a, &b)));
                }
                Ok(Value::Undef)
            }
            Native::Op30 => {
                // op_define：args = (t, n, e, r)；r 是数组时 e 是参数表，否则 e 是指令体
                if args.len() >= 4 {
                    let t = to_number(&args[0]);
                    let n = to_number(&args[1]);
                    let e = &args[2];
                    let r = &args[3];
                    let is_arr = matches!(r, Value::Arr(_));
                    let params: Vec<u64> = if is_arr {
                        match e {
                            Value::Arr(a) => a.borrow().iter().map(|v| key_of(to_number(v))).collect(),
                            _ => vec![],
                        }
                    } else {
                        vec![]
                    };
                    let body_value = if is_arr { r.clone() } else { e.clone() };
                    let body: Vec<Vec<Value>> = match body_value {
                        Value::Arr(a) => a
                            .borrow()
                            .iter()
                            .map(|row| match row {
                                Value::Arr(items) => items.borrow().clone(),
                                _ => vec![],
                            })
                            .collect(),
                        Value::Null | Value::Undef => vec![],
                        other => {
                            return Err(JsError(format!(
                                "'{}' object is not iterable",
                                py_type_name(&other)
                            )))
                        }
                    };
                    let f = Value::Func(Rc::new(Func::Closure {
                        params,
                        body,
                        ret: n.to_bits(),
                    }));
                    self.set(t, f);
                }
                Ok(Value::Undef)
            }
            Native::Op33 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e)), Some(Value::Num(r))) =
                    (args.first(), args.get(1), args.get(2))
                {
                    let v = to_number(&self.get(*e)) * to_number(&self.get(*r));
                    self.set(*n, Value::Num(v));
                }
                Ok(Value::Undef)
            }
            Native::Op34 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e))) = (args.first(), args.get(1)) {
                    let v = self.get(*e);
                    self.set(*n, v);
                }
                Ok(Value::Undef)
            }
            Native::Op35 => {
                if let (Some(Value::Num(n)), Some(Value::Num(e)), Some(Value::Num(r))) =
                    (args.first(), args.get(1), args.get(2))
                {
                    let divisor = to_number(&self.get(*r));
                    let v = if divisor == 0.0 {
                        0.0
                    } else {
                        to_number(&self.get(*e)) / divisor
                    };
                    self.set(*n, Value::Num(v));
                }
                Ok(Value::Undef)
            }
        }
    }

    // ------------------------------------------------------------ 属性读取

    pub fn index(&mut self, obj: &Value, key: &Value) -> R<Value> {
        let name = to_string(key);
        match obj {
            Value::Obj(o) => {
                if let Some(v) = obj_get(o, &name) {
                    return Ok(v);
                }
                // localStorage.length 是 Python 的 property，随 _d 变化
                if name == "length" {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        return Ok(Value::Num(d.borrow().len() as f64));
                    }
                }
                if let Some(wild) = obj_get(o, HIDDEN_WILD) {
                    return Ok(Value::Env(Rc::new(EnvFn {
                        b: Rc::from(to_string(&wild).as_str()),
                        this: Some(Value::Obj(o.clone())),
                    })));
                }
                Ok(Value::Undef)
            }
            Value::Arr(a) => {
                let len = a.borrow().len();
                match name.as_str() {
                    "length" => Ok(Value::Num(len as f64)),
                    "pop" | "push" | "shift" | "unshift" | "join" | "slice" | "indexOf"
                    | "includes" | "reverse" | "concat" | "map" | "filter" | "splice" => {
                        Ok(Value::Func(Rc::new(Func::Bound {
                            this: obj.clone(),
                            name,
                        })))
                    }
                    _ => {
                        let idx = to_number(key);
                        if idx.is_nan() {
                            return Ok(Value::Undef);
                        }
                        let mut i = idx.trunc() as i64;
                        if i < 0 {
                            i += len as i64;
                        }
                        if i < 0 || i >= len as i64 {
                            Ok(Value::Undef)
                        } else {
                            Ok(a.borrow()[i as usize].clone())
                        }
                    }
                }
            }
            Value::Str(text) => {
                let len = text.chars().count();
                match name.as_str() {
                    "length" => Ok(Value::Num(len as f64)),
                    "indexOf" | "charAt" | "slice" | "toLowerCase" | "toUpperCase"
                    | "includes" | "split" | "trim" => Ok(Value::Func(Rc::new(Func::Bound {
                        this: obj.clone(),
                        name,
                    }))),
                    _ => Ok(Value::Undef),
                }
            }
            Value::Undef | Value::Null => {
                let kind = if matches!(obj, Value::Undef) { "undefined" } else { "null" };
                Err(JsError(format!(
                    "TypeError: Cannot read properties of {} (reading '{}')",
                    kind, name
                )))
            }
            Value::Func(_) | Value::Env(_) | Value::Native(_) => match name.as_str() {
                "bind" => Ok(obj.clone()),
                "call" | "apply" => Ok(Value::Func(Rc::new(Func::Bound {
                    this: obj.clone(),
                    name,
                }))),
                _ => Ok(Value::Undef),
            },
            Value::Regs => {
                let k = to_number(key);
                Ok(self.get(k))
            }
            _ => Ok(Value::Undef),
        }
    }

    /// 数组/字符串/函数方法（Python `index` 里返回的那些 lambda）
    fn call_member(&mut self, this: &Value, name: &str, args: &[Value]) -> R<Value> {
        match this {
            Value::Arr(arr) => {
                match name {
                    "pop" => {
                        let mut b = arr.borrow_mut();
                        Ok(if b.is_empty() { Value::Null } else { b.pop().unwrap() })
                    }
                    "push" => {
                        let mut b = arr.borrow_mut();
                        b.extend(args.iter().cloned());
                        Ok(Value::Num(b.len() as f64))
                    }
                    "shift" => {
                        let mut b = arr.borrow_mut();
                        Ok(if b.is_empty() { Value::Null } else { b.remove(0) })
                    }
                    "unshift" => {
                        let mut b = arr.borrow_mut();
                        for (i, v) in args.iter().enumerate() {
                            b.insert(i, v.clone());
                        }
                        Ok(Value::Num(b.len() as f64))
                    }
                    "join" => {
                        let sep = args.first().map(to_string).unwrap_or_else(|| ",".into());
                        let items: Vec<String> = arr.borrow().iter().map(to_string).collect();
                        Ok(s(&items.join(&sep)))
                    }
                    "slice" => {
                        let n = arr.borrow().len() as i64;
                        let start = args.first().map(|v| to_number(v) as i64).unwrap_or(0);
                        let start = if start < 0 { (n + start).max(0) } else { start.min(n) };
                        let end = match args.get(1) {
                            Some(v) if !matches!(v, Value::Null) => {
                                let e = to_number(v) as i64;
                                if e < 0 {
                                    (n + e).max(0)
                                } else {
                                    e.min(n)
                                }
                            }
                            _ => n,
                        };
                        let end = end.max(start);
                        let out = arr.borrow()[start as usize..end as usize].to_vec();
                        Ok(Value::Arr(new_arr(out)))
                    }
                    "indexOf" => {
                        let needle = args.first().cloned().unwrap_or(Value::Undef);
                        let idx = arr
                            .borrow()
                            .iter()
                            .position(|x| strict_eq(x, &needle))
                            .map(|i| i as f64)
                            .unwrap_or(-1.0);
                        Ok(Value::Num(idx))
                    }
                    "includes" => {
                        let needle = args.first().cloned().unwrap_or(Value::Undef);
                        Ok(Value::Bool(arr.borrow().iter().any(|x| strict_eq(x, &needle))))
                    }
                    "reverse" => {
                        let mut items = arr.borrow().clone();
                        items.reverse();
                        Ok(Value::Arr(new_arr(items)))
                    }
                    "concat" => {
                        let mut out = arr.borrow().clone();
                        for a in args {
                            match a {
                                Value::Arr(other) => out.extend(other.borrow().iter().cloned()),
                                other => out.push(other.clone()),
                            }
                        }
                        Ok(Value::Arr(new_arr(out)))
                    }
                    "map" => {
                        let f = args.first().cloned().unwrap_or(Value::Undef);
                        let snapshot = arr.borrow().clone();
                        let mut out = Vec::with_capacity(snapshot.len());
                        for (i, x) in snapshot.iter().enumerate() {
                            let r = self.call(
                                &f,
                                &[x.clone(), Value::Num(i as f64), Value::Arr(arr.clone())],
                            )?;
                            out.push(r);
                        }
                        Ok(Value::Arr(new_arr(out)))
                    }
                    "filter" => {
                        let f = args.first().cloned().unwrap_or(Value::Undef);
                        let snapshot = arr.borrow().clone();
                        let mut out = Vec::new();
                        for (i, x) in snapshot.iter().enumerate() {
                            let r = self.call(
                                &f,
                                &[x.clone(), Value::Num(i as f64), Value::Arr(arr.clone())],
                            )?;
                            if truthy(&r) {
                                out.push(x.clone());
                            }
                        }
                        Ok(Value::Arr(new_arr(out)))
                    }
                    "splice" => {
                        let len = arr.borrow().len() as i64;
                        let start = args.first().map(|v| to_number(v) as i64).unwrap_or(0);
                        let start = if start < 0 {
                            (len + start).max(0)
                        } else {
                            start.min(len)
                        };
                        let delete_count = match args.get(1) {
                            Some(v) => (to_number(v) as i64).max(0),
                            None => len - start,
                        };
                        let end = (start + delete_count).min(len);
                        let items: Vec<Value> = args.iter().skip(2).cloned().collect();
                        let mut b = arr.borrow_mut();
                        let removed: Vec<Value> = b.splice(start as usize..end as usize, items).collect();
                        Ok(Value::Arr(new_arr(removed)))
                    }
                    _ => Ok(Value::Undef),
                }
            }
            Value::Str(text) => {
                let chars: Vec<char> = text.chars().collect();
                match name {
                    "indexOf" => {
                        let needle = match args.first() {
                            Some(v) => to_string(v),
                            None => return Ok(Value::Num(-1.0)),
                        };
                        if needle.is_empty() {
                            return Ok(Value::Num(0.0));
                        }
                        let hay: Vec<char> = chars.clone();
                        let nd: Vec<char> = needle.chars().collect();
                        let pos = hay
                            .windows(nd.len())
                            .position(|w| w == nd.as_slice())
                            .map(|i| i as f64)
                            .unwrap_or(-1.0);
                        Ok(Value::Num(pos))
                    }
                    "charAt" => {
                        let i = args.first().map(|v| to_number(v) as i64).unwrap_or(0);
                        if i < 0 || i as usize >= chars.len() {
                            Ok(s(""))
                        } else {
                            Ok(s(&chars[i as usize].to_string()))
                        }
                    }
                    "slice" => {
                        let n = chars.len() as i64;
                        let start = args.first().map(|v| to_number(v) as i64).unwrap_or(0);
                        let start = if start < 0 { (n + start).max(0) } else { start.min(n) };
                        let end = match args.get(1) {
                            Some(v) if !matches!(v, Value::Null) => {
                                let e = to_number(v) as i64;
                                if e < 0 {
                                    (n + e).max(0)
                                } else {
                                    e.min(n)
                                }
                            }
                            _ => n,
                        };
                        let end = end.max(start);
                        let part: String = chars[start as usize..end as usize].iter().collect();
                        Ok(s(&part))
                    }
                    "toLowerCase" => Ok(s(&text.to_lowercase())),
                    "toUpperCase" => Ok(s(&text.to_uppercase())),
                    "includes" => {
                        let needle = args.first().map(to_string).unwrap_or_default();
                        Ok(Value::Bool(text.contains(&needle)))
                    }
                    "split" => match args.first() {
                        Some(Value::Null) | None => Ok(Value::Arr(new_arr(vec![s(text)]))),
                        Some(sep) => {
                            let sep = to_string(sep);
                            if sep.is_empty() {
                                return Err(JsError(
                                    "empty separator".to_string(),
                                ));
                            }
                            let parts: Vec<Value> = text.split(sep.as_str()).map(s).collect();
                            Ok(Value::Arr(new_arr(parts)))
                        }
                    },
                    "trim" => Ok(s(text.trim())),
                    _ => Ok(Value::Undef),
                }
            }
            Value::Func(_) | Value::Env(_) | Value::Native(_) => match name {
                "call" => self.call(this, &args[1.min(args.len())..]),
                "apply" => {
                    let list = match args.get(1) {
                        Some(Value::Arr(a)) => a.borrow().clone(),
                        _ => vec![],
                    };
                    self.call(this, &list)
                }
                _ => Ok(Value::Undef),
            },
            _ => Ok(Value::Undef),
        }
    }

    // ------------------------------------------------------------ 环境函数

    fn call_env(&mut self, ef: &EnvFn, args: &[Value]) -> R<Value> {
        let name: &str = &ef.b;
        let this = ef.this.clone();
        let arg = |i: usize| args.get(i).cloned().unwrap_or(Value::Undef);
        match name {
            // ---- 时间/随机（与 tools/pyoracle.py 的 pin 一致）
            "performance.now" => Ok(Value::Num(self.perf_now)),
            "PerfStub.getEntriesByType" | "PerfStub.getEntriesByName" => {
                Ok(Value::Arr(new_arr(vec![])))
            }
            "PerfStub.mark" | "PerfStub.measure" => Ok(Value::Null),
            "Date.now" => Ok(Value::Num(self.clock_now)),
            "Date.parse" | "Date.UTC" => Ok(Value::Num(0.0)),
            "MathStub.random" => Ok(Value::Num(self.random)),

            // ---- Math
            "MathStub.floor" => Ok(Value::Num(to_number(&arg(0)).floor())),
            "MathStub.ceil" => Ok(Value::Num(to_number(&arg(0)).ceil())),
            "MathStub.round" => Ok(Value::Num((to_number(&arg(0)) + 0.5).floor())),
            "MathStub.abs" => Ok(Value::Num(to_number(&arg(0)).abs())),
            "MathStub.max" => {
                let mut best = f64::NEG_INFINITY;
                for a in args {
                    best = best.max(to_number(a));
                }
                Ok(Value::Num(best))
            }
            "MathStub.min" => {
                let mut best = f64::INFINITY;
                for a in args {
                    best = best.min(to_number(a));
                }
                Ok(Value::Num(best))
            }
            "MathStub.imul" => {
                let a = to_number(&arg(0)) as i64 as i32;
                let b = to_number(&arg(1)) as i64 as i32;
                Ok(Value::Num(a.wrapping_mul(b) as u32 as f64))
            }
            "MathStub.pow" => Ok(Value::Num(
                to_number(&arg(0)).powf(to_number(&arg(1))),
            )),
            "MathStub.sqrt" => Ok(Value::Num(to_number(&arg(0)).max(0.0).sqrt())),
            "MathStub.cbrt" => {
                let x = to_number(&arg(0));
                Ok(Value::Num(x.abs().cbrt().copysign(x)))
            }
            "MathStub.hypot" => {
                let mut sum = 0.0;
                for a in args {
                    let n = to_number(a);
                    sum += n * n;
                }
                Ok(Value::Num(sum.sqrt()))
            }
            "MathStub.trunc" => Ok(Value::Num(to_number(&arg(0)).trunc())),
            "MathStub.sign" => {
                let x = to_number(&arg(0));
                Ok(Value::Num(if x > 0.0 {
                    1.0
                } else if x < 0.0 {
                    -1.0
                } else {
                    0.0
                }))
            }
            "MathStub.log" => Ok(Value::Num(to_number(&arg(0)).max(1e-300).ln())),
            "MathStub.exp" => Ok(Value::Num(to_number(&arg(0)).min(700.0).exp())),
            "MathStub.expm1" => Ok(Value::Num(to_number(&arg(0)).exp_m1())),
            "MathStub.isSafeInteger" => {
                let v = arg(0);
                Ok(Value::Bool(matches!(v, Value::Num(n) if n.fract() == 0.0 && n.abs() <= 9007199254740991.0)))
            }

            // ---- Object
            "ObjectStub.create" => Ok(Value::Obj(new_obj(vec![
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "ObjectStub.keys" => Ok(Value::Arr(new_arr(self.object_keys(&arg(0))))),
            "ObjectStub.values" => Ok(Value::Arr(new_arr(self.object_values(&arg(0))))),
            "ObjectStub.entries" => {
                let entries: Vec<Value> = match &arg(0) {
                    Value::Obj(o) => visible_keys(o)
                        .into_iter()
                        .map(|k| {
                            let v = obj_get(o, &k).unwrap_or(Value::Undef);
                            Value::Arr(new_arr(vec![Value::Str(Rc::from(k.as_str())), v]))
                        })
                        .collect(),
                    _ => vec![],
                };
                Ok(Value::Arr(new_arr(entries)))
            }
            "ObjectStub.assign" => {
                let target = arg(0);
                if let Value::Obj(t) = &target {
                    for src in args.iter().skip(1) {
                        if let Value::Obj(so) = src {
                            let pairs = so.borrow().clone();
                            for (k, v) in pairs {
                                if !hidden(&k) {
                                    obj_set(t, &k, v);
                                }
                            }
                        }
                    }
                }
                Ok(target)
            }
            "ObjectStub.getPrototypeOf" => Ok(Value::Null),
            "ObjectStub.defineProperty" => {
                let o = arg(0);
                if let Value::Obj(obj) = &o {
                    let key = to_string(&arg(1));
                    let desc = arg(2);
                    let val = match desc {
                        Value::Obj(d) => obj_get(&d, "value").unwrap_or(Value::Null),
                        other => other,
                    };
                    obj_set(obj, &key, val);
                }
                Ok(o)
            }
            "ObjectStub.freeze" => Ok(arg(0)),

            // ---- Reflect
            "ReflectStub.get" => {
                let o = arg(0);
                let k = arg(1);
                self.index(&o, &k)
            }
            "ReflectStub.set" => {
                let o = arg(0);
                let key = to_string(&arg(1));
                let val = arg(2);
                match &o {
                    Value::Obj(obj) => {
                        obj_set(obj, &key, val);
                        Ok(Value::Bool(true))
                    }
                    _ => Ok(Value::Bool(false)),
                }
            }
            "ReflectStub.has" => {
                let key = to_string(&arg(1));
                Ok(Value::Bool(match &arg(0) {
                    Value::Obj(o) => obj_get(o, &key).is_some(),
                    _ => false,
                }))
            }
            "ReflectStub.deleteProperty" => {
                let key = to_string(&arg(1));
                if let Value::Obj(o) = &arg(0) {
                    o.borrow_mut().retain(|(k, _)| k != &key);
                }
                Ok(Value::Bool(true))
            }
            "ReflectStub.ownKeys" => Ok(Value::Arr(new_arr(keys_of(&arg(0))))),
            "ReflectStub.apply" => {
                let f = arg(0);
                let list = match arg(2) {
                    Value::Arr(a) => a.borrow().clone(),
                    _ => vec![],
                };
                self.call(&f, &list)
            }
            "ReflectStub.construct" => {
                let f = arg(0);
                let list = match arg(1) {
                    Value::Arr(a) => a.borrow().clone(),
                    _ => vec![],
                };
                self.call(&f, &list)
            }
            "ReflectStub.getPrototypeOf" => Ok(Value::Null),

            // ---- Storage
            "StorageStub.setItem" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        let k = to_string(&arg(0));
                        let v = to_string(&arg(1));
                        obj_set(&d, &k, s(&v));
                    }
                }
                Ok(Value::Null)
            }
            "StorageStub.getItem" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        let k = to_string(&arg(0));
                        return Ok(obj_get(&d, &k).unwrap_or(Value::Null));
                    }
                }
                Ok(Value::Null)
            }
            "StorageStub.removeItem" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        let k = to_string(&arg(0));
                        d.borrow_mut().retain(|(kk, _)| kk != &k);
                    }
                }
                Ok(Value::Null)
            }
            "StorageStub.clear" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        d.borrow_mut().clear();
                    }
                }
                Ok(Value::Null)
            }
            "StorageStub.key" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Obj(d)) = obj_get(o, "_d") {
                        let i = to_number(&arg(0)) as i64;
                        let keys: Vec<String> =
                            d.borrow().iter().map(|(k, _)| k.clone()).collect();
                        if i >= 0 && (i as usize) < keys.len() {
                            return Ok(s(&keys[i as usize]));
                        }
                    }
                }
                Ok(Value::Null)
            }

            // ---- crypto
            "CryptoStub.getRandomValues" => {
                let arr = arg(0);
                if let Value::Arr(a) = &arr {
                    let len = a.borrow().len();
                    let mut b = a.borrow_mut();
                    for i in 0..len {
                        b[i] = Value::Num(0.0);
                    }
                }
                Ok(arr)
            }
            "CryptoStub.randomUUID" => Ok(s("00000000-0000-4000-8000-000000000000")),

            // ---- 全局函数
            "b64e" => Ok(s(&b64_encode(&to_string(&arg(0))))),
            "b64d" => Ok(s(&b64_decode(&to_string(&arg(0))))),
            "String" => Ok(s(&to_string(&arg(0)))),
            "Boolean" => Ok(Value::Bool(truthy(&arg(0)))),
            "Number" => Ok(Value::Num(to_number(&arg(0)))),
            "parseInt" => {
                let raw = to_string(&arg(0));
                let radix = match args.get(1) {
                    Some(v) => to_number(v) as u32,
                    None => 10,
                };
                match i64::from_str_radix(raw.trim(), radix.max(2)) {
                    Ok(v) => Ok(Value::Num(v as f64)),
                    Err(_) => Err(JsError(format!(
                        "invalid literal for int() with base {}: {}",
                        radix, raw
                    ))),
                }
            }
            "parseFloat" => {
                let raw = to_string(&arg(0));
                match raw.trim().parse::<f64>() {
                    Ok(v) => Ok(Value::Num(v)),
                    Err(_) => Err(JsError(format!(
                        "could not convert string to float: {}",
                        raw
                    ))),
                }
            }
            "isNaN" => Ok(Value::Bool(matches!(arg(0), Value::Num(n) if n.is_nan()))),
            "isFinite" => Ok(Value::Bool(matches!(arg(0), Value::Num(n) if n.is_finite()))),
            "quote" => Ok(s(&uri_quote(&to_string(&arg(0))))),
            "unquote" => Ok(s(&uri_unquote(&to_string(&arg(0))))),
            "unescape" => Ok(arg(0)),
            "setTimeout" => Ok(Value::Num(0.0)),
            "setInterval" => Ok(Value::Num(0.0)),
            "clearTimeout" | "clearInterval" | "cancelAnimationFrame" | "cancelIdleCallback" => {
                Ok(Value::Null)
            }
            "queueMicrotask" | "cancelIdleCallback2" => Ok(Value::Null),
            "requestAnimationFrame" => Ok(Value::Num(0.0)),
            "requestIdleCallback" => Ok(Value::Num(0.0)),
            "getComputedStyle" => Ok(Value::Obj(new_obj(vec![
                ("getPropertyValue".to_string(), s("")),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "matchMedia" => {
                let q = to_string(&arg(0));
                Ok(Value::Obj(new_obj(vec![
                    ("media".to_string(), s(&q)),
                    ("matches".to_string(), Value::Bool(false)),
                    ("onchange".to_string(), Value::Null),
                    ("addListener".to_string(), s("")),
                    ("removeListener".to_string(), s("")),
                    ("addEventListener".to_string(), s("")),
                    ("removeEventListener".to_string(), s("")),
                    (HIDDEN_KIND.to_string(), s("d")),
                ])))
            }
            "Event" => Ok(Value::Obj(new_obj(vec![
                ("type".to_string(), arg(0)),
                (HIDDEN_KIND.to_string(), s("p")),
            ]))),
            "MessageChannel" => Ok(Value::Obj(new_obj(vec![
                ("port1".to_string(), Value::Obj(new_obj(vec![
                    ("postMessage".to_string(), s("")),
                    (HIDDEN_KIND.to_string(), s("d")),
                ]))),
                ("port2".to_string(), Value::Obj(new_obj(vec![
                    ("postMessage".to_string(), s("")),
                    (HIDDEN_KIND.to_string(), s("d")),
                ]))),
                (HIDDEN_KIND.to_string(), s("p")),
            ]))),
            "chrome.csi" | "chrome.loadTimes" => Ok(Value::Obj(new_obj(vec![
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "CSS.supports" => Ok(Value::Bool(true)),
            "indexedDB.open" => Ok(Value::Obj(new_obj(vec![
                ("onerror".to_string(), Value::Null),
                ("onsuccess".to_string(), Value::Null),
                ("result".to_string(), Value::Obj(new_obj(vec![
                    (HIDDEN_KIND.to_string(), s("d")),
                ]))),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "fetch" | "postMessage" | "location.assign" | "location.replace" | "location.reload"
            | "history.back" | "history.forward" | "history.go" | "history.pushState"
            | "history.replaceState" | "DocStub.addEventListener" | "DocStub.removeEventListener"
            | "ElementStub.setAttribute" | "ElementStub.addEventListener"
            | "ElementStub.removeEventListener" => Ok(Value::Null),

            // ---- 事件
            "EventTargetStub.addEventListener" => {
                let t = to_string(&arg(0));
                let f = arg(1);
                self.listeners.entry(t).or_default().push(f);
                Ok(Value::Null)
            }
            "EventTargetStub.removeEventListener" => {
                let t = to_string(&arg(0));
                let f = arg(1);
                if let Some(list) = self.listeners.get_mut(&t) {
                    if let Some(pos) = list.iter().position(|x| strict_eq(x, &f)) {
                        list.remove(pos);
                    }
                }
                Ok(Value::Null)
            }
            "EventTargetStub.dispatchEvent" => {
                let ev = arg(0);
                let ev_type = match &ev {
                    Value::Obj(o) => to_string(&obj_get(o, "type").unwrap_or(Value::Undef)),
                    _ => String::new(),
                };
                let handlers = self.listeners.get(&ev_type).cloned().unwrap_or_default();
                for h in handlers {
                    let _ = self.call(&h, std::slice::from_ref(&ev));
                }
                Ok(Value::Null)
            }

            // ---- document
            "DocStub.createElement" | "DocStub.createElementNS" => {
                let tag = if name == "DocStub.createElementNS" {
                    to_string(&arg(1))
                } else {
                    to_string(&arg(0))
                };
                Ok(Value::Obj(crate::env::element(&tag)))
            }
            "DocStub.createDocumentFragment" => {
                Ok(Value::Obj(crate::env::element("fragment")))
            }
            "DocStub.createTextNode" => Ok(Value::Obj(new_obj(vec![
                ("nodeType".to_string(), Value::Num(3.0)),
                ("textContent".to_string(), arg(0)),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "DocStub.querySelector" | "DocStub.getElementById" => Ok(Value::Null),
            "DocStub.querySelectorAll" | "DocStub.getElementsByClassName" => {
                Ok(Value::Arr(new_arr(vec![])))
            }
            "DocStub.getElementsByTagName" => {
                if to_string(&arg(0)) == "script" {
                    if let Some(Value::Obj(doc)) = obj_get(&self.window, "document") {
                        if let Some(scripts) = obj_get(&doc, "scripts") {
                            return Ok(scripts);
                        }
                    }
                }
                Ok(Value::Arr(new_arr(vec![])))
            }

            // ---- 元素
            "ElementStub.appendChild" => {
                let node = arg(0);
                if let Some(Value::Obj(el)) = &this {
                    if let Some(Value::Arr(children)) = obj_get(el, "children") {
                        children.borrow_mut().push(node.clone());
                    }
                    match &node {
                        Value::Obj(o) => obj_set(o, "parentNode", Value::Obj(el.clone())),
                        other => {
                            return Err(JsError(format!(
                                "'{}' object has no attribute 'parentNode'",
                                py_type_name(other)
                            )))
                        }
                    }
                }
                Ok(node)
            }
            "ElementStub.removeChild" => {
                let node = arg(0);
                if let Some(Value::Obj(el)) = &this {
                    if let Some(Value::Arr(children)) = obj_get(el, "children") {
                        let mut b = children.borrow_mut();
                        if let Some(pos) = b.iter().position(|x| strict_eq(x, &node)) {
                            b.remove(pos);
                        }
                    }
                }
                Ok(node)
            }
            "ElementStub.insertBefore" => {
                let node = arg(0);
                if let Some(Value::Obj(el)) = &this {
                    if let Some(Value::Arr(children)) = obj_get(el, "children") {
                        children.borrow_mut().push(node.clone());
                    }
                }
                Ok(node)
            }
            "ElementStub.getAttribute" | "document.currentScript.getAttribute" => Ok(Value::Null),
            "ElementStub.getContext" => {
                let kind = to_string(&arg(0));
                match kind.as_str() {
                    "2d" => Ok(Value::Obj(crate::env::ctx2d())),
                    "webgl" | "experimental-webgl" | "webgl2" => Ok(Value::Obj(crate::env::webgl(
                        &this.clone().unwrap_or(Value::Undef),
                    ))),
                    _ => Ok(Value::Null),
                }
            }
            "ElementStub.toDataURL" => Ok(s("data:image/png;base64,")),
            "ElementStub.toBlob" => {
                if let Some(cb) = args.first() {
                    if cb.is_callable() {
                        self.call(cb, &[Value::Null])?;
                    }
                }
                Ok(Value::Null)
            }
            "ElementStub.getBoundingClientRect" => Ok(Value::Obj(new_obj(vec![
                ("x".to_string(), Value::Num(0.0)),
                ("y".to_string(), Value::Num(0.0)),
                ("width".to_string(), Value::Num(0.0)),
                ("height".to_string(), Value::Num(0.0)),
                ("top".to_string(), Value::Num(0.0)),
                ("left".to_string(), Value::Num(0.0)),
                ("right".to_string(), Value::Num(0.0)),
                ("bottom".to_string(), Value::Num(0.0)),
                ("toJSON".to_string(), b("noop")),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "ElementStub.getClientRects" => {
                let rect = self.call_env(ef, &[])?;
                Ok(Value::Arr(new_arr(vec![rect])))
            }
            "ElementStub.querySelector" => Ok(Value::Null),
            "ElementStub.querySelectorAll" => Ok(Value::Arr(new_arr(vec![]))),

            // ---- canvas
            "Ctx2D.measureText" => Ok(Value::Obj(new_obj(vec![
                ("width".to_string(), Value::Num(0.0)),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "Ctx2D.getImageData" | "Ctx2D.createImageData" => Ok(Value::Obj(new_obj(vec![
                ("data".to_string(), Value::Arr(new_arr(vec![]))),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "Ctx2D.gradient" => Ok(Value::Obj(new_obj(vec![
                ("addColorStop".to_string(), b("noop")),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "WebGL.getExtension" => {
                if to_string(&arg(0)) == "WEBGL_debug_renderer_info" {
                    Ok(Value::Obj(new_obj(vec![
                        ("UNMASKED_VENDOR_WEBGL".to_string(), Value::Num(37445.0)),
                        ("UNMASKED_RENDERER_WEBGL".to_string(), Value::Num(37446.0)),
                        (HIDDEN_KIND.to_string(), s("d")),
                    ])))
                } else {
                    Ok(Value::Null)
                }
            }
            "WebGL.getParameter" => {
                let p = to_number(&arg(0)) as i64;
                let v = match p {
                    37445 | 7936 => "Google Inc. (Intel)",
                    37446 | 7937 => {
                        "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"
                    }
                    _ => "",
                };
                if v.is_empty() {
                    Ok(Value::Num(0.0))
                } else {
                    Ok(s(v))
                }
            }
            "WebGL.getSupportedExtensions" => Ok(Value::Arr(new_arr(vec![s(
                "WEBGL_debug_renderer_info",
            )]))),

            // ---- Set / Map
            "SetStub" | "MapStub" | "WeakSetStub" | "WeakMapStub" => {
                Ok(Value::Obj(crate::env::set_like()))
            }
            "SetStub.add" => {
                let v = arg(0);
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        let mut b = list.borrow_mut();
                        if !b.iter().any(|x| strict_eq(x, &v)) {
                            b.push(v);
                        }
                    }
                    return Ok(Value::Obj(o.clone()));
                }
                Ok(Value::Null)
            }
            "SetStub.has" => {
                let v = arg(0);
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        return Ok(Value::Bool(list.borrow().iter().any(|x| strict_eq(x, &v))));
                    }
                }
                Ok(Value::Bool(false))
            }
            "SetStub.delete" => {
                let v = arg(0);
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        let mut b = list.borrow_mut();
                        if let Some(pos) = b.iter().position(|x| strict_eq(x, &v)) {
                            b.remove(pos);
                            return Ok(Value::Bool(true));
                        }
                    }
                }
                Ok(Value::Bool(false))
            }
            "SetStub.clear" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        list.borrow_mut().clear();
                    }
                }
                Ok(Value::Null)
            }
            "SetStub.values" | "SetStub.keys" => {
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        return Ok(Value::Arr(new_arr(list.borrow().clone())));
                    }
                }
                Ok(Value::Arr(new_arr(vec![])))
            }
            "SetStub.forEach" => {
                let f = arg(0);
                if let Some(Value::Obj(o)) = &this {
                    if let Some(Value::Arr(list)) = obj_get(o, "_s") {
                        let items = list.borrow().clone();
                        for v in items {
                            self.call(&f, &[v.clone(), v, Value::Obj(o.clone())])?;
                        }
                    }
                }
                Ok(Value::Null)
            }

            // ---- 杂项 stub
            "ArrayStub.isArray" => Ok(Value::Bool(matches!(arg(0), Value::Arr(_)))),
            "ArrayStub.from_" => {
                let out = match arg(0) {
                    Value::Arr(a) => a.borrow().clone(),
                    Value::Str(t) => t.chars().map(|c| s(&c.to_string())).collect(),
                    Value::Obj(o) => visible_keys(&o)
                        .into_iter()
                        .map(|k| Value::Str(Rc::from(k.as_str())))
                        .collect(),
                    _ => vec![],
                };
                Ok(Value::Arr(new_arr(out)))
            }
            "ArrayStub.of" => Ok(Value::Arr(new_arr(args.to_vec()))),
            "NumberStub.isInteger" => {
                Ok(Value::Bool(matches!(arg(0), Value::Num(n) if n.fract() == 0.0)))
            }
            "NumberStub.isFinite" => {
                Ok(Value::Bool(matches!(arg(0), Value::Num(n) if n.is_finite())))
            }
            "NumberStub.isNaN" => Ok(Value::Bool(matches!(arg(0), Value::Num(n) if n.is_nan()))),
            "NumberStub.isSafeInteger" => Ok(Value::Bool(
                matches!(arg(0), Value::Num(n) if n.fract() == 0.0 && n.abs() <= 9007199254740991.0),
            )),
            "NumberStub.parseInt" => self.call_env(&EnvFn { b: Rc::from("parseInt"), this: None }, args),
            "NumberStub.parseFloat" => {
                self.call_env(&EnvFn { b: Rc::from("parseFloat"), this: None }, args)
            }
            "UserAgentDataStub.toJSON" => Ok(Value::Obj(new_obj(vec![
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "UserAgentDataStub.getHighEntropyValues" => Ok(Value::Obj(new_obj(vec![
                ("architecture".to_string(), s("x86")),
                ("bitness".to_string(), s("64")),
                ("brands".to_string(), Value::Arr(new_arr(vec![]))),
                ("fullVersionList".to_string(), Value::Arr(new_arr(vec![]))),
                ("model".to_string(), s("")),
                ("platform".to_string(), s("Windows")),
                ("platformVersion".to_string(), s("15.0.0")),
                ("uaFullVersion".to_string(), s("152.0.7977.83")),
                ("wow64".to_string(), Value::Bool(false)),
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            "JSON.dumps" => Ok(s(&stringify(&arg(0)))),
            "JSON.loads" => {
                let text = to_string(&arg(0));
                match serde_json::from_str::<serde_json::Value>(&text) {
                    Ok(j) => Ok(from_json(&j)),
                    Err(e) => Err(JsError(format!("JSONDecodeError: {}", e))),
                }
            }
            "TextEncoderStub" => Ok(Value::Obj(new_obj(vec![
                ("encode".to_string(), b("TextEncoderStub.encode")),
                (HIDDEN_KIND.to_string(), s("p")),
            ]))),
            "TextDecoderStub" => Ok(Value::Obj(new_obj(vec![
                ("decode".to_string(), b("TextDecoderStub.decode")),
                (HIDDEN_KIND.to_string(), s("p")),
            ]))),
            "TextEncoderStub.encode" => {
                let text = to_string(&arg(0));
                let bytes: Vec<Value> = text.bytes().map(|b| Value::Num(b as f64)).collect();
                Ok(Value::Arr(new_arr(bytes)))
            }
            "TextDecoderStub.decode" => {
                let bytes: Vec<u8> = match arg(0) {
                    Value::Arr(a) => a.borrow().iter().map(|v| to_number(v) as u8).collect(),
                    _ => vec![],
                };
                Ok(s(&String::from_utf8_lossy(&bytes)))
            }
            "URLStub" => Ok(Value::Obj(new_obj(url_stub(&to_string(&arg(0)))))),
            "URLSearchParamsStub" => Ok(Value::Obj(new_obj(vec![
                ("_items".to_string(), Value::Arr(new_arr(vec![]))),
                ("get".to_string(), b("URLSearchParamsStub.get")),
                ("keys".to_string(), b("URLSearchParamsStub.keys")),
                ("toString".to_string(), b("URLSearchParamsStub.toString")),
                (HIDDEN_KIND.to_string(), s("p")),
            ]))),
            "URLSearchParamsStub.get" => Ok(Value::Null),
            "URLSearchParamsStub.keys" => Ok(Value::Arr(new_arr(vec![]))),
            "URLSearchParamsStub.toString" => Ok(s("")),

            "noop" | "wild_none" => Ok(Value::Null),
            "wild_obj" => Ok(Value::Obj(new_obj(vec![
                (HIDDEN_KIND.to_string(), s("d")),
            ]))),
            _ => Ok(Value::Null),
        }
    }

    fn object_keys(&self, v: &Value) -> Vec<Value> {
        match v {
            Value::Obj(o) => {
                // Python: StorageStub 有专门的固定键表
                if obj_get(o, "_d").is_some() {
                    return ["length", "clear", "getItem", "setItem", "removeItem", "key"]
                        .iter()
                        .map(|k| s(k))
                        .collect();
                }
                let class_keys: Vec<String> = match obj_get(o, "$c") {
                    Some(Value::Arr(a)) => a.borrow().iter().map(to_string).collect(),
                    _ => vec![],
                };
                visible_keys(o)
                    .into_iter()
                    .filter(|k| !class_keys.contains(k))
                    .map(|k| Value::Str(Rc::from(k.as_str())))
                    .collect()
            }
            Value::Arr(a) => (0..a.borrow().len()).map(|i| Value::Num(i as f64)).collect(),
            _ => vec![],
        }
    }

    fn object_values(&self, v: &Value) -> Vec<Value> {
        match v {
            Value::Obj(o) => visible_keys(o)
                .into_iter()
                .map(|k| obj_get(o, &k).unwrap_or(Value::Undef))
                .collect(),
            _ => vec![],
        }
    }
}

fn url_stub(href: &str) -> Vec<(String, Value)> {
    let (scheme, rest) = href.split_once("://").unwrap_or(("https", href));
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (hostname, _port) = match authority.rsplit_once(':') {
        Some((h, _p)) => (h, ""),
        None => (authority, ""),
    };
    let (pathname, _query) = match path.split_once('?') {
        Some((p, q)) => (p, q),
        None => (path, ""),
    };
    vec![
        ("href".to_string(), s(href)),
        ("origin".to_string(), s(&format!("{}://{}", scheme, authority))),
        ("protocol".to_string(), s(&format!("{}:", scheme))),
        ("host".to_string(), s(authority)),
        ("hostname".to_string(), s(hostname)),
        ("pathname".to_string(), s(if pathname.is_empty() { "/" } else { pathname })),
        ("search".to_string(), s("")),
        ("hash".to_string(), s("")),
        ("searchParams".to_string(), Value::Obj(new_obj(vec![
            ("_items".to_string(), Value::Arr(new_arr(vec![]))),
            ("get".to_string(), b("URLSearchParamsStub.get")),
            ("keys".to_string(), b("URLSearchParamsStub.keys")),
            ("toString".to_string(), b("URLSearchParamsStub.toString")),
            (HIDDEN_KIND.to_string(), s("p")),
        ]))),
        (HIDDEN_KIND.to_string(), s("p")),
    ]
}

// ------------------------------------------------------------ 小工具

fn xor_str(a: &str, b: &str) -> String {
    if b.is_empty() {
        return a.to_string();
    }
    let bb: Vec<char> = b.chars().collect();
    a.chars()
        .enumerate()
        .map(|(i, c)| char::from_u32((c as u32) ^ (bb[i % bb.len()] as u32)).unwrap_or('?'))
        .collect()
}

fn uri_quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        let c = byte as char;
        if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '~' | '/') {
            out.push(c);
        } else {
            out.push_str(&format!("%{:02X}", byte));
        }
    }
    out
}

fn uri_unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            if let Ok(v) = u8::from_str_radix(hex, 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    match String::from_utf8(out) {
        Ok(text) => text,
        Err(e) => e.into_bytes().iter().map(|b| *b as char).collect(),
    }
}

// ------------------------------------------------------------ 迷你正则

/// `op 11` 需要一个 `re.search`。只实现 SDK 用到的那一小撮语法：
/// 字面量、`.`、`[...]`（含 `^` 取反与范围）、`\\.` `\\d` `\\w` `\\s` 等转义、
/// `* + ?` 量词、`^ $` 锚点、`(...)` `(?:...)` 分组、`(?=...)` 前瞻、`|` 或。
/// 出现不支持的构造时退化成“原样字面量查找”。
pub mod mini_regex {
    #[derive(Clone, Debug)]
    enum Node {
        Char(char),
        Any,
        Class { neg: bool, items: Vec<(char, char)> },
        Group(Vec<Vec<Node>>),
        Lookahead(Vec<Vec<Node>>),
        AnchorStart,
        AnchorEnd,
        Repeat { node: Box<Node>, min: usize, max: usize },
    }

    pub fn search(pattern: &str, text: &str) -> Option<String> {
        let chars: Vec<char> = pattern.chars().collect();
        let mut pos = 0usize;
        let alts = match parse_alts(&chars, &mut pos) {
            Some(a) if pos >= chars.len() => a,
            _ => {
                // 退化成字面量查找
                return if text.contains(pattern) { Some(pattern.to_string()) } else { None };
            }
        };
        let hay: Vec<char> = text.chars().collect();
        for start in 0..=hay.len() {
            for alt in &alts {
                let mut ends = Vec::new();
                match_seq(alt, &hay, start, &mut ends);
                if let Some(end) = ends.into_iter().max() {
                    return Some(hay[start..end].iter().collect());
                }
            }
        }
        None
    }

    fn parse_alts(chars: &[char], pos: &mut usize) -> Option<Vec<Vec<Node>>> {
        let mut alts: Vec<Vec<Node>> = Vec::new();
        let mut cur: Vec<Node> = Vec::new();
        while *pos < chars.len() {
            let c = chars[*pos];
            match c {
                '|' => {
                    *pos += 1;
                    alts.push(std::mem::take(&mut cur));
                    continue;
                }
                ')' => break,
                '(' => {
                    *pos += 1;
                    let mut lookahead = false;
                    if *pos + 1 < chars.len() && chars[*pos] == '?' {
                        match chars[*pos + 1] {
                            ':' => {
                                *pos += 2;
                            }
                            '=' => {
                                *pos += 2;
                                lookahead = true;
                            }
                            '!' => return None,
                            _ => return None,
                        }
                    }
                    let inner = parse_alts(chars, pos)?;
                    if *pos >= chars.len() || chars[*pos] != ')' {
                        return None;
                    }
                    *pos += 1;
                    let node = if lookahead {
                        Node::Lookahead(inner)
                    } else {
                        Node::Group(inner)
                    };
                    cur.push(node);
                    continue;
                }
                '*' | '+' | '?' => {
                    let (min, max) = match c {
                        '*' => (0, usize::MAX),
                        '+' => (1, usize::MAX),
                        _ => (0, 1),
                    };
                    *pos += 1;
                    let prev = cur.pop()?;
                    if matches!(prev, Node::AnchorStart | Node::AnchorEnd) {
                        return None;
                    }
                    cur.push(Node::Repeat { node: Box::new(prev), min, max });
                    continue;
                }
                '^' => {
                    *pos += 1;
                    cur.push(Node::AnchorStart);
                    continue;
                }
                '$' => {
                    *pos += 1;
                    cur.push(Node::AnchorEnd);
                    continue;
                }
                '[' => {
                    *pos += 1;
                    let mut neg = false;
                    if *pos < chars.len() && chars[*pos] == '^' {
                        neg = true;
                        *pos += 1;
                    }
                    let mut items: Vec<(char, char)> = Vec::new();
                    if *pos < chars.len() && chars[*pos] == ']' {
                        items.push((']', ']'));
                        *pos += 1;
                    }
                    while *pos < chars.len() && chars[*pos] != ']' {
                        let mut lo = chars[*pos];
                        if lo == '\\' {
                            *pos += 1;
                            if *pos >= chars.len() {
                                return None;
                            }
                            lo = escaped(chars[*pos]);
                        }
                        *pos += 1;
                        if *pos + 1 < chars.len() && chars[*pos] == '-' && chars[*pos + 1] != ']' {
                            let mut hi = chars[*pos + 1];
                            if hi == '\\' {
                                *pos += 1;
                                if *pos + 1 >= chars.len() {
                                    return None;
                                }
                                hi = escaped(chars[*pos + 1]);
                            }
                            *pos += 2;
                            items.push((lo, hi));
                        } else {
                            items.push((lo, lo));
                        }
                    }
                    if *pos >= chars.len() {
                        return None;
                    }
                    *pos += 1;
                    cur.push(Node::Class { neg, items });
                    continue;
                }
                '\\' => {
                    *pos += 1;
                    if *pos >= chars.len() {
                        return None;
                    }
                    let c2 = chars[*pos];
                    *pos += 1;
                    match c2 {
                        'd' => cur.push(Node::Class { neg: false, items: vec![('0', '9')] }),
                        'D' => cur.push(Node::Class { neg: true, items: vec![('0', '9')] }),
                        'w' => cur.push(Node::Class {
                            neg: false,
                            items: vec![('a', 'z'), ('A', 'Z'), ('0', '9'), ('_', '_')],
                        }),
                        'W' => cur.push(Node::Class {
                            neg: true,
                            items: vec![('a', 'z'), ('A', 'Z'), ('0', '9'), ('_', '_')],
                        }),
                        's' => cur.push(Node::Class {
                            neg: false,
                            items: vec![(' ', ' '), ('\t', '\t'), ('\n', '\n'), ('\r', '\r')],
                        }),
                        'S' => cur.push(Node::Class {
                            neg: true,
                            items: vec![(' ', ' '), ('\t', '\t'), ('\n', '\n'), ('\r', '\r')],
                        }),
                        'b' | 'B' => return None,
                        other => cur.push(Node::Char(escaped(other))),
                    }
                    continue;
                }
                '.' => {
                    *pos += 1;
                    cur.push(Node::Any);
                    continue;
                }
                _ => {
                    *pos += 1;
                    cur.push(Node::Char(c));
                }
            }
        }
        alts.push(cur);
        Some(alts)
    }

    fn escaped(c: char) -> char {
        match c {
            'n' => '\n',
            't' => '\t',
            'r' => '\r',
            other => other,
        }
    }

    fn match_seq(nodes: &[Node], text: &[char], pos: usize, out: &mut Vec<usize>) {
        if nodes.is_empty() {
            out.push(pos);
            return;
        }
        let head = &nodes[0];
        let rest = &nodes[1..];
        let mut nexts: Vec<usize> = Vec::new();
        match head {
            Node::Char(c) => {
                if pos < text.len() && text[pos] == *c {
                    nexts.push(pos + 1);
                }
            }
            Node::Any => {
                if pos < text.len() && text[pos] != '\n' {
                    nexts.push(pos + 1);
                }
            }
            Node::Class { neg, items } => {
                if pos < text.len() {
                    let hit = items.iter().any(|(lo, hi)| text[pos] >= *lo && text[pos] <= *hi);
                    if hit != *neg {
                        nexts.push(pos + 1);
                    }
                }
            }
            Node::AnchorStart => {
                if pos == 0 {
                    nexts.push(pos);
                }
            }
            Node::AnchorEnd => {
                if pos == text.len() {
                    nexts.push(pos);
                }
            }
            Node::Lookahead(alts) => {
                let mut ends = Vec::new();
                for alt in alts {
                    match_seq(alt, text, pos, &mut ends);
                }
                if !ends.is_empty() {
                    nexts.push(pos);
                }
            }
            Node::Group(alts) => {
                for alt in alts {
                    match_seq(alt, text, pos, &mut nexts);
                }
            }
            Node::Repeat { node, min, max } => {
                // 贪心：尽量多匹配
                let mut levels: Vec<Vec<usize>> = vec![vec![pos]];
                let cap = if *max == usize::MAX { text.len() + 1 } else { *max };
                for _ in 0..cap {
                    let last = levels.last().cloned().unwrap_or_default();
                    let mut nxt = Vec::new();
                    for p in last {
                        match_seq(std::slice::from_ref(node), text, p, &mut nxt);
                    }
                    nxt.retain(|p| !levels.last().map(|l| l.contains(p)).unwrap_or(false));
                    if nxt.is_empty() {
                        break;
                    }
                    levels.push(nxt);
                }
                for (count, level) in levels.iter().enumerate().rev() {
                    if count >= *min && count <= cap {
                        nexts.extend(level.iter().copied());
                    }
                    // 允许更少的重复次数（回溯）
                    if count <= *min {
                        break;
                    }
                }
                nexts.sort_unstable();
                nexts.dedup();
            }
        }
        for p in nexts {
            match_seq(rest, text, p, out);
        }
    }
}

// ------------------------------------------------------------ tests

#[cfg(test)]
mod tests {
    use super::mini_regex::search;
    use crate::value::{num_to_string, strict_eq, to_number, to_string, Value};

    #[test]
    fn regex_literal() {
        assert_eq!(search("abc", "xabcx"), Some("abc".to_string()));
        assert_eq!(search("abc", "xyz"), None);
    }

    #[test]
    fn regex_class_and_quantifier() {
        assert_eq!(search("[0-9]+", "ab123cd"), Some("123".to_string()));
        assert_eq!(search("[^/]+", "/abc/def"), Some("abc".to_string()));
        assert_eq!(search("a.c", "xabcy"), Some("abc".to_string()));
    }

    #[test]
    fn regex_anchors_and_alternation() {
        assert_eq!(search("^a+$", "aaa"), Some("aaa".to_string()));
        assert_eq!(search("^a+$", "aaab"), None);
        assert_eq!(search("(aa|bb)+", "xxbbaa"), Some("bbaa".to_string()));
    }

    #[test]
    fn regex_lookahead() {
        assert_eq!(search(r"sdk\.js(?=[?#]|$)", "a/sdk.js?x=1"), Some("sdk.js".to_string()));
        assert_eq!(search(r"sdk\.js(?=[?#]|$)", "a/sdk.jsx"), None);
    }

    /// SDK 里 op 11 用的那条正则：脚本 src 实际匹配不到（与 CPython re.search 一致）
    #[test]
    fn regex_sdk_pattern_matches_cpython() {
        let pattern = r"/sentinel/[^/]+/sdk\.js(?=[?#]|$)";
        assert_eq!(search(pattern, "https://sentinel.openai.com/sentinel/sdk.js"), None);
        assert_eq!(search("/sdk.js", "https://sentinel.openai.com/sentinel/sdk.js"),
                   Some("/sdk.js".to_string()));
    }

    #[test]
    fn num_to_string_matches_js() {
        assert_eq!(num_to_string(0.0), "0");
        assert_eq!(num_to_string(8.0), "8");
        assert_eq!(num_to_string(0.5), "0.5");
        assert_eq!(num_to_string(0.000001), "0.000001");
        assert_eq!(num_to_string(1.5e-7), "1.5e-7");
        assert_eq!(num_to_string(1e21), "1e+21");
        assert_eq!(num_to_string(f64::NAN), "NaN");
        assert_eq!(num_to_string(f64::NEG_INFINITY), "-Infinity");
    }

    #[test]
    fn js_value_semantics() {
        assert_eq!(to_string(&Value::Undef), "undefined");
        assert_eq!(to_string(&Value::Null), "null");
        assert_eq!(to_string(&Value::Num(1.0)), "1");
        assert!(strict_eq(&Value::Undef, &Value::Undef));
        assert!(!strict_eq(&Value::Undef, &Value::Null));
        assert_eq!(to_number(&Value::Str(std::rc::Rc::from(" 42 "))), 42.0);
        assert!(to_number(&Value::Str(std::rc::Rc::from("x"))).is_nan());
    }

    #[test]
    fn base64_roundtrip_and_utf8() {
        use crate::value::{b64_decode, b64_encode};
        assert_eq!(b64_encode("abc"), "YWJj");
        assert_eq!(b64_decode("YWJj"), "abc");
        // 含 >255 的字符时 Python 的 b64e 走 UTF-8；b64d 再按 latin-1 解码，
        // 于是 atob(btoa(x)) 得到的是 mojibake（**故意**与 Python 一致）
        assert_eq!(b64_encode("\u{321}"), "zKE=");
        assert_eq!(b64_decode("zKE="), "\u{cc}\u{a1}");
    }
}
