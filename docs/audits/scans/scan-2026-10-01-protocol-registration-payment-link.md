# 协议注册 / 支付链接提取模块扫描（2026-10-01）

> 范围：协议注册链路（`sms_tool/registration_handlers.py`、`sms_tool/auth_flow/`、
> `sms_tool/registration_preflight.py`、`sms_tool/http_client.py`、`sms_tool/sentinel/`）
> 与支付链接提取链路（`sms_tool/pay_link/`、`sms_tool/paypal_link/`、`sms_tool/upi_link/`、
> `services/protocol-payment/`、`sms_tool/payment_capability.py`、
> `sms_tool/accounts/account_promotion.py`）。
>
> 参考项目（浅克隆到 `runtime/tmp/refrepos/`，**仅供参考，不入库**）：
>
> | 参考 | 提交 | 日期 | 说明 |
> |---|---|---|---|
> | `myfanhua/turb-gpt-free-register` | `ffcda12` | 2026-09-29 | 多驱动注册（protocol/roxy/cloak/browser_use/skyvern）+ 远程提链服务 |
> | `wangshen233/turb-gpt-free-register-oss-20260928` | `7df094e` | 2026-09-28 | 上者的 OSS 精简版；含 `promo_detector/` 与内置的 `vendor/gpt-register-tool/协议` |
>
> 本报告记录**对照取证 + 落地结果**。所有判定均给出本仓或参考仓的文件行号；
> 落地项见 §7。

---

## 7. 落地结果（2026-10-01）

按 P0-1 → P2 顺序执行，全部落地并通过门禁：

| 项 | 变更 | 文件 | 测试 |
|---|---|---|---|
| P0-1 | 预检登录入口改为浏览器页 `chatgpt.com/auth/login?next=%2F`，`registration.preflight_login_page=legacy` 可回退 | `sms_tool/registration_preflight.py` | `tests/test_registration_preflight_login_page.py` |
| P0-2 | 预检失败区分 Cloudflare 挑战（`CLOUDFLARE_CHALLENGE_MARKER`），命令层按出口整体跳过 | `sms_tool/registration_preflight.py`、`sms_tool/commands/registration.py` | 同上 + `tests/test_registration_preflight_budget.py` |
| P1-1 | 新增只读 Plus 试用券探测并合入查优惠（`coupon_probe`，批路径默认开） | `sms_tool/accounts/account_promotion.py`、`accounts/promotion_batch.py` | `tests/test_account_promotion_coupon.py` |
| P1-2 | 新增 `payable_amount` / `discount_breakdown` / `amount_observations` 纯解析，`offer_state` 增 `discounted_zero_due` 子态 | `sms_tool/checkout_contract.py`、`sms_tool/payment_capability.py` | `tests/test_checkout_amount_evidence.py` |
| P1-3 | `issue_sentinel_bundle` 改为**同一 `p` 跨 flow**；注册密码泳道可选预热（`registration.sentinel_password_bundle`，默认关） | `sms_tool/sentinel/client.py`、`sms_tool/registration_handlers.py` | `tests/test_sentinel_password_bundle.py` |
| P2 | 账单模板新增 35 国，未知国家改用**请求国占位模板**（不再回落德国） | `sms_tool/pp_link_helpers.py` | `tests/test_paypal_pp_link_helpers.py` |

**仍未验证的假设（必须线上受控对照）**：P0-1 的端点替换、P0-2 的流程内换出口、
P1-3 的密码页 bundle 形态。落地只提供开关与能力，默认值见各项；对照纪律见
`docs/current/protocol-registration.md` 的「Validation limits」。

---

## 0. 结论摘要

**本仓在两条链路上整体领先参考项目**，扫描找到 4 个可落地优化 + 2 个次要项。
其中唯一有"机器特征"风险的是 P0-1：预检直接 GET 了真浏览器从不单独访问的端点。

| # | 结论 | 方向 | 证据 |
|---|---|---|---|
| ✅ | 支付链接提取：本仓本地实现 **15 个方法**（`blik/ideal/kakao/momo/pix/twint/direct_card/paypal/upi/gopay/grabpay/gcash/qris/bizum/naver_pay`），参考项目只有 **4 个方法**且**外包给远程 CDK 服务** | 本仓领先 | `payment_catalog.PAYMENT_METHODS`；`myfanhua/core/extract_link_service.py:50` |
| ✅ | Sentinel：参考项目**反向复用本仓协议组件**作为其 Sentinel 引擎 | 本仓领先 | `wangshen233/core/sentinel_protocol.py` docstring；`vendor/gpt-register-tool/协议/` |
| 🔴 | **P0-1** 预检 GET `{auth_base}/log-in` 是纯机器特征（真浏览器不单独访问） | 改端点 + 加 CF 判据 | `sms_tool/registration_preflight.py:122`；`wangshen233/core/openai_auth.py:154` |
| 🔴 | **P0-2** 注册流程内无 Cloudflare challenge 检测与换出口；403 只进会话熔断 | 对齐 `follow_authorize` | `sms_tool/http_client.py:140`；`myfanhua/core/openai_auth.py:263` |
| 🟡 | **P1-1** 缺只读 Plus 试用券探测（`promo_campaign/check_coupon`） | 新增薄探测 | `vendor/.../plus_trial_checker.py:20,134` |
| 🟡 | **P1-2** 金额/优惠解析缺 `discount_breakdown` + `amount_observations` 交叉一致 | 补纯解析 | `wangshen233/core/promo_detector/oaics.py:397,413` |
| 🟡 | **P1-3** 密码页 Sentinel 未复现"三个 flow 共享同一 `p`"的浏览器形态 | 对齐 flow bundle | `myfanhua/core/openai_auth.py:413` |
| 🟢 | **P2-1** 账单模板国家覆盖 15 国（参考约 200 国） | 扩表 | `sms_tool/pp_link_helpers.py`；`promo_detector/billing.py:_build_billing_templates` |
| ❌ | **不移植**：远程 CDK 提链服务、外部 `flow_trigger` 服务 | 保持本地/自持 | 见 §4 |

---

## 1. 本仓模块现状

### 1.1 协议注册（email / AT 协议泳道）

| 层 | 模块 | 契约 |
|---|---|---|
| 步骤函数（signin/authorize/continue/OTP/TOTP） | `sms_tool/auth_flow/`（`steps`/`signup`/`login`/`otp`/`totp`/`password_step`/`sentinel_flow`/`deps`） | `docs/current/protocol-registration.md` |
| 阶段编排 + 运行时状态 | `sms_tool/registration_handlers.py` | 阶段序：auth_flow → user_register → OTP send/wait/validate → create_account → session → AT probe → TOTP |
| 预检 | `sms_tool/registration_preflight.py` | 4 端点 + 代理 scheme 纠正 |
| HTTP 重试 / 熔断 | `sms_tool/http_client.py` | 403/429 → `session_circuit_open` |
| Sentinel | `sms_tool/sentinel/`（`bundle`/`client`/`runner`） | Node SDK runner + `chatgpt_checkout` 缓存 |
| 失败分类 / 死路账本 | `failure_registry.py`、`registration_retry_guard.py`、`accounts/account_terminal.py` | |

**已具备、与参考项目同级或更强**：
`registration_network_preflight`（`registration_preflight.py:105`）、密码优先泳道
（`registration_handlers._password_lane_active`）、邮箱码重试一次
（`registration_otp_stages.validate_email_otp`）、OTP 超时根因后缀
（`_otp_timeout_error`）、`about-you` 误路由识别（`auth_flow/steps._is_about_you_step`）、
账号已停用判定（`account_terminal`）。

### 1.2 支付链接提取

| 层 | 模块 | 方法 |
|---|---|---|
| 目录 / 路由 / 状态机 | `payment_catalog.py`、`payment_routing.py`、`payment_executor.py`、`pay_link/`（registry/core/base/normalize/adapters） | 15 方法统一 `PaymentResult` 契约 |
| 原生方法 | `gen_pp_link.py`+`paypal_extract.py`+`paypal_link/`（PayPal）、`upi_link/`（UPI）、`wallet_provider/transport`+`gcash_provider/transport` | |
| 子进程提取器 | `services/protocol-payment/{pix,ideal,kakao,blik,twint,momo,direct_card}` | `protocol_payment.v1` 终态回报 |
| 能力探测 | `payment_capability.py`（`cs_*`/`oaics_*` 只读）、`checkout_contract.py`（`StripeCapabilityEvidence`） | 副作用受限 |
| 出口门禁 | `payment_egress.py` | 强出口国别校验 |

**已具备、强于参考项目**：本地 15 方法提取（参考 4 方法全靠远程服务）、
`payment_egress` 强出口门禁、`PaymentOperationStore` 幂等/恢复、
`unknown` 终态 `requires_reconciliation` 不自动重试。

---

## 2. 参考项目对照

### 2.1 turb 协议注册（`core/openai_auth.py`）

| 参考能力 | 位置 | 本仓对应 | 判定 |
|---|---|---|---|
| `network_preflight()` 三边缘预检 | `myfanhua:236` | `registration_network_preflight` | 同级（本仓多代理 scheme 纠正） |
| **CF challenge 检测 + 换上游重试** | `wangshen233:149,154`；`myfanhua:263 follow_authorize` | 无（见 P0-2） | **差距** |
| 预检打 `chatgpt.com/auth/login?next=%2F` | `wangshen233:154` | 打 `{auth_base}/log-in`（P0-1） | **差距** |
| `request_password_sentinel_bundle`（同一 `p` 三 flow） | `myfanhua:413` | `_issue_sentinel` 单 flow | 次要（P1-3） |
| `navigate_create_account_password` / `navigate_about_you`（先导航建状态） | `myfanhua:564,695` | 直接 POST（`referer=/about-you`）；`codex_oauth` 另有 about-you 处理 | 次要（P2-2） |
| `detect_account_unusable_text` + `_ACCOUNT_DEAD_CODES` | `myfanhua:80` | `failure_registry`/`account_terminal` | 同级 |
| 代理网络重试 / 可重试 authorize 分类 | `myfanhua:210` | `http_client.request_with_retry` | 同级 |

### 2.2 turb OSS 支付/优惠（`core/promo_detector/`、`plus_trial_checker.py`）

| 参考能力 | 位置 | 本仓对应 | 判定 |
|---|---|---|---|
| 只读 Plus 试用券探测 `promo_campaign/check_coupon` | `plus_trial_checker.py:20,134` | 无 | **差距**（P1-1） |
| `payable_amount` 权威路径序 | `oaics.py:387` | `checkout_contract._extract_amount_minor:341` | 同级 |
| `discount_breakdown(subtotal,discount,total)` | `oaics.py:397` | 无 | **差距**（P1-2） |
| `amount_observations` 多路径一致 | `oaics.py:413` | 无 | **差距**（P1-2） |
| `payment_method_types` / `custom_payment_methods` | `oaics.py:307,326` | `StripeCapabilityEvidence` | 同级 |
| `_promo_is_cf_challenge` + `_rotate_promo_proxy` | `promo_check.py:161,169` | `payment_egress` + 路由换池 | 同级 |
| `_build_billing_templates`（约 200 国） | `promo_detector/billing.py` | `pp_link_helpers.BILLING_DATA`（15 国） | 次要（P2-1） |

### 2.3 关键事实：参考项目反向复用本仓组件

`wangshen233/core/sentinel_protocol.py` 的模块 docstring 明写：

> "主人手上有一份**已经改好并验证过**的协议注册机（GPT-Register-Tool 的 `协议/`），
> 它直接跑 OpenAI 的**真 sdk.js**，两趟出 token。本模块就是那个路径的适配层。"

该仓 `vendor/gpt-register-tool/协议/` 即本仓协议组件的较早副本。**结论**：Sentinel
路径本仓是上游，不需要向参考项目学习；参考项目里"t 长度偏短"的旧告警已被其自行证伪
（`sentinel_protocol.py` 注释）。

---

## 3. 优化项

### 🔴 P0-1 预检不要 GET `auth.openai.com/log-in`

**现状**：`sms_tool/registration_preflight.py:120-127` 的 4 个检查里，
`auth-login` 直接请求 `{auth_base}/log-in`，`sentinel-frame` 的 referer 也是它。

**参考证据**：`wangshen233/core/openai_auth.py:154 ensure_auth_ip_not_challenged`
的注释给出 HAR 事实——

> "真浏览器从不 GET auth.openai.com/log-in（09-11 抓包 219 条 ABSENT；09-13 真机复抓
> 也没有）。这个端点只在流程内被 authorize 重定向自然触达，脱离流程单独打是纯机器特征。"

因此参考项目改打 `https://chatgpt.com/auth/login?next=%2F`。

**风险**：预检发生在**领取付费邮箱之前**（`commands/registration.py:100
preflight_registration_before_mailbox`），本意是省邮箱；但用一个只有协议端会打的端点
做预检，可能反而把出口送进 CF challenge，形成"预检越勤、越容易被拦"的自伤。

**建议**：
1. 把 `auth-login` 检查换成 `https://chatgpt.com/auth/login?next=%2F`；
   `sentinel-frame` 的 referer 同步改。
2. `chatgpt-login`（`{chat_base}/login`）同样需要复核：参考项目证据只覆盖
   `auth.openai.com/log-in`，但 `chatgpt.com/login` 也不在 09-11 的 219 条里。
3. **必须做受控线上对照**（参见 `docs/current/protocol-registration.md`
   "Validation limits"）：同一批邮箱、同一组出口，A/B 预检端点，比较
   `registration_preflight_failed:*` 与首轮 `authorize` 落点分布。不得仅凭注释改。

### 🔴 P0-2 注册流程内 CF challenge 检测 + 换出口

**现状**：`sms_tool/http_client.py:140-146` 把 `403/429` 一律转成
`session_circuit_open`（403 默认冷却 900s），既不识别 CF challenge，也不换出口。
`proxy_edge_probe.py` 能判定 `blocked_by_cloudflare`，但只在
`accounts/promotion_batch.py` 使用，注册链路未接线。

**参考做法**：`myfanhua/core/openai_auth.py:263 follow_authorize` 对
`is_cloudflare_challenge(resp)` 先 `session.rotate_bridge_upstream()` 再重试，
把"被 CF 拦"与普通 4xx 分开。

**影响**：预检通过后，若 authorize 命中 per-request challenge，本仓该账号直接以
熔断失败收场，且**已消耗的邮箱/OTP 无法回收**；参考项目在同一批内换出口重试。

**建议**（分层，勿一步到位）：
1. 只读接线：`registration_preflight` 的失败分支先用 `proxy_edge_probe.probe_openai_edge`
   判定 `blocked_by_cloudflare`，把错误名细分（`...:cloudflare_challenge`），
   使 `failure_registry`/batch 能按"出口被拦"而非"网络错误"处理。
2. 出口轮换：在 `commands/registration.py` 已有的"候选路由"循环里，把 CF 判定
   纳入 `host_failures` 快速跳过（当前已按主机计数跳过，只需让 CF 也进该计数）。
3. 流程内轮换（可选、需 A/B）：对齐 `follow_authorize`，在 `auth_flow` 的
   authorize 步骤对 CF challenge 做一次同池换出口重试。

### 🟡 P1-1 补只读 Plus 试用券探测

**现状**：`accounts/account_promotion.py:50 parse_accounts_check` 从
`accounts/check/v4-2023-04-27` 的 `eligible_promo_campaigns.plus` **间接**判断可试用。
参考项目用一个**更直接的官方端点**作主判据：

```
GET https://chatgpt.com/backend-api/promo_campaign/check_coupon
      ?coupon=plus-1-month-free&is_coupon_from_query_param=true
```

（`vendor/gpt-register-tool/协议/plus_trial_checker.py:20`，
`check_plus_trial()` 见 `:134`；accounts/check 只用于补充 plan/兑换细节。）

**建议**：在 `account_promotion` 增加只读 `check_coupon` 探测（不兑换），与
`parse_accounts_check` 互为佐证：
- 两者一致 ⇒ 提高 `promotion_state=可试用Plus` 置信度；
- `check_coupon` 200 而 accounts/check 说 free ⇒ 保留 `PROMOTION_STATE_TRIAL_ELIGIBLE`；
- 401/403 时用参考的 `_looks_deactivated` 词表区分"封号"与"AT 失效"
  （本仓 `promotion_states` 目前只分 `AUTH_INVALID`/`PROBE_FAILED`）。

**成本**：纯只读 GET，无需 Checkout；复用 `auth_headers` + `proxy_routing` 既有出口纪律。

### 🟡 P1-2 金额/优惠解析补交叉一致

**现状**：`pp_link_helpers.stripe_amount_details`（`:317`）与
`checkout_contract._extract_amount_minor`（`:341`）都只取**第一个命中的权威字段**，
不校验同一 payload 内其它金额路径是否一致。当 payload 同时带
`total.total` 与 `total.taxInclusive` 时，参考项目明确记录了误判案例：

> `promo_detector/oaics.py` `_PAYABLE_PATHS` 上方注释：把 `total.taxInclusive`（税额分量，
> 无优惠时为 0）与 `total.total` 并列比较会得出"金额观测不一致"，结果 `amount=None`、
> `promo=no`——把本该显示的价格也吞掉。

**建议**：新增纯函数（`services/protocol-payment/common/protocol_core.py` 或
`checkout_contract` 内）：
- `payable_amount(payload)`：按权威路径序返回 `(路径, 金额)`；
- `discount_breakdown(payload)`：返回 `(subtotal, discount, total)`，用于区分
  "全额抵扣"与"非零价"；
- `amount_observations(payload)`：列出所有路径观测值，仅在**权威值与观测值冲突**时
  标 `unknown`（而不是把 `taxInclusive` 当权威）。
映射：`StripeCapabilityEvidence.offer_state` 增加 `discounted_zero_due` 子态，
`payment_capability.build_capability_probe_result`（`:275`）在
`require_zero` 下用 `discount_breakdown` 佐证 `amount==0` 是"被抵扣"而非"读不到"。

### 🟡 P1-3 密码页 Sentinel flow bundle

**现状**：`registration_handlers._issue_sentinel`（`:589`）按调用点单 flow 取 token。
参考项目记录浏览器样本在密码页**一次**加载即对 `authorize_continue` /
`username_password_create` 等三个 flow 携带**同一个 `p`**，并专门实现
`request_password_sentinel_bundle`（`myfanhua:413`）复现该形态，注释原文：

> "浏览器样本的三条 req 携带完全相同的 p，而不是每个 flow 重新随机一次。"

**建议**：先只做**取证**——在 `prepare_identity` 里记录当前各 flow 的 `p` 是否不同
（一次日志、不改行为），确认本仓是否确实与浏览器形态偏离。若偏离且能定位到
`_requirements_token` 每次随机，再决定是否引入 bundle（属于 Sentinel 载荷形态变更，
需按 `protocol-registration.md` 的"受控线上对照"纪律执行）。

### 🟢 P2-1 账单模板国家覆盖

`sms_tool/pp_link_helpers.BILLING_DATA` 约 15 国，未命中回落 `DE`；
参考 `promo_detector/billing.py` 用 `GEO` 表 + `_OVERRIDE_LINE1` 覆盖约 200 国，
缺 GEO 的国家也有国家码兜底。**建议**：只扩表、不改调用签名；把
`billing_for_country` 的兜底从"硬编码 DE"改为"按目标国生成占位模板"，
避免非覆盖国拿德国地址导致 Checkout 国别/币种不一致。

### 🟢 P2-2 导航先行（navigation-first）状态构建

参考 `navigate_create_account_password`（`myfanhua:564`）与
`navigate_about_you`（`myfanhua:695`）在 POST 之前先 GET 页面，显式建立
auth step 状态并检测落入旧密码路径。本仓 `create_account`（`registration_handlers:933`）
直接 POST，仅以 `referer=/about-you` 表达上下文。**建议**：仅在
`create_account` 连续失败（如 `invalid_auth_step`）的诊断分支里增加一次 GET
`/about-you` 的取证，不改变主路径。

---

## 4. 明确不对齐的项

| 项 | 参考实现 | 不对齐理由 |
|---|---|---|
| 远程 CDK 提链服务 | `myfanhua/core/extract_link_service.py`：`POST /api/extract` + SSE + CDK 配额，只有 `pix/upi/kakao_pay/ideal` | 本仓本地实现 15 方法、出口可控、无外部依赖与密钥。远程化会引入第三方可用性风险 |
| 外部 `flow_trigger` | `wangshen233/core/flow_trigger.py`：注册后 POST 到固定 IP 的内部服务 | 与本仓"注册成功以 AT 探测为准"的契约无关，且是硬编码外部地址 |
| 纯 Python PoW Sentinel | `myfanhua/core/sentinel.py` | 本仓 Node SDK runner 已被参考项目反过来采纳（§2.3） |

---

## 5. 验证计划

1. **P0-1**：受控同批 A/B（预检端点）——比较
   `registration_preflight_failed:*` 计数、首轮 authorize 落点、CF challenge 出现率。
   改端点前保留当前行为为默认，用配置开关灰度。
2. **P0-2**：离线单测——给 `registration_preflight` 注入伪造的 `cf-mitigated: challenge`
   响应，断言错误名细分且进入 `host_failures` 跳过；线上只观察不切换。
3. **P1-1**：离线单测——`check_coupon` 200/401/403 三种 body → 状态映射；
   与 `parse_accounts_check` 的一致性矩阵。
4. **P1-2**：离线单测——`payable_amount`/`discount_breakdown`/`amount_observations`
   对含 `total.taxInclusive` 的 fixture（复现参考项目记录的误判）返回
   `amount=total.total`，且 `offer_state=zero_due` 不因税分量被推翻。
5. **P1-3**：仅取证日志，不改行为。
6. 回归：`pytest tests/test_protocol_payment_*.py tests/test_registration_*.py
   tests/test_checkout_contract.py tests/test_payment_capability.py`。

---

## 6. 复现方式（本报告用到的参考仓副本）

```powershell
cd runtime/tmp/refrepos
git clone --depth 1 https://github.com/myfanhua/turb-gpt-free-register myfanhua
git clone --depth 1 https://github.com/wangshen233/turb-gpt-free-register-oss-20260928 wangshen233
```

`runtime/tmp/` 已被 `.gitignore` 排除；参考仓副本仅供对照，**不得**成为项目依赖
（对齐 `docs/architecture.md` 的依赖方向与 `services/protocol-payment/` 进程边界纪律）。

---

## 8. 追加：UPI 深度链接三增量（2026-10-01，参考 `cljh1190-rgb/upi-vippro-tool-api`）

背景：本仓 UPI 的 `mandate_not_signed` 是**结构性**——Stripe 拒绝第三方 confirm
Checkout 创建的 SetupIntent（`UPI_LOCAL_MANDATE_FATAL_MARKERS`），参考仓的本地
通道同样是 `upi-zero-link` 上游（我们已移植），因此它**不含**新的签字机制；但它有三项
我们缺的工程增量，已按独立开关接入 `sms_tool/upi_link/pipeline.py`：

| 增量 | 内容 | 开关（默认） | 落地位置 | 测试 |
|---|---|---|---|---|
| ① 印度出口分级 + 结账准入 | 同代理内换 session 直到出口为 IN 且能进结账（匿名 GET `/backend-api/payments/checkout` 与 `/`，非 CF 且 <500 视为准入）；在共享 `payment_egress` 强门禁**之前**选路 | `upi.india_exit_probe` / `UPI_INDIA_EXIT_PROBE`（**off**） | `_upi_select_india_exit` / `_upi_exit_admits_checkout` / `_upi_exit_country`；`proxy_edge_probe.probe_openai_edge(path=...)` | `tests/test_upi_india_exit.py` |
| ② confirm 前双 init + 双 tax | 复现参考实测的 `init → tax → init → tax` 顺序（第一次 tax 响应可能仍带更新前金额） | `upi.repeat_tax_region` / `UPI_REPEAT_TAX_REGION`（**on**） | once-body 的 Stage 4.5 | `tests/test_upi_tax_repeat.py` |
| ③ 换出口重开结账单 | 仅在**未走到 confirm**的失败（`no_free_trial` / `upi_not_available` / `checkout_*` / `oaics_*`）上换一条出口重开整张 Checkout；`mandate_not_signed` / `payment_chain_link` / `link_unverified` / `upi_provider_declined` 永不重开（approve 一次性） | `upi.rounds` / `UPI_ROUNDS`（**1** = 原行为） | 公开 `generate_upi_qr_link` 包装 `_generate_upi_qr_link_once` | `tests/test_upi_rounds.py` |

**常量与契约**：`generate_upi_qr_link` 仍只有一处定义且签名不变（
`tests/test_upi_link_entrypoint_unique.py`），单次尝试体改名为
`_generate_upi_qr_link_once`；配额 `UPI_CALL_OPTIONS` 未动（`rounds` 走配置/环境，
不进签名）。

**验证**：全量 `pytest` 5706 passed / 7 skipped（1097 subtests）；全部门禁 OK
（delayed-import / unused-import / endpoints-literal / bare-print / module-coverage /
docs-consistency / config-key / ruff / compileall 等）。三项均为可 A/B 的独立开关，
默认值见上表（① 与 ③ 默认关闭以保持既有网络与重试预算）。
