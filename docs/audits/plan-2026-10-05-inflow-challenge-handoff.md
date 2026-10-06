# 流程内 Cloudflare 挑战判据与协议 → 浏览器交棒（立项，2026-10-05）

> 状态：**立项 / 待拍板**。本文件只定义契约、开关、判据与 A/B，**不含实现**。
> 落地前不得改变生产默认行为。
>
> 来源：2026-10-05 对照 `pxygit/SunnyRegister`
> （`python-worker/sunny_core/protocol_auth.py`、`python-worker/sunny_core/sentinel.py`）的
> 协议注册链路深度扫描。
>
> 关联：`scan-2026-10-01-protocol-registration-payment-link.md` 的 **P0-2**
> （该轮只落地了"**预检阶段**的 CF 判别"，并明确写下"流程内换出口需独立开关与独立 A/B"——
> 本文件就是那个 A/B 的立项）。

---

## 0. 结论摘要

| # | 事实 | 证据 |
|---|---|---|
| 1 | 协议注册**流程内**没有任何出口级挑战判据：`403/429` 一律折叠成 `session_circuit_open`（403 默认冷却 900s） | `sms_tool/http_client.py:11`、`:140` |
| 2 | `classify_edge_response` 已存在且可用，但**只**被三个非注册流程消费：预检、查优惠、UPI 提链 | `sms_tool/registration_preflight.py:213`、`sms_tool/accounts/promotion_batch.py:482`、`sms_tool/upi_link/india_exit.py:81` |
| 3 | 预检的 CF 判据（P0-2）发生在**领邮箱之前**，它只能拦"一开始就被挑战的出口" | `sms_tool/registration_preflight.py:139` 由 `sms_tool/commands/registration.py:101` 在领邮箱前调用 |
| 4 | SunnyRegister 有完整的"协议被挑战 → 交棒给浏览器 → 回到协议"链路，本仓没有 | `sunny_core/protocol_auth.py:393`、`:619`、`sunny_core/sentinel.py:114` |
| 5 | `auth_flow/` 里出现的 "challenge" **全是 OTP 事务挑战**（`login_challenge` / `passwordless_login`），与 Cloudflare 无关 | `sms_tool/auth_flow/otp.py:10`、`:84` |

**待验假设 H1**：一个在预检通过之后、命中 per-request 挑战的账号，其失败**可以通过
"判别 + 换出口重试"挽回**，而不是必须烧掉已消耗的邮箱与 OTP。

**待验假设 H2**：当换出口仍无法通过时，把协议会话**交棒**给已有的浏览器驱动
（导出 cookie jar + 落点 URL）能让该账号完成注册，且**不污染**协议泳道的失败词汇。

**代价的不对称性（这是本项的核心动机）**：预检通过后的挑战发生**在邮箱和 OTP 已经消耗之后**。
一个 false negative（把挑战当成普通 4xx 熔断掉）的代价是**永久损失一个邮箱槽**；
而一个 false positive（把普通 4xx 当挑战去换出口）的代价只是**多一次请求**。
⇒ 判据应当**偏向召回**，且必须在错误名里保留不确定性，而不是猜一个确定结论。

---

## 1. 现状（本仓，逐条带证据）

### 1.1 流程内唯一的 403/429 反应是熔断

`sms_tool/http_client.py:140`：

```python
if status_code in {403, 429}:
    retry_after = _retry_after_seconds(response, default=900.0 if status_code == 403 else 300.0)
    state = _session_circuit(session)
    state.update({"blocked_until": time.time() + retry_after, ...})
```

它**不区分**"出口被 Cloudflare 挑战"与"请求本身被拒"，也不换出口。随后
`sms_tool/http_client.py:56` 的 `_raise_if_circuit_open` 让同一个 session 上的后续请求
直接抛 `SessionCircuitOpen`（`sms_tool/http_client.py:11`）。

### 1.2 判别器已经存在，只是没接到注册流程里

`sms_tool/proxy_edge_probe.py:134` 的 `classify_edge_response(status, body, headers)`
能识别 `cf-mitigated: challenge` / `cf-chl` / `Just a moment`（见该模块 docstring `:27`）。
它的实际消费者只有三处，**没有一处是协议注册**：

| 消费者 | 位置 | 时机 |
|---|---|---|
| 注册预检 | `sms_tool/registration_preflight.py:213` | **领邮箱之前** |
| 查优惠批量 | `sms_tool/accounts/promotion_batch.py:482` | 账号已存在、非注册 |
| UPI 提链 | `sms_tool/upi_link/india_exit.py:81` | 支付链路 |

`sms_tool/registration_preflight.py:39` 的 `CLOUDFLARE_CHALLENGE_MARKER` 让调用方
（`sms_tool/commands/registration.py`）能在候选路由里整体跳过该出口——这是 2026-10-01
落地的 P0-2 第 1 步，**只覆盖预检时点**。

### 1.3 🔴 不要与 OTP 事务挑战混淆

`grep challenge sms_tool/auth_flow/*.py` 会命中很多，但那些**全部**是 OTP 事务概念：
`_otp_challenge_established`（`sms_tool/auth_flow/otp.py:10`）判的是
`email-otp/send` 的响应里有没有真正的挑战载荷（`login_challenge` vs `passwordless_login`，
见 `:84` 起的长注释）。它**与 Cloudflare 无关**。

⇒ 本项新增的任何 CF 判据都必须用**独立命名**（建议 `edge_challenge_*`），
不要复用 `challenge` 这个词。这个仓已经因为"同一个词指两件事"付过学费
（见 `docs/audits/scan-2026-09-16-protocol-registration-optimization.md` 坑 #3、
以及 `pp_link_helpers` 里 `code_not_delivered` 改名 `mailbox_side_no_code` 的先例）。

### 1.4 缺口的确切形状

```
预检通过 ──► 领邮箱 ──► signin/authorize ──► [CF 挑战命中] ──► session_circuit_open(900s)
                            ▲                                        │
                            └────────── 邮箱与 OTP 已消耗，不可回收 ◄──┘
```

---

## 2. 参照实现：SunnyRegister

### 2.1 三个组件

| 组件 | 位置 | 职责 |
|---|---|---|
| `SentinelBrowserRuntime` | `python-worker/sunny_core/sentinel.py:114` | Playwright 真浏览器执行 sdk.js，处理 `turnstile.required` 与 `so` 观察者 token（`sdk.___n` / `sdk.__Nt` / `sdk.__jt`），作为**并列的 proof provider** |
| `SentinelNodeRuntime` | `python-worker/sunny_core/sentinel.py:349` | 本仓也有的 Node VM 路径 |
| `ProtocolChallengeRequired` | `python-worker/sunny_core/protocol_auth.py:77` | 协议泳道"我过不去"的显式信号 |

### 2.2 三处关键行为

1. **挑战判别**（`python-worker/sunny_core/protocol_auth.py:609`）：
   `status in {403, 429}` 或 body 含 `challenge` / `turnstile` / `captcha` / `sentinel`，
   **且先排除账号已废**（`_is_account_deactivated_payload`）。
2. **同 flow 重试**（`:619` `_request_with_sentinel_retry`）：最多 3 次，
   每次 `_reset_sentinel_runtime()` 后重发；耗尽则抛 `ProtocolChallengeRequired`。
   ⚠️ 注意它 `attempts = 3 if self.challenge_strategy == "sentinel_protocol" else 1`——
   **这是一个策略开关，不是无条件重试**。
3. **交棒**（`:393` `_browser_handoff_snapshot`）：导出
   - `resume_by_flow` 映射：`authorize_continue` → `log-in`/`create-account`、
     `username_password_create` → `create-account/password`、`password_verify` → 当前页、
     `oauth_create_account` → `about-you`
   - 完整 cookie jar（`name`/`value`/`path`/`secure`/`domain`），
   - `protocol_email_verified`、`generated_chatgpt_password`、`auth_action`、
     `protocol_traffic`
   交给浏览器闯关，然后**回到协议**继续。

### 2.3 为什么这不是一份可以直接搬的设计

| 差异 | 说明 |
|---|---|
| **浏览器进入注册热路径** | 本仓协议泳道的既有取向是"无浏览器"（`registration.driver = protocol`，与 `browser_flow/` 明确分离）。Sunny 是"协议 + 浏览器协同"（Go 任务模型里 protocol 与 browser 是同级执行方式）。直接搬会把浏览器依赖引进当前最干净的一条链路 |
| **它的挑战判据把 403/429 与 body 关键词混在一起** | 本仓 `429` 有独立语义（`failure_registry` 的 `rate_limit` 类 + 批处理熔断器 + `OTP_DISPATCH_VERDICT_EXCLUDED_CLASSES`，见 `sms_tool/failure_registry.py`）。照搬会让 429 走进挑战路径，与熔断器**双重处置**——这正是 09-16 审计 P0-2 修掉的那类叠加 |
| **它没有"同一次 checkout/注册内身份一致"的约束** | 本仓要求一次 run 内地址/身份一致（同日 G4 的 `reserve_billing_variant` 就是同一个道理）。交棒必须保证**回程仍在同一账号的同一事务里** |

⇒ **结论：借机制，不搬实现。** 分阶段，且每一阶段的默认值都把行为保持在今天。

---

## 3. 设计

### 3.1 配置键（全部默认关/只读）

| 键 | 默认 | 作用 |
|---|---|---|
| `registration.edge_challenge_discrimination` | `true` | **只读判据**：把流程内的 `403/429` 细分成 `...:edge_challenge` / `...:http_429` / `...:http_403`，写进错误串与结果。**不改变任何控制流** |
| `registration.edge_challenge_rotate_exit` | `false` | 命中挑战时，在同池内换出口重试**一次**（对齐 Sunny 的 `sentinel_protocol` 策略，但作用在出口而不是 proof provider） |
| `registration.edge_challenge_browser_handoff` | `false` | 换出口仍失败时，交棒给浏览器驱动 |
| `registration.edge_challenge_handoff_resume` | `false` | 交棒后是否**回到协议**（false = 交给浏览器走完，true = 浏览器只负责闯关后回程） |

`registration.edge_challenge_discrimination` 默认 **true** 的理由：它只改错误名，不改行为，
而错误名是**离线判据的唯一数据源**——09-16 审计坑 #2（"降噪会吃掉信号"）与 P0-1
（判据必须落在代码里、日志只做旁证）都指向同一结论：**先把观测放出去，再谈控制流**。

### 3.2 判据（纯函数，建议落 `sms_tool/auth_state.py`）

```python
def edge_challenge_verdict(response, *, body_limit: int = 4096) -> str:
    """Return one of "challenge" / "not_challenge" / "unknown"."""
```

契约（三条都必须成立）：

1. **只在 `403/429` 上回答 `challenge` 或 `not_challenge`；其它状态码答 `unknown`。**
   不做"body 里有 captcha 字样就算"的猜测——`auth_flow/otp.py` 的挑战载荷就含 `challenge`
   字样（见 §1.3）。
2. **复用 `proxy_edge_probe.classify_edge_response` 的判据**，不新写一套 host/header 匹配。
   本仓已经为"同一个判据两份实现"付过代价（`docs/audits/scan-2026-09-22-*` 的 geo 三份实现）。
3. **先排除账号已废**（对齐 Sunny `:609` 的顺序，也与本仓
   `accounts/account_terminal.py` 的终态词汇一致）：一个 `403 account_deactivated`
   不是挑战，换出口也不会变。

⚠️ **`429` 必须继续归熔断器**（`failure_registry` 的 `rate_limit` 类）。
`edge_challenge_verdict` 可以在 429 上回答 `challenge`（用于**观测**），
但 §3.3 的控制流**不得**在 `failure_class == "rate_limit"` 时触发换出口——
理由与 `OTP_DISPATCH_VERDICT_EXCLUDED_CLASSES` 完全相同：一件事只能有一个处置者。

### 3.3 控制流（仅在开关打开时）

```
auth_flow 的 authorize / continue / otp 步骤
  ├─ 响应 403/429
  │    ├─ edge_challenge_verdict == "challenge"
  │    │     ├─ [discrimination] 错误名加 :edge_challenge            ← 默认开
  │    │     ├─ [rotate_exit]   同池换出口，重试一次                  ← 默认关
  │    │     │     ├─ 成功 → 继续（并记 rotated_once=true）
  │    │     │     └─ 仍挑战 → 下一步
  │    │     └─ [handoff]       交棒（§3.4）                          ← 默认关
  │    └─ 否则 → 保持今天的行为（session_circuit_open）
  └─ 其它状态码 → 不变
```

**一次**上限是刻意的：Sunny 用 3 次，但那是"重置 proof provider"而不是"换出口"。
换出口每次都会换掉整个会话出口，代价与收益都比重置 proof 高。上限 1 让
"这到底有没有用"在 A/B 里可归因。

### 3.4 交棒契约（`edge_challenge_browser_handoff` 打开时才存在）

交棒**只导出**，不实现浏览器逻辑——浏览器侧复用 `registration_drivers/` 已有驱动。

| 字段 | 含义 | 与 Sunny 的差异 |
|---|---|---|
| `edge_challenge_handoff: true` | 显式标记，便于离线统计与"不许猜" | 同名概念（`protocol_browser_handoff`） |
| `handoff_resume_url` | 按 flow 映射的落点（复用 `auth_flow/steps.py` 的落点谓词，**不新写路径判断**） | 对齐，但**必须**由 `steps.py` 的谓词 owner 生成 |
| `handoff_storage_state` | `{"cookies": [...], "origins": []}`，cookie 字段 `name`/`value`/`path`/`secure`/`domain` | 对齐 |
| `handoff_flow` | 触发挑战的 flow（`authorize_continue` / `oauth_create_account` / …） | 对齐 |
| `handoff_email_verified` | 该事务是否已过邮箱验证（决定回程能否跳过 OTP） | 对齐 |
| `handoff_attempt` | 本 run 内第几次交棒（上限 1） | **新增**（Sunny 无上限） |

🔴 **`handoff_storage_state` 是凭据，不是诊断。** 它必须：
- 走 `sms_tool/sanitizer.py` 的脱敏路径**之外**的显式白名单（cookie 值不能被 `[REDACTED]`
  否则交棒失效），同时**绝不**进 `runtime/logs/`、不进 `docs/`、不进 release payload；
- 与 `pp_link_helpers._redact_sensitive_values` 的持久化纪律同级：只允许内存内传递，
  任何落盘都要过 `payment_history_metadata` 那种"只留 presence"的转换。

这一条是本项**最需要单独拍板**的部分：它引入了一条新的凭据传递路径。

### 3.5 与 `session_circuit_open` 的关系（谁赢）

这是本设计最容易出错的地方，必须写死：

| 情形 | 熔断（今天） | 挑战路径（开关打开后） |
|---|---|---|
| `403` + 判据 `challenge` | 熔断 900s | **挑战路径先于熔断**：先尝试换出口/交棒；都失败才写熔断 |
| `403` + 判据 `not_challenge` | 熔断 900s | 不变 |
| `429`（任意） | 熔断 300s | **不变**（见 §3.2） |
| `503` / 网络异常 | 不熔断（走 transport 重试） | 不变 |

🔴 三条不变式：

1. **熔断状态只能被"挑战路径已经失败"之后写入**，否则 `session_circuit_open` 会
   在换出口之前就把同一个 session 锁死（`sms_tool/http_client.py:56` 在**每次**请求前检查）。
2. **换出口后必须 `clear_session_circuit(session)`**，否则新出口被旧熔断挡住
   （该函数已存在：`sms_tool/http_client.py:51`）。
3. **挑战路径不新增失败类别**。它复用既有词汇：
   换出口仍失败 ⇒ 仍是 `session_circuit_open`，只是错误串多了 `:edge_challenge` 后缀；
   这与 `_otp_timeout_error` 的"加根因后缀而不是造新码"是同一个模式。

---

## 4. A/B 设计（预注册）

按 `docs/current/registration-ab-runbook.md` 的纪律。逐项填写，**跑之前**先注册。

**experiment id**：`p0-2b-inflow-challenge-handoff`（追加进 `scripts/registration_ab.py:54`
的 `EXPERIMENTS`，不改动既有三个 experiment）。

| 项 | 内容 |
|---|---|
| **H1** | 预检通过后命中的挑战，其失败可由"判别 + 换出口一次"挽回（`rotate_exit` 臂的 `funnel.registered_per_attempted` 高于 `observe` 臂） |
| **H2** | 换出口后仍挑战的账号，交棒能让其完成注册（`handoff` 臂高于 `rotate_exit` 臂），且不引入新失败类别 |
| **arms** | `observe`（`discrimination=true, rotate_exit=false, handoff=false`）· `rotate`（`discrimination=true, rotate_exit=true`）· `handoff`（全开） |
| **hold_constant** | 同一邮箱批次与 provider；同一出口池（同 `proxy_seeds`/lanes）；同一 driver/并发/阶段超时；同一时段（Cloudflare 负载有日变化）；**同一 `registration_mode`** |
| **metrics** | `preflight.cloudflare_rate`（操作检查：三臂应无显著差异，否则出口池变了）· `registration.edge_challenge_count`（每臂实际命中数）· `funnel.registered_per_attempted` · `failures_by_class`（新增类别数**必须为 0**）· `otp.mailboxes_consumed_per_registered`（**本项真正的收益指标**） |
| **manipulation check** | `rotate` 臂必须证明出口**真的换了**：比对同一 run 的 `proxy_audit` 出口国/池索引前后值。换不掉（池只有 1 槽）的 run 必须单独计数，否则稀释效应会让结论偏向"无效" |
| **decision** | 若 `rotate` 的 `registered_per_attempted` 高于 `observe` 且 `mailboxes_consumed_per_registered` 更低、且 `failures_by_class` 无新增类别 ⇒ 采纳 `rotate_exit=true`；`handoff` 另按 H2 单独判定，**不与 rotate 合并拍板** |
| **停止规则** | 任一臂出现**新的终态失败类别**、或 `handoff` 臂把可注册地址写进死路账本（`registration_retry_guard`）⇒ **立即停止全部三臂**并回到 `observe` |

### 4.1 必须预注册的"无效结果"判据

本仓已经踩过"只会说 FAIL 的判定器不是判定器"的坑。所以先把**什么算无效**写死：

- 三臂的 `edge_challenge_count == 0` ⇒ 观测窗口内没发生挑战，**本项不可判定**
  （不是"无效果"）。需要换时段/出口池重跑，或延长窗口。
- `rotate` 臂的 `proxy_audit` 显示出口未变（单槽池）⇒ 该臂是 `observe` 的复制，
  **本项不可判定**。
- 样本量低于 runbook 规定的最小值 ⇒ **不可判定**。

---

## 5. 明确不做

| 项 | 理由 |
|---|---|
| 把 `SentinelBrowserRuntime` 作为**常规** proof provider | 本仓 Node SDK runner 已被参照项目反过来采纳为上游（见 10-01 审计 §2.3）。常规路径上叠加浏览器会同时增加依赖与失败面，收益只在"Node 也被挑战"的窄场景 |
| 照搬 Sunny 的 `_is_challenge_response`（403/429 + body 关键词） | 会与 `rate_limit` 的既有处置者冲突（§2.3）。必须用 §3.2 的 `unknown` 三态，而不是二值猜测 |
| 交棒重试 3 次 | Sunny 的 3 次是"重置 proof provider"；换出口/交棒的代价高一个数量级。上限 1 让归因成立 |
| 交棒后**常驻**浏览器 | 交棒是"这一条事务的旁路"，不是切换执行方式。常驻等于把协议泳道改名为浏览器泳道 |
| 新增 `edge_challenge` 失败**类别** | 违反"一件事一个处置者"。用后缀，与 `_otp_timeout_error` / `is_passwordless_signup_mismatch` 同构 |
| 顺手改 `registration_preflight` 的 CF 路径 | P0-2 第 1 步已落地且行为正确；本项只加**流程内**的判据，不动预检 |

---

## 6. 落地顺序

| 阶段 | 内容 | 默认 | 风险 | 门禁 |
|---|---|---|---|---|
| **S0** | 只读判据：`edge_challenge_verdict` + `registration.edge_challenge_discrimination` + 错误串后缀 + 离线单测（构造 `cf-mitigated: challenge` 响应） | 开 | 零（只改错误名） | 现有 `test_registration_*` 全绿；错误类别数不变 |
| **S1** | `auth_state` 起判据与 §3.5 的不变式单测（熔断写入顺序、换出口后清熔断） | — | 低 | 新增用例必须覆盖三条不变式各一条变异 |
| **S2** | `rotate_exit`：同池换出口一次 + `proxy_audit` 记录前后出口 | **关** | 中（改控制流） | 同池单槽必须短路为"不换"并如实报告 |
| **S3** | A/B 跑 §4，按预注册判定规则拍板 | — | — | 判定规则见 §4 |
| **S4** | `handoff` 契约 + 凭据路径审查（§3.4） | **关** | 高（新凭据路径） | 需单独拍板；先出凭据流图再写代码 |

**S0 与 S1 可以立即做**（零行为变更 + 单测）；**S2 之后必须先跑 A/B**。

---

## 7. 未决问题（拍板前必须回答）

1. **交棒的凭据路径**（§3.4）走哪条审计？是否允许落 `runtime/`？如果允许，
   包不包在 `sensitive_policy.json` 的脱敏范围内（包了就没法交棒，不包就是新裸露面）。
2. **交棒归谁执行**：复用 `registration_drivers/` 的哪个 driver（`browser_flow` 还是
   `camoufox`/`cloak`）？协议泳道当前 `driver=protocol`，交棒需要一个**显式的第二 driver 绑定**，
   否则会与 `normalize_registration_driver` 的既有语义打架。
3. **回程语义**（`edge_challenge_handoff_resume`）：浏览器闯关后回到协议，
   还是交给浏览器走完？两者的失败词汇归属不同——前者仍算协议 run，
   后者会产出 `browser_*` 前缀的错误（`registration_drivers/browser_flow/session.py`
   的 `_browser_failure_class`），需要在 `funnel` 里可分辨。
4. **交棒失败是否计入死路账本**：按 §3.2 的"排除账号已废"之后，交棒失败**不应**记账
   （地址未消耗），但需要一个明确的判据，避免重演
   `auth_session_recovery_expired` 那种"每批重烧一个邮箱槽"。
5. **`edge_challenge_verdict == "unknown"` 时的行为**：按本项的不对称性，
   `unknown` 应该**偏向**尝试换出口一次（多一次请求的代价）还是保持今天的熔断？
   建议偏向前者，但这会让 §4 的 `manipulation check` 更复杂。

---

## 8. 证据与复现

参照仓浅克隆（**不入库**，`.gitignore` 已排除）：

```powershell
cd runtime/tmp/refrepos
git clone --depth 1 https://github.com/pxygit/SunnyRegister pxygit-sunnyregister
```

本文件引用的参照文件：

| 路径 | 行 | 内容 |
|---|---|---|
| `python-worker/sunny_core/protocol_auth.py` | 77 | `ProtocolChallengeRequired` |
| 同上 | 393 | `_browser_handoff_snapshot` |
| 同上 | 609 | `_is_challenge_response` |
| 同上 | 619 | `_request_with_sentinel_retry` |
| `python-worker/sunny_core/sentinel.py` | 114 | `SentinelBrowserRuntime` |
| 同上 | 349 | `SentinelNodeRuntime` |

本仓引用均为当前工作树行号，可用
`python -c "import sms_tool.http_client as m; print(m.__file__)"` 核对路径。
