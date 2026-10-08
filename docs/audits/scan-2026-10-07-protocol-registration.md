# 协议注册模块扫描（2026-10-07）

> 范围：协议注册链路（`sms_tool/auth_flow/`、`sms_tool/registration_handlers.py`、
> `sms_tool/registration_otp_stages.py`、`sms_tool/registration_preflight.py`、
> `sms_tool/http_client.py`、`sms_tool/sentinel/`、`sms_tool/registration_outcome.py`、
> `sms_tool/registration_pulse.py`）与在飞工作区改动。
>
> 参考项目（浅克隆到 `runtime/tmp/refrepos/`，**仅供参考，不入库**）：
>
> | 参考 | 提交 | 日期 | 说明 |
> |---|---|---|---|
> | `pxygit/SunnyRegister` | `bf5fb64` | 2026-09-29 | Go 后端 + `python-worker/sunny_core/` 纯协议泳道（`protocol_auth.py` 1391 行、`openai_auth.py` 3577 行）+ 浏览器泳道 |
> | `myfanhua/turb-gpt-free-register` | `ffcda12` | 2026-09-29 | 多驱动注册；协议链路在 `main.py` + `core/openai_auth.py`（与 10-01 扫描同一提交） |
>
> 本报告记录**近几轮线上执行取证 + 对照取证 + 待落地项**。历史审计不受
> `scripts/docs_consistency_scan.py` 的 `file:line` 门禁约束，但本文指针均按当前
> 工作区（未提交改动生效）核对。

---

## 0. 结论摘要

| # | 结论 | 类型 | 证据 |
|---|---|---|---|
| 🔴 P0-A | **P1-5（`signup_continue_screen_hint`）开关在目标失败形状上不可达** ⇒ 已跑的 A/B 是**假对照**，hint arm 的 5/5 `email_otp_send_stuck` 不能读成"screen_hint 无效" | 实验有效性 | `sms_tool/auth_flow/signup.py:294` 早返回 vs `signup.py:56` 唯一读点；`runtime/tmp/reg_in/run_p15_hint.log` |
| 🔴 P0-B | **流程内仍无出口级挑战判据、无同 flow 重刷 proof 重试、无协议→浏览器交棒**（`plan-2026-10-05` 未落地） | 能力缺口 | `sms_tool/http_client.py:140`；SunnyRegister `protocol_auth.py:610,619,393` |
| 🟡 P1-A | 密码页 prime 的**导航请求头不完整**（缺 `sec-fetch-site`/`sec-fetch-user`），turb 的发齐且落点错即 `raise`；P1-4"落点正确但事务仍 passwordless"的结论因此**有一个未排除的解释** | 变量未对齐 | `sms_tool/auth_flow/signup.py:151` + `sms_tool/http_utils.py:51`；turb `core/openai_auth.py:564` |
| 🟡 P1-B | AT 探测的**边缘 403 不保留 checkpoint** ⇒ "账号已建好、AT 只是被 CF 拦"的 run 会重走整个注册 | 代价不对称 | `registration_outcome.py:132`（只认 `status_code==0`）+ `accounts/account_liveness.py:295`（403→`unknown`）；SunnyRegister `access_token_probe.py` 显式分 `blocked` |
| 🟡 P1-C | 在飞修复：Sentinel `challenge_proof` 随 challenge 传递，`turnstile.dx` 用**取题那份 `p`** 解码（与 turb `_request_p` 同机制）——这也是 P1-3 bundle 假设成立的前提 | 在飞/正确 | `sentinel/client.py:271`、`sentinel/runner.py:135`、`sentinel/runtime/sentinel-runner.js:1399` |
| 🟡 P1-D | 在飞修复：配置段 `mappingproxy` vs `dict` ⇒ 三个注册开关在**生产**读不到、测试全绿 | 生产 bug 类 | `sms_tool/config.py` `_freeze`；`auth_flow/steps.py:107,157,180` |
| 🟢 P2-1 | OTP 超时不重发：SunnyRegister 超时后 `resend` 再等 60s；本仓在 `_pending` 标记上**直接止损**（判据有 6/6 实测支撑，但 `dispatched` 形状无补救） | 行为差异 | `registration_otp_stages.py:74,91`；SunnyRegister `protocol_auth.py:999` |
| 🟢 P2-2 | `email_otp_send_stuck` 被 pulse 当作**出口级**封禁并换池；而当前证据指向**事务臂**（服务端把事务架成 passwordless）而非出口 | 杠杆错配 | `registration_pulse.py:141`、`run.log` 的 `[Pulse] OTP dispatch route blocked` |

**一句话诊断**：近两轮（10-06 / 10-07）的协议注册**全部停在同一步**——服务端把注册事务架在
passwordless 邮箱验证臂上（`email_verification_mode=passwordless_login` 而
`original_screen_hint=signup`），`user/register` 要么 400 `invalid_auth_step`、要么 200 但
`email-otp/send` 之后 `passwordless_email_otp_send_pending` 不消失。两个参考项目都用
"**强制先导航到密码页**"绕开该分支，本仓的 P1-4 prime 复现了这一步却**没有改变事务臂**，
而 P1-5 的开关**根本不在失败形状的代码路径上**。⇒ 下一步变量应是**登录/signin 形状**
（turb 用 `screen_hint=login_or_signup` 且不发 `authorize/continue`；SunnyRegister 用
`locale=ja-JP` 且密码/TOTP LS 泳道），但在动它之前必须先修 P0-A 的实验有效性。

---

## 1. 近几轮执行情况（2026-10-06 ~ 2026-10-07）

### 1.1 失败形状（两个泳道、两种错误名，同一根因）

| 运行 | 时间 | 泳道 / signin 尝试 | `[2-Auth flow]` 落点 | `[3-User register]` | `[4-Trigger email OTP]` | 终态 |
|---|---|---|---|---|---|---|
| `runtime/tmp/reg_in/run.log` | 10-06 13:57 | passwordless / `login_or_signup` | `/email-verification` | 跳过（passwordless） | `send 200` → `/email-verification` | `email_otp_send_stuck` |
| `runtime/tmp/reg_in/run_p15_hint.log`（hint arm） | 10-07 11:26 | password / `signup_screen_hint` | `/email-verification` | `200` `page.type=email_otp_send` | `send 200` → `/email-verification` | `email_otp_send_stuck` ×5 |
| `runtime/logs/processes/2256`、`runtime/tmp/reg_in/run_p15_obs_probe.log` | 10-07 12:16 | password / `signup_screen_hint` | `/email-verification` | `400 invalid_auth_step` | —（未到） | `password_step_unconfirmed:invalid_auth_step` |

`run_p15_obs_probe.log` 的 `client_auth_session` 信号（增强 dump 生效后的第一条完整证据）：

```text
client_auth_session.email_verification_mode       = "passwordless_login"
client_auth_session.original_screen_hint          = "signup"
client_auth_session.passwordless_disabled         = false
client_auth_session.passwordless_otp_from_password_redirect = false
client_auth_session.signup_source                 = ""
```

即：我们请求 `signup`，服务端**记录**了 `original_screen_hint=signup`，却把
`email_verification_mode` 架成 `passwordless_login`。随后带密码的 `user/register` 被拒
（`invalid_auth_step`），或（另一批）被接受但发码永不派发。**这是"事务臂"问题，不是出口问题。**

### 1.2 A/B harness 状态

`python scripts/registration_ab.py compare --arm default=... --arm hint=...`：

```json
{"experiment": "p1-5-signup-continue-screen-hint", "powered": false,
 "verdict": "underpowered", "reason": "each arm needs >= 30 attempted registrations",
 "metrics": {"default": {"attempted": null, "probes": 0}, "hint": {"attempted": null, "probes": 10}}}
```

两个 arm 的 `funnel` 都是 `null`（未采集批次报告）⇒ `attempted` 不可算。hint arm 的
`preflight` 是 10/10 通过（IN 池），与 `run_p15_hint.log` 的 0/5 注册并存。

P1-4（`prime_create_account_password`）已在
`sms_tool/auth_flow/steps.py:165` 的 docstring 里**单臂定案**："prime GET 5/5 执行且落点正确，
服务端仍架 passwordless 臂 ⇒ 密码页状态**不是**服务端选臂的判据；不要把该变量再单独测一次。"

### 1.3 pulse 的处置

`run.log` 显示 `[Pulse] ⚠ OTP dispatch route blocked … Proxy pool cursor rotated`。
`registration_pulse._is_otp_ban_signal`（`registration_pulse.py:141`）把
`email_otp_send_stuck`（含 `otp_send_stuck`）判为**出口级**封禁并换池。若真因是事务臂
（见 §1.1），换出口是**错的杠杆**——与
`docs/current/protocol-registration.md` 里"extractor 用换出口对付缺 gate token 是错的杠杆"
是同一类教训。

---

## 2. 本仓模块现状

| 层 | 模块 | 契约 |
|---|---|---|
| 步骤函数 | `sms_tool/auth_flow/`（`steps`/`signup`/`login`/`otp`/`totp`/`password_step`/`sentinel_flow`/`deps`） | `docs/current/protocol-registration.md` |
| 阶段编排 | `sms_tool/registration_handlers.py` | 阶段序：auth_flow → user_register → OTP send/wait/validate → create_account → session → AT probe → TOTP |
| OTP 三阶段 | `sms_tool/registration_otp_stages.py` | `send` / `wait` / `validate` + 两个根因判据 |
| 预检 | `sms_tool/registration_preflight.py` | 浏览器入口页 + CF 判据（P0-1/P0-2 已落地，仍待线上对照） |
| HTTP | `sms_tool/http_client.py` | 403/429 → `session_circuit_open`（无挑战判别、无换出口、无重刷 proof） |
| Sentinel | `sms_tool/sentinel/`（`bundle`/`client`/`runner`） | Node SDK runner + `FLOW_PAGE_URLS` 单一 flow→page 表 |
| 结果契约 | `registration_outcome.py` / `registration_result.py` / `registration_funnel.py` | AT 探测决定 success |
| 熔断/脉冲 | `registration_pulse.py` / `registration_retry_guard.py` / `failure_registry.py` | 类别 → 重试/掉号/换出口 |

**10-01 扫描的六项已落地**（P0-1 预检端点、P0-2 预检 CF 判据、P1-1 `check_coupon`、
P1-2 金额交叉一致、P1-3 Sentinel bundle、P2 账单模板），其中 **P0-1 端点 / P0-2 流程内换出口 /
P1-3 bundle 三项仍未线上受控对照**——本报告 §1 说明：这三项都**不是**当前失败形状的成因。

---

## 3. 对照参考项目

### 3.1 SunnyRegister `python-worker/sunny_core/protocol_auth.py`

| 能力 | 位置 | 本仓对应 | 判定 |
|---|---|---|---|
| 挑战判别（403/429/body 关键词，先排除账号已废） | `:610 _is_challenge_response` | 无（`http_client.py:140` 一律熔断） | **差距**（P0-B） |
| 同 flow 重刷 proof 重试（最多 3 次，保留 cookie） | `:619 _request_with_sentinel_retry` | 无（每次请求新 mint，但被 403 熔断挡住） | **差距**（P0-B） |
| 协议→浏览器交棒（cookie jar + 落点 URL + 已生成密码 + 流量快照） | `:393 _browser_handoff_snapshot` | 无 | **差距**（P0-B / `plan-2026-10-05` H2） |
| `turnstile.dx` 用**取题那份 `p`** 解码（Node runner 回交 `cachedProof`） | `:463 _sentinel_headers_once`（`_request_p` 同族） | 在飞修复（P1-C） | **已对齐（在飞）** |
| 密码/TOTP "LS" 登录泳道 + `ProtocolLoginSecretRejected` 分类 | `:783 _submit_password`、`:836 _verify_login_password`、`:875 _complete_mfa` | 有密码泳道 + `auth_flow/totp.py` | 同级 |
| workspace 选择 | `:935 _select_workspace` | `auth_flow/` 无 | 次要（P2-3，注册新号无影响） |
| OTP 超时→重发→再等 60s | `:999 _verify_email` | `wait_email_otp` 在 `_pending` 上止损 | 差异（P2-1，本仓止损有 6/6 实测支撑） |
| AT 探测四态 `valid/invalid/blocked/probe_failed` + 代理被拦改直连 | `access_token_probe.py` | `probe_account_liveness` 403→`unknown`，无直连回退 | **差距**（P1-B） |
| `login_or_signup` 强制密码页：`_submit_password` **先 GET** `/create-account/password` 再 POST | `:783` | P1-4 prime 已落地（但头不齐，见 P1-A） | 变量未对齐 |
| signin 形状：`locale=ja-JP`、`screen_hint` 恒定、`authorize/continue` 带 `screen_hint` | `:645 _start_next_auth`、`:738 _authorize_email` | 本仓 signin 无 `locale`；continue 带 `screen_hint` 的开关**在失败形状不可达** | **待验**（P2-2 + P0-A） |

⚠ 注意：SunnyRegister 的 `run()`（`:1202`）在 `initial_path == "/email-verification"` 时
**同样跳过** `_authorize_email`——所以它的 `screen_hint` continue 体在这条形状上也是空转的。
**它的差异化不在 continue，而在 signin 形状与强制密码页导航。**

### 3.2 turb `main.py` + `core/openai_auth.py`

| 能力 | 位置 | 本仓对应 | 判定 |
|---|---|---|---|
| 预检只打浏览器入口页（不单独打 `auth.openai.com/log-in`） | `:236 network_preflight` | P0-1 已落地 | 已对齐 |
| authorize 403 立即停止、不重放 OAuth state | `:263 follow_authorize` | `http_client` 熔断（行为相近，但无挑战判别） | 部分 |
| **强制导航到密码页且落点错即 `raise`**，带 `sec-fetch-site: same-origin` + `sec-fetch-user: ?1` | `:564 navigate_create_account_password`；`main.py:345` | P1-4 prime（**头不齐 + 非致命**） | **变量未对齐**（P1-A） |
| `p` 随 challenge 回交 SDK（`data["_request_p"]`） | `:317 request_sentinel_token`、`:413 request_password_sentinel_bundle` | 在飞修复（P1-C） | 已对齐（在飞） |
| 密码页 bundle：同一 `p` 跨 `email_otp_validate` / `username_password_create` / `authorize_continue` | `:413` | P1-3 开关（默认关，未对照） | 已有能力 |
| signin 形状：`prompt=login` + `screen_hint=login_or_signup` + `login_hint`，**不发 `authorize/continue`** | `core/chatgpt_auth.py:140 signin_openai` | 本仓首尝试 `screen_hint=signup`，落 `/email-verification` 后不发 continue | **待验（最高优先级）** |
| `navigate_about_you` 先导航再 POST | `:695`；`main.py` 阶段5 | P1-6 开关（默认关） | 已有能力 |
| `registration_disallowed` 有界退避 `[8,20,45]s`（每轮重铸 proof） | SunnyRegister `:1058 _create_account`（turb 侧对应机制） | P1-7 开关（默认关） | 已有能力 |
| `detect_account_unusable_text` + `_ACCOUNT_DEAD_CODES` | `:80,95` | `failure_registry` / `account_terminal` | 同级 |

---

## 4. 发现与建议

### 🔴 P0-A 先修 P1-5 的实验有效性：开关在目标形状上不可达

`_prepare_signup_auth_state` 在 authorize 落 `/email-verification` 时**直接早返回**
（`sms_tool/auth_flow/signup.py:294`）：

```python
if steps._is_signup_password_step(current_url) or steps._is_email_verification_step(current_url):
    return {"ok": True, ..., "skipped": True, ...}
```

而 `_signup_continue_screen_hint_enabled()` 的**唯一读点**在
`_continue_signup_username`（`signup.py:56`），该函数在 `signup.py:355/385` 才被调用。
⇒ 落 `/email-verification` 时**一次都不会执行**，开关等于没开。
`run_p15_hint.log` 里没有任何 `Signup username continue` 行（该行只有 4 个历史进程日志命中，
全在 09-16 批次 34632），与代码一致。

`scripts/registration_ab.py` 的操纵检查（`build_record`，`:362`）只核对
`config.json` 里 toggle 的值（`toggle_verified`），**不核对代码路径是否执行** ⇒
"开关空转"会伪装成"开关无效"。

**建议（按序）**：
1. 在 `build_record` 增加**机制证据**字段（例如 `mechanism_seen`：该 arm 的日志里是否出现
   该开关独有的行——P1-5 看 `Signup username continue`、P1-4 看 `Create account password page`、
   P1-6 看 `About you page prime`）。机制未出现 ⇒ 该 arm 记 `inconclusive(manipulation_failed)`，
   不得进 `compare` 的速率判定。
2. 把 P1-5 的**假设与作用点对齐**：假设要测的是"落 `/email-verification` 时**补发一次带
   `screen_hint: signup` 的 `authorize/continue`** 能否把事务臂从 passwordless 扳回 signup"，
   那就必须在早返回分支里调用 `_continue_signup_username`（新开关、单变量、独立 A/B），
   而不是复用现有开关。
3. 在结论回流前，**不得**把 `run_p15_hint.log` 的 5/5 写成"screen_hint 无效"。

### 🔴 P0-B 流程内挑战判据 / 同 flow 重试 / 交棒（`plan-2026-10-05` 落地）

`plan-2026-10-05-inflow-challenge-handoff.md` 已给出完整契约（四开关、`edge_challenge_verdict`
三态、三条与 `session_circuit_open` 的不变式、预注册 A/B、6 条明确不做）。本仓现状未变：

- `sms_tool/http_client.py:140`：403/429 → 熔断，`_raise_if_circuit_open` 让后续请求直接抛
  `SessionCircuitOpen`（`http_client.py:11`）。
- `classify_edge_response`（`sms_tool/proxy_edge_probe.py:134`）仍只被预检 / 查优惠 / UPI 消费。
- 代价不对称（本项动机）：预检通过后的挑战发生在**邮箱与 OTP 已消耗之后**，false negative
  永久损失邮箱槽；false positive 只多一次请求 ⇒ 判据应偏向召回并保留 `unknown`。

**建议**：按 `plan-2026-10-05` 的 S0+S1（只读判据 + 失败名细分，零行为变更）先行落地，
再按预注册 A/B 决定换出口 / 交棒开关。**不要**把本报告的 P0-A/P1-A 与它同批做。

### 🟡 P1-A 密码页 prime 的导航头不完整

- 本仓：`_prime_create_account_password_page`（`signup.py:151`）→ `_follow_continue_url`
  （`sms_tool/http_utils.py:51`），只发 `Accept` + `Referer`。
- turb：`navigate_create_account_password`（`core/openai_auth.py:564`）发
  `sec-fetch-site: same-origin` + `sec-fetch-user: ?1`，落点非密码页即 `raise`。
- P1-4 的结论"prime 5/5 落点正确但事务仍 passwordless"把"落点"当成了"状态已建立"的充分证据。
  请求头不完整（无真实顶层导航语义）是**未排除的解释**。

**建议**：把 prime 的请求头对齐 turb（`sec-fetch-site` / `sec-fetch-user`），
作为**独立单变量**再做一次有界对照（默认仍关）；同时在 docstring 里区分
"落点正确"与"状态建立"两个判据。这与 `plan-2026-10-05` 里"交棒要带真实导航语义"同源。

### 🟡 P1-B AT 探测的边缘 403 不保留 checkpoint

- `_retain_registration_checkpoint`（`registration_outcome.py:132`）只在
  `status_code == 0` 时保留 checkpoint。
- `probe_account_liveness`（`accounts/account_liveness.py:150`）经
  `quota_result_from_payload`（`:295`）只把 401 判 `token_invalid`；CF 边缘 403 返回
  `status="unknown"`、`status_code=403`。
- ⇒ `_registration_outcome` 以 `access_token_probe_http_403` 收场（`:340`），checkpoint 不保留，
  批处理重走整个注册（→ `user_already_exists`）。**账号其实已经建好了。**
- 对照：SunnyRegister `probe_access_token` 显式分 `blocked` 与 `invalid`，代理被拦时改**直连重试**，
  并明写"未判定令牌失效"。

**建议**：`_retain_registration_checkpoint` 的判据从 `status_code == 0` 放宽为
"`status_code == 0` **或** 边缘拦截 403（非 `token_invalid`）"，并把 probe 的 `status`
纳入判定；同时在 probe 侧补一次直连回退（对齐 SunnyRegister）。

### 🟡 P1-C 在飞修复：Sentinel `challenge_proof` 绑定（保留）

`_challenge` 现在返回 `(payload, proof)`（`sentinel/client.py:271`），`issue_sentinel_token`
把它作为 `challenge_proof` 传给 `run_sentinel_sdk`（`sentinel/runner.py:135`），runner 经
`--challenge-proof` 注入（`sentinel-runner.js:1319`），`handleIframeMessage` 用
`options.challengeProof || message.p` 作为 `cachedProof`（`:1399`）。理由与 turb
`request_sentinel_token` 的 `data["_request_p"]` 一致：`turnstile.dx` 与取题那份 `p` 绑定。

**这条是 P1-3 bundle 假设的前提**：若 dx 解码用的是 VM 内重新采样的另一份 `p`，
"三条 flow 共享同一 `p`"的对照就测不出差异（被解码失败掩盖）。落地时应在
`docs/current/protocol-registration.md` 的 Sentinel 契约里补一句 `p`/`dx` 绑定关系。

### 🟡 P1-D 在飞修复：`mappingproxy` vs `dict`（生产开关 fail-closed）

`_existing_login_continue_enabled` / `_signup_continue_screen_hint_enabled` /
`_prime_create_account_password_page_enabled` 原先用 `isinstance(cfg, dict)` 判断生产配置段。
`sms_tool/config.py` 的 `_freeze` 把段冻成 `mappingproxy`（**非 dict**）⇒ 开关在**生产**
读不到（`_existing_login_continue_enabled` 会 fail-closed 到默认 `True`，另两个 fail 到 `False`），
而测试传普通 `dict` 全绿。已改为 `Mapping`（`steps.py:107,157,180`）。

**留档价值**：这是"测试绿、生产关"的一类缺陷。建议在
`docs/current/configuration.md` 或 `sms_tool/config.py` 的 `_freeze` docstring 里
钉一句"配置段的类型判据必须用 `Mapping`，不得用 `dict`"，并加一个针对冻结配置的回归测试。

### 🟢 P2-1 OTP 超时重发（差异，不是缺陷）

SunnyRegister `_verify_email`（`protocol_auth.py:999`）超时后 `_send_email_otp(resend=True)`
再等 60s；本仓 `wait_email_otp`（`registration_otp_stages.py:74`）在
`otp_dispatch_verdict == "stuck"` 时直接止损（`:91`），并在 `dispatched` 形状的超时上用
`_otp_timeout_error`（`:118`）标根因。本仓的止损有 6/6 实测支撑（服务端没派发就别烧 300s），
**不建议**无条件对齐；若要补 `dispatched` 形状的有界重发，需独立开关 + A/B。

### 🟢 P2-2 pulse 的杠杆错配

见 §1.3。`_is_otp_ban_signal` 把 `email_otp_send_stuck` 判为出口级。当前证据指向**事务臂**
而非出口。**建议**：`OTP_DISPATCH_VERDICT_EXCLUDED_CLASSES` 或 pulse 判据里，对
"事务臂为 passwordless 且 `original_screen_hint=signup`"这一**可观测组合**单列一态
（`arm_mismatch`），与 `edge_blocked` 分开处置；换池只对后者生效。这属于 P0-A/P1-A 定案后的
后续项，不要现在改判据。

### 🟢 P2-3 `locale` 未声明 / workspace 选择缺失（待取证）

SunnyRegister signin 固定 `locale=ja-JP`（`protocol_auth.py:645`）；本仓
`_openai_signin_url`（`auth_flow/steps.py:291`）不声明 locale。`workspace` 步骤
（SunnyRegister `:935`）本仓 `auth_flow/` 无（grep 0 命中）。两者都**只是参考实现差异**，
不是已证差距；列入"下一个 signin 形状实验"的候选变量即可。

---

## 5. 验证计划

1. **P0-A（零线上流量）**：给 `scripts/registration_ab.py` 加 `mechanism_seen` 判据 +
   离线单测；用现有 `run_p15_hint.log` 回放，断言它被标成 `manipulation_failed` 而不是
   `underpowered`。再把 P1-5 的作用点改到早返回分支（新开关），重跑预注册 A/B。
2. **P0-B（离线单测 + 线上只观察）**：按 `plan-2026-10-05` 的 S0+S1；注入伪造
   `cf-mitigated: challenge` 响应，断言失败名细分且进入 `host_failures`。
3. **P1-A（离线单测 + 单变量线上对照）**：断言 prime 请求带 `sec-fetch-site`/`sec-fetch-user`；
   线上只跑 prime 头对齐 vs 现状，默认仍关。
4. **P1-B（离线单测）**：`at_probe` 注入 `status_code=403` 且 body 无 token 词表 ⇒
   断言 `_retain_registration_checkpoint` 为真、终态带"边缘拦截"后缀而非重走注册。
5. **P1-C/P1-D（回归）**：`pytest tests/test_sentinel_runner.py
   tests/test_protocol_registration_canary.py tests/test_auth_state.py
   tests/test_registration_ab.py tests/test_user_register_response_contract.py`。
6. **signin 形状实验（最高优先级，P0-A 修好之后）**：单变量 A/B
   `screen_hint=signup`（现状）vs `login_or_signup`（turb），持有密码泳道、出口池、
   `authorize/continue` 行为恒定；主指标 = `email_verification_mode` 是否从
   `passwordless_login` 变到 signup 臂，辅指标 = `registered_per_attempted`。
7. **门禁**：`python scripts/registration_ab.py plan`、`pytest`、`ruff`、
   `docs_consistency_scan.py`、`test_audits_readme_index.py`。

---

## 6. 明确不对齐

| 项 | 参考实现 | 不对齐理由 |
|---|---|---|
| 远程 CDK 提链服务 | `myfanhua/core/extract_link_service.py` | 本仓本地 15 方法、出口可控；远程化引入第三方可用性风险 |
| 外部 `flow_trigger` | `wangshen233/core/flow_trigger.py` | 与本仓"注册成功以 AT 探测为准"的契约无关，且是硬编码外部地址 |
| 纯 Python PoW Sentinel | `myfanhua/core/sentinel.py` | 本仓 Node SDK runner 已被参考项目反向采纳（见 10-01 扫描 §2.3） |
| 无条件 3 次挑战重试 | SunnyRegister `_request_with_sentinel_retry` | 其 `attempts = 3 if strategy == "sentinel_protocol" else 1` 是策略开关；本仓须先有 `edge_challenge_verdict` 三态再决定重试，避免与批处理熔断双重处置 |

---

## 7. 复现方式

```powershell
cd runtime/tmp/refrepos
git clone --depth 1 https://github.com/pxygit/SunnyRegister sunnyregister          # bf5fb64
git clone --depth 1 https://github.com/myfanhua/turb-gpt-free-register myfanhua    # ffcda12
```

近几轮执行的原始日志（工作区内，未入库）：

- `runtime/tmp/reg_in/run.log`（10-06 passwordless）
- `runtime/tmp/reg_in/run_p15_hint.log`（10-07 P1-5 hint arm，0/5）
- `runtime/tmp/reg_in/run_p15_obs_probe.log`（10-07 增强 dump 的第一条完整事务臂证据）
- `runtime/logs/processes/{2256,8852}/backend_stdout.jsonl`（同两轮的结构化日志）
- `runtime/ab/p1-5-signup-continue-screen-hint__{default,hint}.json`（A/B 记录）

`runtime/tmp/` 已被 `.gitignore` 排除；参考仓副本仅供对照，**不得**成为项目依赖
（对齐 `docs/architecture.md` 的依赖方向与 `services/protocol-payment/` 进程边界纪律）。
