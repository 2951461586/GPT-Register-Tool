# 协议注册模块扫描（2026-10-10）

> 范围：协议注册链路（`sms_tool/auth_flow/`、`sms_tool/registration_handlers.py`、
> `sms_tool/registration_otp_stages.py`、`sms_tool/registration_edge_challenge.py`、
> `sms_tool/registration_resume.py`、`sms_tool/registration_sentinel_stages.py`、
> `sms_tool/http_client.py`、`sms_tool/http_utils.py`、`sms_tool/proxy_edge_probe.py`、
> `sms_tool/registration_pulse.py`）与驱动它的对照工具
> `scripts/registration_ab.py` / `docs/current/registration-ab-runbook.md`。
>
> 本报告记录 HEAD `e94688b`（2026-10-10 12:13，工作区干净）的现状。10-07/10-08 的
> 落地（P0-A 机制门禁、P0-B S0–S4a、P1-A/B/C/D、F3 `arm_mismatch`）经核对**全部在位**；
> 本轮头条是**实验工具自身**的一处 P0：最新四个预注册实验**结构上不可判定**。
>
> 参考仓库副本（`runtime/tmp/refrepos/`）本轮已不在工作区，故无新增外部对照；
> 结论全部来自本仓代码与线上日志。

---

## 0. 结论摘要

| # | 结论 | 类型 | 证据 |
|---|---|---|---|
| 🔴 P0-A′ | **`read_toggles` 只登记 12 个已声明 toggle 中的 8 个** ⇒ P1-9 / P1-10 / P1-11 / P1-12 的 arm 恒记 `toggle_verified=false`，`compare` 恒返回 `unverified`，**`--config` 也救不回**；且 `unverified` 先于机制门禁返回 ⇒ 10-07 刚补的假对照门禁对这四个实验**完全不可达** | 实验工具缺陷 | `scripts/registration_ab.py:672`（修复前的手写 if 链）；`tests/test_registration_ab.py:450` 把键名写死成 P1-4..P1-8 |
| 🟡 P1-A′ | P1-6 / P1-7 的机制行是**裸 `print`** ⇒ 只进 `backend_stdout.jsonl`，不进 `sms_tool.log`；而 `build_record` 的 docstring 声称"stdout 行 ⇒ 两种日志通道都在"，runbook 的 `collect` 示例传的正是 `sms_tool.log` | 判据通道 | `sms_tool/http_utils.py:67`（P1-6 经 `label=`）、`sms_tool/registration_handlers.py:1070`（P1-7）；对照 `sms_tool/operator_output.py` 的双通道 `emit` |
| 🟡 P1-B′ | P1-7 的机制行 `Create account temporarily disallowed` **只在真的撞到 `registration_disallowed` 时才出现**，却登记在**常开**机制门禁上 ⇒ 未撞到即 `manipulation_failed`（应为 `not_judgeable`）。同一份 runbook 对 **P0-2b** 已明确采用后一种处置 | 门禁误判 | `scripts/registration_ab.py:263`；`docs/current/registration-ab-runbook.md` §3 的 P0-2b 段 |
| 🟡 P1-C′ | `e94688b` 声称"八个 `registration.*` A/B 开关共用 `steps._registration_flag`"，实际只覆盖 `auth_flow` 那八个；**4 个 A/B 开关仍各自手写**，且未知值语义分三套（`sentinel_password_bundle` 未知值 **fail-open 为真**） | 收敛不完整 | `registration_sentinel_stages.py:43`、`registration_edge_challenge.py:52`、`registration_handlers.py:975,1027`；`auth_flow/steps.py:89` |
| 🟢 P2-1 | `docs/current/protocol-registration.md:374` 的 Validation limits 枚举了 8 项对照，**漏 p1-9 / p1-10 / p1-11 / p1-12 / p0-2b**（runbook 里齐全） | 文档漂移 | `docs_consistency_scan.py` 只查 `file:line`/符号，看不见散文枚举 |
| 🟢 P2-2 | runbook §3/§4/§7 与脚本默认值写 `runtime/registration_ab/`，而**唯一存在的记录在 `runtime/ab/`**（`DEFAULT_OUT_DIR` 自 `c9386db` 起从未变过）⇒ 照 runbook 的 `compare` 示例复现 P1-5 会找不到文件 | 文档/实现不一致 | `scripts/registration_ab.py:57`；`docs/current/registration-ab-runbook.md:108,154,205`；`tests/test_registration_ab.py:933` 引用的是 `runtime/ab/` |

**一句话诊断**：代码侧的门禁与落地都还在（全量 6208 passed），但**对照工具把最新四个实验
挡在判定之外**——这正是 10-07 扫描 P0-A 要防的"假对照/不可判定"的**第二个面**：上次修的是
"开关开了但代码路径没执行"，这次是"开关被读了但工具没登记"。两处都必须修，否则 signin
形状系列（P1-10/11/12）永远只能靠手工 TSV 判读。

---

## 1. 基线与门禁（本轮）

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6208 passed, 8 skipped**, 1155 subtests passed |
| `pytest -q -k "registration or auth_flow or signin or edge_challenge or sentinel"` | 1121 passed, 96 subtests passed |
| `ruff check`（注册域 11 个模块） | All checks passed |
| `import_layer_ratchet` | OK（287 module-level edge(s) / 31 pair(s)；11 mutual，60 minority；186 delayed） |
| `delayed_import_ratchet` | OK: 421 ≤ baseline 421 |
| `unused_import_ratchet` | OK: 354 ≤ baseline 355 |
| `bare_print_ratchet` | OK（512 bare prints） |
| `config_key_ratchet` | OK（`registration=41`, `email_registration=33`） |
| `endpoints_literal_ratchet` | OK（235 inline literal / 41 file） |
| `docs_consistency_scan` | passed（release-v2026.10.08.md） |

10-07/10-08 落地核对（全部在位）：

- **P0-A**：`scripts/registration_ab.py` 的第二道操纵检查（`mechanism_marker` /
  `mechanism_seen` / `mechanism_ok`，`compare` 在 `underpowered` **之前**判
  `manipulation_failed`）。
- **P0-B S0+S1**：`proxy_edge_probe.edge_challenge_verdict` 三态 + `:edge_challenge` 后缀。
- **P0-B S2/S3/S4a**：`registration.edge_challenge_rotate_exit`、`p0-2b` 预注册、
  `unknown` 偏向换出口的独立计数；S4b（浏览器交棒）**按设计未落地**。
- **P1-A**：`registration.prime_navigation_headers`（`sec-fetch-site` / `-user`）。
- **P1-B**：AT 探测的边缘 403 保留 checkpoint（`:edge_blocked`）。
- **P1-C**：Sentinel `challenge_proof` 随 challenge 传递。
- **P1-D**：冻结配置段一律用 `Mapping`；`steps._registration_flag` 收敛了 `auth_flow` 的八个。
- **F3**：`email_otp_send_stuck:arm_mismatch` 在 `registration_pulse._is_otp_ban_signal`
  里**先于** `otp_send_stuck` 短路（`registration_pulse.py:149` 附近），事务臂不再被当出口级封禁。

---

## 2. 🔴 P0-A′ `read_toggles` 漏登记：四个实验不可判定

### 2.1 事实

修复前 `read_toggles`（`scripts/registration_ab.py`）是一条手写 `if key in registration`
链，只覆盖 8 个键；`EXPERIMENTS` 里声明了 **12** 个 toggle：

```text
declared: 12   read: 8
MISSING : registration.prime_navigation_headers         (P1-9)
          registration.signin_screen_hint_login_or_signup (P1-10)
          registration.signin_prompt_login             (P1-11)
          registration.signin_locale_ja_jp             (P1-12)
```

`build_record` 取 `actual = toggles.get(toggle_name)` ⇒ 缺失即 `None` ⇒
`verified = str(None) == str(expected)` 恒假；`compare_records` 的第一段就把
`toggle_verified` 为假的 arm 收进 `unverified` 并**提前 return**（早于
`manipulation_failed` 与功率检验）。

### 2.2 复现（修复前）

config 里四个键全写 `true`：

```text
read_toggles -> {}
p1-9-prime-navigation-headers      toggle_actual=None toggle_verified=False
p1-10-signin-screen-hint           toggle_actual=None toggle_verified=False
p1-11-signin-prompt-login          toggle_actual=None toggle_verified=False
p1-12-signin-locale                toggle_actual=None toggle_verified=False
compare verdict: unverified | ...pass --config so the toggle is verified...
```

`compare` 的提示让操作者"传 `--config`"，但传了也没用——**键根本没被读**。修复后
按两臂各自的 config 复现 P1-12，才走到真正的判定路径：

```text
P1-12 verdict: inconclusive | success rates within 0.05: default=0.7 locale=0.75
```

### 2.3 根因：门禁测的是**实例**不是**不变量**

`tests/test_registration_ab.py:450 test_read_toggles_exposes_every_new_toggle` 名为
"every new toggle"，实际把 P1-4..P1-8 的键名**写死**在断言里。加 P1-9..P1-12 时
没有人改它，也没有任何测试遍历 `EXPERIMENTS`（`:801` 只校验机制行的两两不同），
于是漏登记**零门禁**。

这与 10-07 的 P0-A 是同一族的两面：

| | 10-07 P0-A | 本轮 P0-A′ |
|---|---|---|
| 假对照的形态 | 开关开了，但代码路径**不可达**（`signup.py` 早返回） | 开关读了，但工具**没登记** |
| 门禁为什么没拦住 | 只核对 config 值，不核对路径是否执行 | 测试写死键名，不遍历设计表 |
| 代价 | 5/5 失败被读成"假设为假" | 四个实验**无法**产出任何 verdict |

### 2.4 修复（本轮已落地）

- `read_toggles` 改为**从 `EXPERIMENTS` 派生**键集合
  （`_registration_toggle_keys()`，`scripts/registration_ab.py:651`），只额外保留一个
  非实验 toggle（`edge_challenge_discrimination`，p0-2 是纯观察臂，`toggle=None`）。
  行为不变：仍只回填 config 里**存在**的键，`test_read_toggles` 的精确相等断言照过。
- 新增 `tests/test_registration_ab.py:479
  test_read_toggles_covers_every_declared_experiment_toggle`：**遍历 `EXPERIMENTS`**，
  断言每个声明的 toggle 都能被读到，且每个有布尔 arm 的实验都因此通过第一道操纵检查。
  下一个新实验不可能再静默漏掉。

> 遗留：P1-9..P1-12 的**机制门禁**现在可达了，但 `runtime/ab/` 里没有它们的 collect 记录
> （signin 系列是手工单账号跑 + TSV 判读，见 §7）。要产出 `compare` verdict 仍需按
> runbook 采两份 arm 记录（含 `--funnel`）。

---

## 3. 🟡 P1-A′ 两个机制行只在 stdout 通道上

`build_record` 的 docstring（`scripts/registration_ab.py:710`）写：

> A marker must be a stdout line (`print`/`emit`) so it survives **either** log
> channel the operator may hand to `collect`.

对 `emit` 成立，对裸 `print` **不成立**。本仓的两条通道是分开的：
`sms_tool.log` 只收 logging 记录（`logging_setup.py` 的 `ResilientRotatingFileHandler`），
`print` 经 `SanitizingTextIO` + `StdoutMirror` 只进 `backend_stdout.jsonl`，
**没有 tee**（`operator_output.py` 的模块 docstring 自己写明"evidence is split across"）。

| 实验 | 机制行 | 发出方式 | 落在 |
|---|---|---|---|
| P1-4 | `Create account password page` | `operator_output.emit` | 两通道 |
| P1-5 | `Signup continue declares screen_hint=signup` | `operator_output.emit` | 两通道 |
| P1-6 | `About you page prime` | `http_utils._follow_continue_url` 的裸 `print`（`:67`，经 `label=`） | **仅 stdout** |
| P1-7 | `Create account temporarily disallowed` | `registration_handlers.py:1070` 裸 `print` | **仅 stdout** |
| P1-8..P1-12 | 各自机制行 | `operator_output.emit` | 两通道 |

runbook §3 的 `collect` 示例传的是 `--log runtime/logs/processes/<pid>/sms_tool.log`。
按它执行 P1-6 / P1-7，标记找不到 ⇒ `mechanism_ok=false` ⇒ 误判 `manipulation_failed`。

**建议**：把这两条改为 `operator_output.emit`（或让 `collect` 同时接受
`backend_stdout.jsonl`），并把 docstring 的"either channel"改成事实陈述。属于
P1-6/P1-7 真正开跑之前的**前置修复**。

---

## 4. 🟡 P1-B′ 有条件机制行挂在常开门禁上（P1-7）

P1-7 的机制行 `Create account temporarily disallowed`（`scripts/registration_ab.py:263`）
只在 `create_account` 真的收到含 `registration_disallowed` 的响应时才打印
（`registration_handlers.py:1055-1071` 的 retry 分支）。若一个批次里**一次都没撞到**
该错误，治疗臂日志里没有标记 ⇒ `mechanism_ok=false` ⇒ `manipulation_failed`，而正确
结论应是"这个窗口没测到东西"。

runbook 对**同族**的 P0-2b 已经写明正确处置：

> 🔴 **P0-2b 故意不注册机制行**：它的机制检查是**有条件的**……用常开的机制门禁会把
> 「这个时间窗没发生挑战」误判成 `manipulation_failed`。所以它的机制检查写在判定规则里，
> 返回 `not_judgeable`。

P1-7 是同一个形状，却登记进了常开门禁。**建议**：与 P0-2b 同法——把 P1-7 的机制检查
移到判定规则（"两臂都没出现 `registration_disallowed` ⇒ `not_judgeable`"），
或给 `mechanism` 增加一个 `"conditional": true` 标志，让 `compare` 对它返回
`not_judgeable` 而不是 `manipulation_failed`。

---

## 5. 🟡 P1-C′ 开关守卫收敛不完整 + 未知值语义分叉

`e94688b` 的提交信息写："The eight `registration.*` A/B toggles each carried their own
copy of the frozen-config guard … They share `steps._registration_flag` now." 事实是
**只收敛了 `auth_flow` 内的八个**；A/B 设计表里的另外四个仍各自手写，且三套未知值语义：

| 开关 | 实验 | 位置 | 未知值（如 `"abc"`） |
|---|---|---|---|
| `sentinel_password_bundle` | P1-3 | `registration_sentinel_stages.py:43-45` | **真**（falsy 黑名单，fail-open） |
| `edge_challenge_rotate_exit` | P0-2b | `registration_edge_challenge.py:52-54` | 假（truthy 白名单） |
| `prime_about_you_page` | P1-6 | `registration_handlers.py:973-975` | 假（truthy 白名单） |
| `create_account_disallowed_backoff` | P1-7 | `registration_handlers.py:1025-1027` | 假（truthy 白名单） |
| （对照）`_registration_flag` | P1-4/5/8/9/10/11/12 + `existing_login_continue_on_verified_page` | `auth_flow/steps.py:89` | 回落到开关**自身默认值** |

即：一个拼错的 `sentinel_password_bundle` 值会把一个**默认关**的开关打开（它会改变
Sentinel payload 形状）；而其余三个同族开关在同样输入下保持关闭。这不是当前线上 bug
（四个都用了 `Mapping`，P1-D 已修），但"一处解析"的承诺没兑现，P1-D 那类缺陷的复制面
仍在。

**建议**：把这四个（以及非 A/B 的 `obtain_refresh_token`、`edge_challenge_discrimination`）
也接到同一个 `_registration_flag` 语义上；`auth_flow` 之外需要一个不反向依赖 `auth_flow`
的宿主（例如放进 `sms_tool/registration_policy.py` 或一个小的 `registration_flags.py`），
否则 `registration_handlers → auth_flow.steps` 会新增跨目录边、涨 ratchet。
属**独立**改动，不要与 P0-A′ 同批。

---

## 6. 🟢 P2 文档漂移

1. **Validation limits 枚举漏项**。`docs/current/protocol-registration.md:374-377` 列了
   8 项待对照（preflight login endpoint、Cloudflare-challenge observation、Sentinel
   password bundle、password-page prime、signup-continue screen hint、about-you page
   prime、create-account disallowed backoff、email-verification continue hint），
   **漏了 p1-9 / p1-10 / p1-11 / p1-12 / p0-2b**。runbook 的表格是齐全的，所以这是
   "契约文档落后于 runbook"。`docs_consistency_scan.py` 只看 `file:line` 与符号表，
   看不见散文枚举——这类漂移只能靠人工或新增门禁。

2. **记录路径不一致**。runbook §3/§4/§7 与 `scripts/registration_ab.py:57
   DEFAULT_OUT_DIR` 都写 `runtime/registration_ab/`，但工作区里**唯一存在的记录**是
   `runtime/ab/p1-5-signup-continue-screen-hint__{default,hint}.json`；
   `git log -S DEFAULT_OUT_DIR` 显示默认值自引入提交 `c9386db` 起从未变过，
   所以那两份记录是用 `--out-dir runtime/ab` 采的，而 landing-2026-10-07-p0a /
   scan-2026-10-07 / `tests/test_registration_ab.py:933` 引用的都是 `runtime/ab/`。
   照 runbook 的 `compare` 示例去读 `runtime/registration_ab/...` 会得到文件不存在。

   **建议**：统一到一个路径（倾向把 `DEFAULT_OUT_DIR` 改成 `runtime/ab` 以匹配既有证据，
   或把两份记录移动并同步三处引用），并在 runbook 里写明 `--out-dir` 的默认值。

---

## 7. 线上形状（10-08 最新证据，未变）

`runtime/tmp/reg_in/run_isolate_nine_20261008_084936.log`（9 个 iCloud，0/9）：

```text
[3-User register (email+password)]  Status: 200
  email_verification_mode = "passwordless_signup"
  original_screen_hint    = "signup"
  passwordless_email_otp_send_pending = true
[4-Trigger email OTP]  Email OTP send: 200 .../email-verification
[5-Get email OTP]  Email OTP send is still pending on the server; skipping the mailbox poll
[!] Registration failed: email_otp_send_stuck
```

与 `landing-2026-10-08-signin-locale.md` §4.3 的结论一致：**唯一与成败完全相关的判据**是
`after_otp_send` 的 `client_auth_session_keys` 里有没有 `passwordless_email_otp_send_pending`；
`email_verification_mode`（10-07 的 `passwordless_login` → 10-08 的 `passwordless_signup`）
**不是**判据。signin 形状三字段（`screen_hint` / `prompt` / `locale`）都不控制该挂起键。

signin 系列（P1-10/11/12）是**手工单账号**跑 + `runtime/tmp/reg_in/p1_12_replicate_results.tsv`
判读（n=5/4，远低于 `MIN_ARM_ATTEMPTED=30`），**没有**走 `collect`。这正是 §2 那个 P0 的
现实后果：harness 判不了它们，所以只能手工——修完 `read_toggles` 后才具备走 harness 的条件。

---

## 8. 落地记录（本轮）

| 项 | 改动 | 验证 |
| --- | --- | --- |
| P0-A′ | `scripts/registration_ab.py`：`read_toggles` 改为从 `EXPERIMENTS` 派生键集合（`_registration_toggle_keys`，新增 `_TOGGLE_KEYS_WITHOUT_AN_EXPERIMENT`）；新增 `tests/test_registration_ab.py::test_read_toggles_covers_every_declared_experiment_toggle` 遍历设计表 | `tests/test_registration_ab.py` **81 passed**；P1-12 两臂复现从 `unverified` 变为 `inconclusive`（进入真实判定路径） |

未落地（按优先级）：

1. P1-A′：P1-6 / P1-7 机制行改走 `operator_output.emit`（或让 `collect` 接受 stdout 镜像）。
2. P1-B′：P1-7 的机制检查改为条件式（`not_judgeable`），对齐 P0-2b。
3. P1-C′：四个手写开关收敛到统一守卫（需先定宿主，避免新增跨目录边）。
4. P2-1 / P2-2：文档枚举补全 + 记录路径统一。

---

## 9. 明确不做

| 项 | 理由 |
|---|---|
| 把 signin 形状家族（P1-10/11/12）再跑一轮 | §7 已证该家族不控制挂起键；landing-2026-10-08 明确"下一步离开 wire 字段"，转向出口 IP 声誉 / 指纹设备层 / 服务端风控窗口 |
| 让 `compare` 在 `unverified` 时继续跑机制检查 | `unverified` 的语义就是"配置快照与 arm 不符，结论不可解释"，继续判会掩盖操纵失败；正确修法是补登记，不是放宽 |
| 顺手把 `_registration_flag` 搬进 `registration_handlers` | 会新增 `registration_handlers → auth_flow.steps` 跨目录边并涨 `import_layer_ratchet`；P1-C′ 需要先定宿主 |
| 无条件对齐 SunnyRegister 的 OTP 超时重发 | 10-07 扫描 P2-1 已结论：本仓的止损有 6/6 实测支撑，差异不是缺陷 |

---

## 10. 复现方式

```powershell
# 门禁
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\import_layer_ratchet.py
.venv\Scripts\python.exe scripts\docs_consistency_scan.py

# P0-A′ 复现（修复后：两臂各自的 config 才能走到真实判定）
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from pathlib import Path; from scripts import registration_ab as ab; print(ab.read_toggles(Path('config.example.json')))"

# 线上证据（工作区内，未入库）
#   runtime/tmp/reg_in/run_isolate_nine_20261008_084936.log
#   runtime/tmp/reg_in/p1_12_replicate_results.tsv
#   runtime/ab/p1-5-signup-continue-screen-hint__{default,hint}.json
```

`runtime/tmp/` 与 `runtime/ab/` 均被 `.gitignore` 排除；本报告不含凭据。
