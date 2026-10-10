# 落地记录：turb 协议差异 D1–D4 离线落地（2026-10-10）

> 落地来源：`docs/audits/scan-2026-10-10-protocol-registration.md` §D1–D4，来自对
> `myfanhua/turb-gpt-free-register@ffcda12`（2026-09-29，无新提交）协议泳道的逐函数对比。
>
> **全部新开关默认 `false`，本次不改变任何生产行为。**

---

## 1. 为什么做这个

10-07/10-08 的 signin 系列（P1-10/11/12）只测了 `screen_hint` / `prompt` / `locale`
三个字段，全部不控制服务端挂不挂 `passwordless_email_otp_send_pending`。10-10 的函数级
对比发现 turb 在这条握手上还有**四处**本仓从未当变量测过的差异，且 turb 自己在注释里把
其中一处的改动日期标在 **2026-09-14 成功样本**上：

| # | 差异 | turb 侧证据 |
|---|---|---|
| D1 | signin/authorize 上下文五参数（`device_id` / passkey capabilities / `ccaps` / `auth_return_target_category` / `ui_locales`） | `core/chatgpt_auth.py:17,33`："signin 不再主动携带 passkey capabilities；authorize 使用两个 ccaps，并明确返回 ChatGPT 首页" |
| D2 | 密码页落点**致命** | `navigate_create_account_password` 落点错即 `raise` |
| D3 | `email-otp/send` 导航头 + `email-otp/validate` 的 Sentinel | `navigate_email_otp_send`；`config/openai_protocol.py::SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=True` |
| D4 | OTP 后 `external_url` 分支（跳过 `create_account`） | `main.py::run_registration` 的 `direct_oauth_after_otp` |

---

## 2. 落地内容

### 2.1 五个新开关（全部默认 `false`）

| 开关 | 实验 | 行为（开启时） |
| --- | --- | --- |
| `registration.turb_signin_authorize_context` | `p1-13` | signin 查询串只留 `ext-oai-did` / `auth_session_logging_id` / `login_hint`（+ 形状字段）；authorize 去掉 `ext-passkey-client-capabilities`、`ccaps` 改 `login_methods chatgpt_login_finalizer_v1`、加 `auth_return_target_category=chatgpt_home` 与 `ui_locales`（取 `Accept-Language` 首标签，turb 的 `navigator_language` 等价物） |
| `registration.prime_password_page_fatal` | `p1-14` | prime 落点不在 `/create-account/password` ⇒ 结果带 `fatal=True` ⇒ `user_register` 以 `password_page_not_reached:<url>` 中止（turb 的 `raise` 语义） |
| `registration.otp_navigation_headers` | `p1-15` | `email-otp/send` 导航带 `sec-fetch-site: same-origin` + `sec-fetch-user: ?1` |
| `registration.otp_validate_sentinel` | `p1-16` | `email-otp/validate` 前新铸 `authorize_continue` Sentinel 并带 `sentinel_token` + `sentinel_so_token` |
| `registration.otp_external_url_branch` | `p1-17` | validate 后若 `page.type=external_url` 或 `continue_url` 是回调 ⇒ 记 `s.otp_external_url`，`create_account` 跳过 POST（`create_ok=True`，交给 `fetch_auth_session`） |

### 2.2 机制行（全部走 `operator_output.emit`，两通道都在）

```text
Turb signin/authorize context
Password page landing is fatal
OTP navigation headers
OTP validate sentinel
OTP external_url branch
```

🔴 **两条刻意的设计选择**，都是吸取已记录的教训：

1. **机制行无条件落在开关自己的分支内**（D2 的 `Password page landing is fatal` 在 prime
   入口即发，不只在落点错时发；D4 的 `OTP external_url branch` 同理）。只在条件命中时
   发会重犯 **P1-7 的条件式标记缺陷** —— 一批永远落点正确的 run 会被判
   `manipulation_failed` 而不是 `not_judgeable`。
2. **机制行走 `emit` 而不是 `print`**。裸 `print` 只进 `backend_stdout.jsonl`，而 runbook
   的 `collect` 示例传 `sms_tool.log`（2026-10-10 扫描 P1-A′ 的教训）。

### 2.3 一处收敛（P1-C′ 的部分修复）

新增 `sms_tool/registration_protocol_helpers.registration_flag(config, key, default)` ——
**纯函数、`Mapping` 安全、与 `auth_flow.steps._registration_flag` 语义逐字一致**。
`registration_otp_stages` 用同一目录导入它，因此 **不新增任何跨目录 import 边**
（`import_layer_ratchet` 保持 287/31）。OTP 阶段的三个开关因此不是又一份手写解析。

> 仍未收敛：`sentinel_password_bundle`（未知值 fail-open 为真）、
> `edge_challenge_rotate_exit`、`prime_about_you_page`、
> `create_account_disallowed_backoff` —— 属扫描 P1-C′ 的剩余项，需先定宿主。

### 2.4 状态字段

`RegistrationOtp.otp_external_url: str = ""`（`registration_runtime.py`），并在
`RegistrationRuntimeState` 的 `TYPE_CHECKING` 平面视图里同步声明。
`create_account` 用 `getattr(..., "")` + `isinstance(str)` 读取，使只建模部分字段的
测试替身（`SimpleNamespace`）与 `Mock` 属性都保持有效。

---

## 3. 预注册

`scripts/registration_ab.py` 新增 `p1-13` … `p1-17` 五个实验（含 `hold_constant`、
主读数、判定规则）。`read_toggles` 是**从 `EXPERIMENTS` 派生**的（10-10 扫描 P0-A′ 的修复），
所以这五个开关自动进入操纵检查 —— 不需要再手工登记，新实验也不可能静默漏掉。

判定规则（runbook §5）新增五行，其中两条是**不可判定**而不是「无效果」：

- `p1-16`：两臂都没收到码 ⇒ `not_judgeable`（validate 从未被走到）。
- `p1-17`：分支从未触发 ⇒ `not_judgeable`。

---

## 4. 验证证据

| 门禁 | 结果 |
| --- | --- |
| `pytest -q`（全量） | **6237 passed, 8 skipped**, 1176 subtests passed |
| 新增测试 | `tests/test_turb_protocol_diff_toggles.py`（27 例 / 21 subtests） |
| `pyright`（改动文件） | 仅剩既有的 `curl_cffi` import 解析告警（未改动行），0 条新增 |
| `ruff check sms_tool scripts tests` | All checks passed |
| `format_guard` | clean（协议注册泳道 33 文件） |
| `line_ending_guard --all` | no mixed line endings（1040 文件） |
| `sentinel_asset_guard` | 2 pinned asset(s) match |
| `docs_consistency_scan` | passed |
| `import_layer_ratchet` | OK（287 / 31 对 / 11 mutual / 60 minority / 186 delayed）——**未新增边** |
| `delayed_import_ratchet` | OK: 421 ≤ 421 |
| `unused_import_ratchet` | OK: 354 ≤ 355 |
| `bare_print_ratchet` | OK（513，未新增裸 print —— 两条新行走 `emit`） |
| `config_key_ratchet` | OK（`registration=46`, `email_registration=33`） |
| `endpoints_literal_ratchet` | OK（235 / 41 file） |
| `provider_decoupling_ratchet` | OK（238 同名定义：192 delegating / 0 identical / 46 divergent） |
| `mailbox_private_import_ratchet` | OK |

顺带修正：`docs/current/protocol-registration.md` 的 Validation limits 段原本**枚举**了
对照清单并已漂移（漏 p1-9..p1-12 / p0-2b）。改为指向 runbook 的表格作为**唯一清单**，
这类散文枚举漂移不会再发生。

---

## 5. 是否强开：结论是**都不强开**，理由分三类

### 5.1 当前失败形状上不可判定 —— 强开无意义（P1-16 / P1-17）

10-10 的 smoke3 实测 0/3，三个地址全部停在 `email_otp_send_stuck`：**码从未派发，
`email-otp/validate` 从未被走到**。P1-16 改的是 validate 的请求头、P1-17 改的是 validate
**之后**的分支 —— 在码到达之前它们连执行机会都没有。强开只会让「没效果」和「没执行」
混在一起，正是 P0-A 机制门禁存在的理由。

⇒ 这两项**等有码到达之后再跑**（先看 P1-13..P1-15 能否让 `email-otp/send` 真正派发）。

### 5.2 与已关闭的家族同源、证据指向别处 —— 强开是换噪声源（P1-13）

P1-13 是 signin/authorize 上下文的最后一束字段。10-08 的 9 轮实测已经把判据钉死在
「服务端挂不挂 `passwordless_email_otp_send_pending`」，而 `screen_hint`/`prompt`/`locale`
三个字段合起来都**不控制**它。P1-13 换的是同一层（wire 字段），先验成功率低。

但它是**唯一还没测过的一束**，而且 turb 把改动日期标在成功样本上 —— 所以它值得跑
**一次受控对照**（≥30/臂），而不是直接强开。

### 5.3 会主动缩小可行域 —— 强开有明确代价（P1-14 / P1-15）

- **P1-14**：强开意味着「prime 落点不对就整轮中止」。10-06/07 的实测显示 prime 落点
  **5/5 正确**（`_prime_create_account_password_page_enabled` 的 docstring），所以它大概率
  永不触发；但一旦触发，它会把「本来还能继续、可能成功的 run」变成失败。它的价值是
  **诊断分离**（区分「状态没建立」与「状态已建立但服务端仍不派发」），不是提成功率 ——
  诊断开关不该进生产默认值。
- **P1-15**：加两个导航头。P1-9（密码页同款头）尚未线上受控对照，先验收益不确定。

### 5.4 建议的执行顺序

```
P1-13（signin/authorize 上下文，唯一未测的那一束）
   ├─ 若动了挂起键 → 落地并复测
   └─ 若不动 → 客户端 signin/authorize 上下文整体关闭
                ⇒ 离开 wire 字段，转指纹/设备层（oai-did / TLS / 出口 IP 声誉）
P1-15（OTP 导航头）可与 P1-13 不同批并行评估，但同批只跑一个
P1-14（致命落点）只在需要诊断「prime 是否真的建立状态」时开
P1-16 / P1-17 等有码到达
```

**前置条件照旧**：每臂 ≥ 30 attempted（`MIN_ARM_ATTEMPTED`）、`--registration-mode password`、
关 pulse、同出口池同邮箱批次；`p1-16` / `p1-17` 另需先解决邮箱供给。

---

## 6. 相关

- `docs/audits/scan-2026-10-10-protocol-registration.md` §D1–D4、P0-A′、P1-A′、P1-B′、P1-C′
- `docs/current/registration-ab-runbook.md` — P1-13..P1-17 行、机制表、判定规则、停止规则
- `docs/current/protocol-registration.md` — Validation limits（改为指向 runbook）
- 参考仓：`runtime/tmp/refrepos/myfanhua`（浅克隆，`.gitignore` 已排除，不入库）

---

## 7. 线上 pilot（2026-10-10 18:22 / 18:28）：**p1-13 不动**

**供给阻塞**：ReMail iCloud 库存为空 —— 4 次购买尝试（`private_first` ×2、`public_only` ×2）
全部 `422 Insufficient inventory.`（4 条 `failed` 订单已记录，未扣费）；8 月那 8 个未注册
订单的详情接口**无 serviceToken**（ReMail 自己会拒用）。按操作者指定，改用 10 月那 14 个
Remail iCloud。

**臂规模不是 7/7**：smoke3 的 3 个地址仍在 24h `otp_pending_quarantine` 窗口内
（`retry_policy.otp_pending_quarantine_seconds=86400`），被 runner 跳过 ⇒ 实际
**default 5 / turb 6**。

| 臂 | 实跑 | 机制行 | `email_verification_mode` | `original_screen_hint` | `passwordless_email_otp_send_pending` | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| default（开关关） | 5 | **0** | `passwordless_signup` | `signup` | `true` | 0/5 `email_otp_send_stuck` |
| turb（开关开） | 6 | **6** | `passwordless_signup` | `signup` | `true` | 0/6 `email_otp_send_stuck` |

**两臂形状逐字段相同** ⇒ turb 的 signin/authorize 上下文（`device_id` / passkey
capabilities / `ccaps` / `auth_return_target_category` / `ui_locales`）**不改变事务臂，也不
改变挂起键**。与 10-08 的结论一致：客户端 wire 形状不控制
`passwordless_email_otp_send_pending`。

**harness 证据**（`runtime/ab/p1-13-turb-signin-authorize-context__{default,turb}.json`）：
两臂 `toggle_verified=true`、`mechanism_ok=true`（default 无标记 / turb 6 次标记）、
预检 10/10。`compare` 返回 **`underpowered`**（5 vs 6 < 30/臂）—— **不得据此下速率结论**；
本 pilot 可读的是**机制与形状**，不是成功率。

**代价**：14 个地址全部进入 24h 隔离（约至 2026-10-11 18:22），当天不可重跑。

**按 §5.4 执行**：p1-13 不动 ⇒ 客户端 signin/authorize 上下文（P1-10/11/12 + P1-13 的五个
参数）**整体关闭**，下一步应**离开 wire 字段**，转向**指纹/设备层**（`oai-did` / TLS 指纹 /
出口 IP 声誉）。p1-15 同样受邮箱供给阻塞；p1-14 仅诊断时开；p1-16/17 等有码。
