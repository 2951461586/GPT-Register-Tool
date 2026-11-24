//! SO token（session observer）—— `sentinel_py._dispatch_behavior` 的逐行移植。
//!
//! Python 参考：
//! * `sentinel_py.py::_dispatch_behavior`     → [`dispatch_behavior`]
//! * `sentinel_py.py::_rng`                   → [`rng_range`]
//! * `sentinel_py.py::get_sentinel_token_py` 的 SO 分支 → [`mint_so`] / [`mint_so_full`]
//!
//! SO 链路（与 Python 完全一致）：
//!
//! ```text
//! new_vm(profile)
//!   └─ set(16, p) / run collector_dx        // 注册事件监听器
//!        └─ dispatch_behavior(...)          // 合成 pointermove/click/scroll/wheel/keydown
//!             └─ reset done/out/err + run snapshot_dx  → so
//! ```
//!
//! 三个必须保留的坑：
//!
//! 1. **collector 跑完 `done` 可能是 `true`**：事件回调里的 VM 指令会被直接跳过，
//!    所以 [`dispatch_behavior`] 一进来就把 `done` 置回 `false`（Python 踩过）。
//! 2. **`_rng(a, b)` 的公式是 `a + int(u * max(1, b - a + 1))`**，`u` 来自
//!    `random.random()`；Python 里每个 `time.sleep(_rng(...))` 也会消耗一次随机，
//!    Rust 侧必须照样消耗，否则事件序列会错位。
//! 3. **事件对象的字段顺序**：Python 是先建基础字段再 `ev.update(extra)`，
//!    JSON 化时键序即插入序，所以这里用 `Vec<(String, Value)>` 保序。
use crate::env::b;
use crate::mint::{load_program, new_vm};
use crate::token::{Profile, Rng};
use crate::value::*;
use crate::vm::Vm;
use std::rc::Rc;

fn s(text: &str) -> Value {
    Value::Str(Rc::from(text))
}

/// `_rng(a, b)`：`a + int(random.random() * max(1, b - a + 1))`。
///
/// `int()` 是向零截断，等于 Rust 的 `f64 as i64`。
pub fn rng_range(rng: &mut Rng, a: i64, b: i64) -> i64 {
    let span = (b - a + 1).max(1) as f64;
    a + (rng.next_f64() * span) as i64
}

/// 合成并派发一个事件（对应 Python 闭包 `emit`）。
///
/// 基础字段与顺序：`type, bubbles, cancelable, defaultPrevented, timeStamp,
/// target, currentTarget, preventDefault, stopPropagation, stopImmediatePropagation`，
/// 之后按 `extra` 的顺序追加。
fn emit(vm: &mut Vm, etype: &str, extra: Vec<(&str, Value)>) {
    let window = Value::Obj(vm.window.clone());
    let mut pairs: Vec<(String, Value)> = Vec::with_capacity(11 + extra.len());
    pairs.push(("type".into(), s(etype)));
    pairs.push(("bubbles".into(), Value::Bool(true)));
    pairs.push(("cancelable".into(), Value::Bool(true)));
    pairs.push(("defaultPrevented".into(), Value::Bool(false)));
    pairs.push(("timeStamp".into(), Value::Num(vm.perf_now)));
    pairs.push(("target".into(), window.clone()));
    pairs.push(("currentTarget".into(), window));
    // Python: `lambda *a: None` —— jsstr/JSON 里都序列化成 native function
    pairs.push(("preventDefault".into(), b("noop")));
    pairs.push(("stopPropagation".into(), b("noop")));
    pairs.push(("stopImmediatePropagation".into(), b("noop")));
    pairs.push((HIDDEN_KIND.to_string(), s("d")));
    for (key, value) in extra {
        pairs.push((key.to_string(), value));
    }

    let event = Value::Obj(new_obj(pairs));
    // Python: `target.dispatchEvent(ev)`；监听器异常在两侧都被吞掉
    // （Python 的 `EventTargetStub.dispatchEvent` 是 `try: cb(ev) except: pass`）。
    if let Some(dispatch) = obj_get(&vm.window, "dispatchEvent") {
        if let Err(err) = vm.call(&dispatch, std::slice::from_ref(&event)) {
            if std::env::var_os("SENTINEL_SO_DEBUG").is_some() {
                eprintln!("[so] 事件回调异常 {}: {}", etype, err.0);
            }
        }
    }
}


/// `_dispatch_behavior(ctx, profile, duration_ms=0)` 的逐行移植。
///
/// `profile` / `duration_ms` 在 Python 里也没有被读取（保留形参是为了对齐签名）。
pub fn dispatch_behavior(vm: &mut Vm, profile: &Profile, rng: &mut Rng, duration_ms: u64) {
    let _ = (profile, duration_ms);

    // collector 跑完时 done 可能为 True；事件回调必须能继续执行 VM 指令。
    vm.done = false;

    let moves = rng_range(rng, 12, 16);
    let mut x = rng_range(rng, 260, 420);
    let mut y = rng_range(rng, 180, 300);

    for _ in 0..moves {
        let dx = rng_range(rng, 5, 18);
        let dy = rng_range(rng, -4, 12);
        x += dx;
        y += dy;
        // Python: time.sleep(_rng(70, 145) / 1000.0) —— 随机照样要消耗
        let _throttle = rng_range(rng, 70, 145);
        emit(
            vm,
            "pointermove",
            vec![
                ("clientX", Value::Num(x as f64)),
                ("clientY", Value::Num(y as f64)),
                ("screenX", Value::Num(x as f64)),
                ("screenY", Value::Num(y as f64)),
                ("movementX", Value::Num(dx as f64)),
                ("movementY", Value::Num(dy as f64)),
                ("buttons", Value::Num(0.0)),
            ],
        );
    }

    let _throttle = rng_range(rng, 90, 220);
    emit(
        vm,
        "click",
        vec![
            ("clientX", Value::Num(x as f64)),
            ("clientY", Value::Num(y as f64)),
            ("screenX", Value::Num(x as f64)),
            ("screenY", Value::Num(y as f64)),
            ("button", Value::Num(0.0)),
            ("buttons", Value::Num(0.0)),
        ],
    );

    for _ in 0..rng_range(rng, 3, 4) {
        let _throttle = rng_range(rng, 80, 180);
        let current = match obj_get(&vm.window, "scrollY") {
            Some(Value::Num(n)) => n,
            _ => 0.0,
        };
        let next = current + rng_range(rng, 35, 120) as f64;
        obj_set(&vm.window, "scrollY", Value::Num(next));
        obj_set(&vm.window, "pageYOffset", Value::Num(next));
        emit(
            vm,
            "scroll",
            vec![
                ("scrollX", Value::Num(0.0)),
                ("scrollY", Value::Num(next)),
            ],
        );
    }

    let _throttle = rng_range(rng, 80, 160);
    emit(
        vm,
        "wheel",
        vec![
            ("deltaX", Value::Num(0.0)),
            ("deltaY", Value::Num(rng_range(rng, 70, 140) as f64)),
            ("clientX", Value::Num(x as f64)),
            ("clientY", Value::Num(y as f64)),
        ],
    );

    for key in ["L", "u", "Tab"] {
        let _throttle = rng_range(rng, 90, 210);
        let code = if key == "Tab" {
            "Tab".to_string()
        } else {
            format!("Key{}", key.to_uppercase())
        };
        emit(
            vm,
            "keydown",
            vec![
                ("key", s(key)),
                ("code", s(&code)),
                ("repeat", Value::Bool(false)),
                ("altKey", Value::Bool(false)),
                ("ctrlKey", Value::Bool(false)),
                ("metaKey", Value::Bool(false)),
            ],
        );
    }
}

/// SO 链路的产物。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SoOutput {
    pub so: String,
    /// collector + snapshot 的指令数（两个程序共用一个 VM，steps 累加）。
    pub steps: usize,
    /// 只跑完 collector 时的指令数。
    pub collector_steps: usize,
    /// dispatch_behavior 期间（事件回调）执行的指令数。
    pub dispatch_steps: usize,
    /// collector 注册的事件类型（Python: `ctx.target.listeners`），用于排查“事件没人收”。
    pub listeners: Vec<String>,
}

/// `get_sentinel_token_py` 的 SO 分支：collector → dispatch → snapshot → `so`。
///
/// 随机源由调用方注入（比对时用 [`Rng::constant`]`(0.5)`，生产用 [`Rng::system`]）。
pub fn mint_so_full(
    collector_dx: &str,
    snapshot_dx: &str,
    p: &str,
    profile: &Profile,
    rng: &mut Rng,
) -> Result<SoOutput, String> {
    let mut machine = new_vm(profile);

    // Python `_run_program(collector_dx, request_p, ctx)`
    machine.set(16.0, s(p));
    let collector = load_program(collector_dx, p)?;
    machine.done = false;
    machine.out = None;
    machine.err = None;
    machine.run_program(collector, p).map_err(|e| e.0)?;
    let collector_steps = machine.steps;
    let mut listeners: Vec<String> = machine.listeners.keys().cloned().collect();
    listeners.sort();

    dispatch_behavior(&mut machine, profile, rng, 0);
    let dispatch_steps = machine.steps - collector_steps;

    let mut so = String::new();
    if !snapshot_dx.is_empty() {
        // Python: done/out/err 归零，只换队列（不重设寄存器 16）
        machine.done = false;
        machine.out = None;
        machine.err = None;
        let snapshot = load_program(snapshot_dx, p)?;
        machine.load_queue(snapshot);
        machine.run().map_err(|e| e.0)?;
        so = machine.out.clone().unwrap_or_default();
    }

    Ok(SoOutput {
        so,
        steps: machine.steps,
        collector_steps,
        dispatch_steps,
        listeners,
    })
}

/// 对拍用的 window 状态快照：`__oai_so_*` 全部键值（jsstr）。
///
/// collector 把采集结果直接写在 window 上，snapshot 再从这里读；
/// 两个实现对不上时，先 diff 这些键最快。
pub fn so_state(vm: &Vm) -> Vec<(String, String)> {
    let mut pairs: Vec<(String, String)> = vm
        .window
        .borrow()
        .iter()
        .filter(|(k, _)| k.starts_with("__oai_so"))
        .map(|(k, v)| (k.clone(), to_string(v)))
        .collect();
    pairs.sort();
    pairs
}

/// [`mint_so_full`] 的便捷版：返回 `so` 字符串。
pub fn mint_so_with_rng(
    collector_dx: &str,
    snapshot_dx: &str,
    p: &str,
    profile: &Profile,
    rng: &mut Rng,
) -> Result<String, String> {
    mint_so_full(collector_dx, snapshot_dx, p, profile, rng).map(|out| out.so)
}

/// `mint_so(collector_dx, snapshot_dx, p, profile)`：生产入口。
///
/// 行为随机用 [`Rng::system`]（= Python 的 `random.random()` 真随机）。
/// 要做逐字节对拍请用 [`mint_so_with_rng`] 传 [`Rng::constant`]`(0.5)`
/// （`tools/so_compare.py` / `so_check` 就是这么跑的）。
pub fn mint_so(
    collector_dx: &str,
    snapshot_dx: &str,
    p: &str,
    profile: &Profile,
) -> Result<String, String> {
    let mut rng = Rng::system();
    mint_so_with_rng(collector_dx, snapshot_dx, p, profile, &mut rng)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rng_range_matches_python_formula() {
        // random.random() == 0.5 时：a + int(0.5 * span)
        let mut rng = Rng::constant(0.5);
        assert_eq!(rng_range(&mut rng, 12, 16), 14); // 12 + int(2.5)
        assert_eq!(rng_range(&mut rng, 260, 420), 340); // 260 + int(80.5)
        assert_eq!(rng_range(&mut rng, -4, 12), -4 + 8); // -4 + int(8.5) = 4
        assert_eq!(rng_range(&mut rng, 70, 145), 70 + 38); // 70 + int(38.0)
    }

    #[test]
    fn rng_range_degenerate_span() {
        // b < a 时 max(1, ...) 兜底，等于 a
        let mut rng = Rng::constant(0.0);
        assert_eq!(rng_range(&mut rng, 5, 5), 5);
        assert_eq!(rng_range(&mut rng, 9, 4), 9);
    }
}
