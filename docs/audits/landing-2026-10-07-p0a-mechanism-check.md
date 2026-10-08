# 落地记录：P0-A 机制判据 + P1-8 补发 continue（2026-10-07）

> 落地来源：`docs/audits/scan-2026-10-07-protocol-registration.md` §5 验证计划 **步骤 1**。
> 本文只记录**已落地**的部分与验证证据；`scan-2026-10-07` 的 P0-B / P1-A / P1-B / signin
> 形状实验**未动**（扫描明确要求分批，且 P0-A 是它们的前置）。
>
> 全部改动**默认关**、**零线上流量**；线上受控对照仍须操作者在真实批次上执行。

---

## 1. 为什么先做这一项

扫描的头条结论是：2026-10-07 的 P1-5 A/B 是**假对照**。
`registration.signup_continue_screen_hint` 的唯一读点在
`_continue_signup_username`（`sms_tool/auth_flow/signup.py`），而
`_prepare_signup_auth_state` 在 authorize 落 `/email-verification` 时**早返回**，
该函数一次都不会执行。`runtime/tmp/reg_in/run_p15_hint.log` 里因此没有一行
`Signup username continue`，hint arm 的 5/5 `email_otp_send_stuck` 是**开关空转**，
不是"screen_hint 无效"。

原来的 harness 只核对 `config.json` 里开关的值（`toggle_verified`），**不核对代码路径
是否执行** ⇒ "开关空转"会伪装成"开关无效"。

---

## 2. 落地内容

### 2.1 `scripts/registration_ab.py`：第二道操纵检查（机制行）

- 每个实验可注册 `mechanism`（一条 stdout 机制行）。`build_record` 记录
  `mechanism_marker` / `mechanism_expected` / `mechanism_seen` / `mechanism_ok`：
  - 处理臂（toggle 期望值为 `True`）⇒ 机制行**必须出现**；
  - 对照臂（`False`）⇒ 机制行**必须不出现**（同一开关跑在两个 arm 是混淆，不是对照）；
  - 实验未注册机制行，或 arm 的期望值不是布尔（P0-1 的 `browser` / `legacy` 是端点名）
    ⇒ `mechanism_ok = null`（**未检查**，绝不等于"失败"）。
- `compare_records` 在 `underpowered` **之前**判 `manipulation_failed`，并附
  `--config` 的 `unverified` 之后。两个拒绝类别都不进速率判定。
- `collect` 在机制未出现时打印 WARNING（与 toggle WARNING 并列）。
- `load_funnel` / `read_toggles` 对缺失或损坏的输入**降级而非抛异常**
  （`load_funnel → None`、`read_toggles → {}`，后者 fail-closed 成 `unverified`）。

机制表（**新开关必须登记一行**）：

| 实验 | 机制行 |
| --- | --- |
| P1-4 | `Create account password page` |
| P1-5 | `Signup continue declares screen_hint=signup` |
| P1-6 | `About you page prime` |
| P1-7 | `Create account temporarily disallowed` |
| P1-8 | `Email verification continue hint` |

> **修订（2026-10-07 扫描 §5.2）。** P1-5 原登记 `Signup username continue`，
> 但那是 `_continue_signup_username` 的**基线** print（开关开/关都打，P1-8 的强制路径
> 也打），不是开关分支内的行：对照臂只要走到该函数就会被误判，P1-8 开着时 P1-5 的
> hint 臂又会误通过。现改为分支内且 `not force` 才发的独占行
> （`sms_tool/auth_flow/signup.py`），并给 P1-5 的 `hold_constant` 钉上
> `signup_email_verification_continue_hint=false`。

P0-1 / P0-2 / P1-3 **未注册**：P0-1 两个 arm 都是字符串端点名，推不出机制行应在哪一侧；
P1-3 的 bundle 行走 logger（`Sentinel password flow bundle primed`），不保证落在
`backend_stdout.jsonl` 这类 stdout 日志里，登记它会在正确运行的 arm 上误报。
机制行必须是 `print` / `operator_output.emit` 的 **stdout** 行，才能同时活在两种日志里。

### 2.2 回放证据：`run_p15_hint.log` → `manipulation_failed`

```powershell
python scripts/registration_ab.py collect `
  --experiment p1-5-signup-continue-screen-hint --arm hint `
  --log runtime/tmp/reg_in/run_p15_hint.log `
  --config runtime/tmp/reg_in/runtime_p15_hint.json `
  --out-dir runtime/tmp/ab_replay
# WARNING: the toggle's mechanism never ran in this log
#   (expected marker 'Signup continue declares screen_hint=signup', seen=False)

python scripts/registration_ab.py compare `
  --arm default=runtime/ab/p1-5-signup-continue-screen-hint__default.json `
  --arm hint=runtime/tmp/ab_replay/p1-5-signup-continue-screen-hint__hint.json
```

```json
{"verdict": "manipulation_failed", "powered": false,
 "reason": "the toggle's mechanism never ran (or ran in the control) for: hint -- its log lacks the expected marker 'Signup continue declares screen_hint=signup'; re-collect with the arm's real run log before comparing rates"}
```

即：该 arm 现在是**机制未执行**，而不是被误读成"样本不足"或"假设被否"。
同一回放已固化为离线测试
（`tests/test_registration_ab.py::P15HintReplayTests`，日志缺失时自动跳过，因为
`runtime/tmp/` 被 `.gitignore` 排除）。

### 2.3 新开关：`registration.signup_email_verification_continue_hint`（默认 `false`）

把 P1-5 的**作用点**搬到它真正够得到的地方：

- `sms_tool/auth_flow/steps.py`：`_signup_email_verification_continue_hint_enabled()`
  （配置段判据用 `Mapping`，不用 `dict` —— P1-D 同类缺陷）。
- `sms_tool/auth_flow/signup.py`：
  - `_continue_signup_username(..., force=False)`：`force` 绕过入口守卫，并让 body
    声明 `screen_hint: "signup"`（不依赖兄弟开关）。
  - `_prepare_signup_auth_state` 的早返回分支：**仅**在
    `not passwordless_web` **且**落点确为 `/email-verification` **且**开关打开时，
    补发一次 `authorize/continue`。失败时按名记
    `email_verification_continue_hint_failed`。
  - 机制行 `Email verification continue hint` 经 `operator_output.emit` 走 stdout +
    结构化日志（`_emit_operator_line` 收敛函数内导入，`delayed_import` 计数不变）。
- `config.example.json` / `sms_tool/config.py` / `scripts/config_key_baseline.json`
  同步登记该键。

**密码泳道限定**：passwordless Web 泳道在该落点**故意不发** `authorize/continue`
（浏览器从该状态直接发 OTP），所以开关在那边是 no-op。

### 2.4 新实验：`p1-8-email-verification-continue-hint`（预注册）

`scripts/registration_ab.py` 的 `EXPERIMENTS` 增加该实验：arms `default` / `hint`，
主指标 `registered_per_attempted`，机制读数
`client_auth_session.email_verification_mode` 是否离开 `passwordless_*`，
判定规则与 P1-5 同构（`auth_state` 类失败增长 ⇒ 无论速率一律 `keep_default_off`）。
hold-constant 里明确要求两个 arm 都保持 `signup_continue_screen_hint=false`。
`docs/current/registration-ab-runbook.md` 已加入该行、机制表与停止规则。

---

## 3. 验证证据（本轮）

| 门禁 | 结果 |
|---|---|
| `pytest -q`（全量） | 5956 passed, 8 skipped |
| `ruff check` + `ruff format --check`（本轮改动文件） | All checks passed / already formatted |
| `scripts/config_key_ratchet.py` | `config-key ratchet OK (registration=35, email_registration=33)` |
| `scripts/bare_print_ratchet.py` | `bare-print ratchet OK (514 bare prints, baseline frozen)` |
| `scripts/delayed_import_ratchet.py` | `delayed-import ratchet OK: 419 <= baseline 419` |
| `scripts/docs_consistency_scan.py` | `Documentation consistency check passed` |
| 新增离线测试 | `tests/test_registration_ab.py`（机制判据 + 回放）、`tests/test_signup_email_verification_continue_hint.py`（15 例 / 16 子测试） |

---

## 4. 顺带修复的文档缺陷

`docs/audits/README.md` 的 `scan-2026-10-07-protocol-registration.md` 索引行**误把
10-04 扫描的整段描述拼在了 10-07 的 P0-A 结论之后**（两行内容重叠，10-07 行以
"…是**开关空转**而非"直接接上 10-04 的"pi-lens 报的 **470 边…"）。已按 10-07 报告
正文重写该行，并把该文件从 CRLF 归一为 LF（`tests/test_line_ending_guard.py` 要求）。

---

## 5. 明确未做

- **P0-B**（流程内挑战判据 / 同 flow 重刷 proof / 协议→浏览器交棒）：本轮的 S0+S1
  已于同日单独落地（见 `landing-2026-10-07-p0b-s0s1-challenge-verdict.md`）；
  **S2（换出口）/ S3（线上 A/B）/ S4（交棒）仍未做**。扫描明确要求 P0-A 与它们分批。
- **P1-A**（prime 导航头对齐 turb）：未动，仍是"落点正确 ≠ 状态建立"的未排除解释。
- **P1-B**（AT 探测边缘 403 保留 checkpoint）：未动。
- **signin 形状实验**（`screen_hint=login_or_signup` / `locale`）：未动；它是 P0-A 修好
  **之后**的下一步，见扫描 §5 步骤 6。
- **线上 A/B**：本轮零线上流量；`p1-8` 的重跑必须由操作者在受控批次上执行。
- **未改任何默认值**：新开关与既有四个开关全部默认 `false`。

---

## 6. 相关

- `docs/audits/scan-2026-10-07-protocol-registration.md` — 发现与 §5 计划
- `docs/current/registration-ab-runbook.md` — 预注册设计、机制表、判定与停止规则
- `docs/current/protocol-registration.md` — Validation limits（两道操纵检查的契约）
- `scripts/registration_ab.py` — `mechanism` 判据与 `p1-8` 实验
