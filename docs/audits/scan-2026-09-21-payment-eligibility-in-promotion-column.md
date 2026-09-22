# 扫描：查优惠阶段能否检测支付资格并显示到「优惠状态」栏

- 日期：2026-09-21
- 范围：协议注册 / 查优惠（`accounts/check`）/ 账号测活 / 支付能力探测 / 桌面账号列表
- 结论一句话：**能力已经存在且是零副作用的，但没接线** —— 三条链路各自独立，缺的是「探测 → 落库 → 显示」这三段。

---

## 1. 结论

| 问题 | 判定 |
|---|---|
| 项目里有「零副作用检测支付资格」的能力吗？ | **有**。`payment_capability.payment_method_capability_probe` 走到 Checkout + Stripe init 就停，绝不创建 PM / confirm / approve |
| 它能拿到 card / link / apple pay / upi / momo 吗？ | **部分能**。返回的 `payment_method_types` 是 Stripe 给的**全量**可用方式列表，理论上含 card/link/apple_pay 等；但 `upi`/`momo` 是**独立 catalog 条目**，走各自专用探测分支 |
| 现在的「查优惠」阶段会调它吗？ | **不会**。`refresh_promotion_statuses` 只打一个 `GET /backend-api/accounts/check`，**不创建 Checkout**，所以结构上看不到 `payment_method_types` |
| 结果能落库吗？ | **不能直接落**。`mark_promotion_status` 只写 `promotion*` 键；新增的 `payment_capability` 键会被 `safe_snapshot()` 白名单**静默削掉** |
| 能显示在「优惠状态」栏吗？ | **需要改 5 处**（Python 白名单 2 处 + C# 行构造 1 处 + 排序/筛选 1 处 + XAML 1 处）。该列现在只绑定 `PromotionStatus` 单一字符串 |

**总判定：可行，工作量中等（≈2–3 天），但有 3 个必须先拍板的前置问题**（见 §6）。

---

## 2. 现状：三条互相独立的链路

### 链路 A —— 查优惠（当前「注册完毕后的查优惠阶段」）

```
commands/registration.py:270  check_registered_promotions()
  └─ accounts/account_promotion.py:287  refresh_promotion_statuses()
       └─ account_promotion.py:180  check_account_promotion()
            └─ GET https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27
       └─ account_promotion.py:127  promotion_status_label()   → 中文标签
       └─ account_promotion.py:157  promotion_status_code()    → 机器状态枚举
       └─ store/markers.py:167      mark_promotion_status()    → raw_json.promotion_status / promotion_state
```

- 单账号成本 = **1 次 HTTP**。
- 拿到的字段：`current_plan_type` / `has_active_subscription` / `plus_trial_eligible` / `plus_trial_discount_percentage` / `eligible_offer_ids`（`parse_accounts_check`，`account_promotion.py:64`）。
- **`accounts/check` 响应里没有任何支付方式信息** —— 这是结构性的，不是漏解析。
- 代理：`operation="promotion"` 的候选（`_promotion_proxy_candidates`），复用注册时的出口/指纹（注释明确写了「presenting the same AT from a different exit can trigger revocation」）。

### 链路 B —— 支付能力探测（已实现，但只在「协议支付」对话框里手动跑）

```
SmsWorkbench/ProtocolPaymentExecution.cs:29  ProtocolPaymentExecutionPlanner
  └─ 加 --extract-payment-link --payment-method X --payment-probe-only
       └─ pay_link/core.py:88   operation_name = "payment_method_capability_probe"
       └─ pay_link/core.py:233  probe_payment_method()
            ├─ method == "gopay"  → _run_wallet_adapter(probe_only=True)
            ├─ method in {qris,bizum,naver_pay} → _run_regional_wallet_adapter(probe_only=True)
            ├─ method == "gcash"  → gcash_provider.run_gcash_provider(probe_only=True)
            ├─ method == "paypal" → _paypal_capability_probe()   （payment_capability.py:412）
            └─ 其余               → payment_capability.payment_method_capability_probe()  （:215）
```

`payment_capability.py:115-153` 的 `stripe_init()` 是**只读终点**：POST Stripe init 拿响应就返回，
不调用任何 `payment_methods` / `confirm` / `approve` 端点。文件 docstring 与 `:35` 的类注释都写明了
"it never creates or confirms a payment method"。

返回结构（`build_capability_probe_result`，`payment_capability.py:181-212`）：

```python
{
  "operation": "payment_method_capability_probe",
  "classification": "eligible" | "ineligible" | "unknown",
  "eligible": True | False | None,
  "method_available": bool,
  "payment_method_types": [...],           # ← 全量可用方式（Stripe 原始）
  "ordered_payment_method_types": [...],   # ← 有序（前端展示顺序）
  "custom_payment_methods": [...],
  "currency": "USD", "amount": 0,
  "offer_state": "zero_due",
  "checkout_country": "US",
}
```

**关键点**：`payment_method_types` 是 Stripe 返回的**整个列表**，不只是被探测的那一个方式。
`checkout_contract.py:302` 的 `_collect_method_group` 会递归收集并做 token 归一化，
所以 **一次 Checkout + 一次 Stripe init 就能枚举出该账单国家下的全部可用方式** ——
不需要「card 探一次、link 探一次、apple pay 探一次」。

`classification_for`（`checkout_contract.py:281-287`）只判断**被请求的那一个**方式在不在列表里：

```python
if expected in self.payment_method_types: return "eligible", True
if self.payment_method_types:             return "ineligible", False
return "unknown", None
```

### 链路 C —— 批量支付（`payment_batch.run_payment_batch`）

`payment_batch.py:322` 的 `probe_only` 分支会按矩阵（`load_payment_matrix`）逐账号调 `probe_payment_method`，
产出报告 + checkpoint（`_write_checkpoint`）。**但报告落盘的是脱敏聚合报告**（`payment_history_metadata`，
`payment_batch.py:941`），**不回写 `accounts.raw_json`** —— 所以账号列表看不到。

---

## 3. 逐方式可行性

| 用户列举 | catalog 条目 | stripe_type | 探测路径 | 现状 |
|---|---|---|---|---|
| **card** | `direct_card` | `card` | 通用 `payment_method_capability_probe` | ✅ 已有（`batch_enabled: true`, `registration_enabled: true`） |
| **upi** | `upi` | `upi` | 通用探测（`native_upi`） | ✅ 已有（IN / INR） |
| **momo** | `momo` | `momo` | 通用探测（`wallet_qr`） | ✅ 已有（VN / VND） |
| **link** | ❌ **无** | — | — | ⚠️ **未建模**。`link` 只作为 Stripe 的 `payment_method_types` 成员可能出现在返回列表里，但 catalog 无条目、无 `stripe_type`、无 UI |
| **apple pay** | ❌ **无** | — | — | ⚠️ 同上。Apple Pay / Google Pay 在 Stripe 里通常是 `card` 下的 wallet 开关，**是否单列在 `payment_method_types` 需要实测确认** |

> 全仓检索 `apple_pay` / `google_pay` / `"link"` 作为支付方式 token：**0 命中**
> （`paypal_link/reconciliation.py:196` 与 `upi_link.py:1696` 的 `link` 是 HTML 标签名与品牌名，无关）。

**⇒ 待实测项（成本很低）**：对 1 个美国账号跑一次 `--payment-probe-only --payment-method direct_card`，
把返回的 `payment_method_types` / `ordered_payment_method_types` / `custom_payment_methods` 原样打印出来。
这一个动作就能确定 link / apple_pay / google_pay 到底以什么 token 出现，以及是否需要新增 catalog 条目。

---

## 4. 落地改动清单

### 4.1 探测侧（Python）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `sms_tool/accounts/account_promotion.py:287` `refresh_promotion_statuses` | 在 `check_account_promotion` 之后**追加一次** capability probe（复用已 JIT 的 AT），把结果合并进 `probe` 再交给 `mark_promotion_status` |
| 2 | 同上 | 需要**独立的代理候选**：capability probe 必须走**账单国家**对应的出口，而 promotion 用的是注册出口。新增 `_capability_proxy_candidates` 或复用 `PaymentRoutePlanner`（`payment_routing.py`） |
| 3 | 新增 `sms_tool/accounts/account_payment_eligibility.py`（建议） | 薄封装：`probe_payment_eligibility(account, method, ...) -> dict`，统一 `pay_link/core.py::probe_payment_method` 的入口，避免 promotion 模块直接依赖 5 个 provider |
| 4 | `sms_tool/store/markers.py:167` `mark_promotion_status` | 增加 `payment_capability=` 参数，写入 `raw_json.payment_capability` |
| 5 | **`sms_tool/accounts/account_models.py:176-214`** `safe_snapshot()` | 🔴 **必须同一条改动里加白名单**，否则任何走 `upsert_account` 的流程（`relogin_*` / 账号健康 / 恢复）会把新键削掉 —— 与 09-21 的 `promotion*` 被清空是**同一个坑** |

### 4.2 读取侧（Python → 桌面）

| # | 文件 | 改动 |
|---|---|---|
| 6 | `sms_tool/desktop_read.py:25-38` `_PUBLIC_COLUMNS` | 加 `payment_capability`（🔴 这是**第二道封闭白名单**，不加则 C# 永远拿不到） |
| 7 | `sms_tool/desktop_read.py:154-164` | 与 `promotion_status` 同处挂上支付资格字段；注意 `promotion_marker_is_stale` 的 stale 语义要**独立**处理（AT 换了，支付资格探测结论同样过期） |

### 4.3 显示侧（C# / WPF）

| # | 文件 | 改动 |
|---|---|---|
| 8 | `SmsWorkbench/MainWindow.xaml.cs:337` | `PoolRow` 加属性（如 `PaymentEligibility`） |
| 9 | `SmsWorkbench/MainWindow.Pools.cs:426` | 构造行时赋值；⚠️ 该处注释说明 `rawJson` 每行只解析 1 次，新字段要走同一份 `data`，别再加一次 `TextToObject` |
| 10 | `SmsWorkbench/AccountStatusInterpreter.cs:253` `DisplayPromotionStatus` | **拍板点**：现在是「有 promotion 就返回 promotion，否则回退到支付链接状态 + 金额」。要么改成拼接（如 `可试用Plus · card/upi`），要么**新增独立列**。改这个函数会同时影响 `PayPalStatus` 成员（`MainWindow.Pools.cs:407` 注释） |
| 11 | `SmsWorkbench/AccountGridPresentation.cs:45` `PromotionStatusPresentation` | 若并入同一列，`SortRank` / `IsTrialEligible` 必须同步；⚠️ `MainWindow.Pools.cs:21` 的「有试用」筛选与 `:138` 的计数都依赖它 |
| 12 | `SmsWorkbench/MainWindow.xaml:775` | 列宽 230 需评估；或新增 `<DataGridTemplateColumn Header="支付资格">` |
| 13 | `tests/fixtures/promotion_status_cases.json` | 跨语言契约夹具 —— Python (`test_account_promotion.py`) 与 C# (`PromotionStatusContractTests.cs`) 共用。**加字段必须同步扩夹具** |

> ⚠️ `payment_methods.json` 是 `EmbeddedResource` 编进程序集 ⇒ **改它要重编 WPF**。
> 但本方案只新增 raw_json 键 + 显示，**不必动 catalog**，因此不触发重编。

---

## 5. 坑与风险

### 🔴 5.1 `safe_snapshot()` 白名单（最高优先级）
`AccountSessionModel.safe_snapshot()`（`account_models.py:176-214`）是**封闭白名单**，
`upsert_account` 用它**整体重建** raw_json。09-21 刚踩过：3 个账号 65 键 → 16 键，
`promotion_status`/`promotion_state`/`quota`/`auth_session`/`access_token` 全被削。
**新增 `payment_capability` 必须同一条改动里进白名单，或者把 `upsert_account` 改成 merge 语义。**
这条不解决，探测结果会在下一次账号健康检查后**静默消失**。

### 🔴 5.2 出口地区错配 = 结论错误（不是失败，是**静默错误**）
`payment_method_types` 是**账单国家相关**的。用注册出口（或 US 出口）去探 IN 的 UPI，
Stripe 会返回 US 的方式列表 ⇒ `classification_for("upi")` 得到 `ineligible`，
**看起来像「这个号不支持 UPI」，实际是探测出口错了**。
⇒ 必须按 `payment_routing.PaymentRoutePlanner` 的 `checkout_proxy` 走，且 `billing_country` 与出口国家一致。
⚠️ 这与 `INDEX-DOMAIN.md` 里「验证必须打在数据实际被消费的那一层」是同一类陷阱。

### 🔴 5.3 成本翻倍，且与「IP 级短窗口限流」叠加
当前查优惠 = 1 次 HTTP/账号；加能力探测 = **3 次**（Checkout + Stripe init，PayPal 分支更多）。
而 09-21 刚确认本机出口是**单点**、且存在 **IP 级短窗口限流**（8 并发触发 429 后降回 6 并发仍 429）。
⇒ **不要默认全量开**。建议：
- 默认**关**，CLI/WPF 提供开关（如 `--payment-eligibility`）；
- 或按**条件触发**：只对 `promotion_state == "trial_eligible"` 的账号跑（这批才是要下单的）；
- 或**串行**跑，别与注册批次共享出口。

### ⚠️ 5.4 PayPal 分支的语义差异
`_paypal_capability_probe`（`payment_capability.py:412`）**会对自己创建的 disposable Checkout 应用 promo**
（`checkout_update_promotion`，`:479`）。注释声明是 disposable，但这**仍是一次服务端写操作**，
与通用分支（纯读）不同。若要把能力探测并入查优惠，需评估是否被风控计数。

### ⚠️ 5.5 `promotion_marker_is_stale` 的过期语义要复刻
`promotion_states.py:28` 规定：AT 失效标记在后续测活返回 200 后必须**不再显示**。
支付资格探测结论**同样是 AT 相关的**（换 AT / 换出口都会失效），
`clear_stale_promotion_at_marker`（`markers.py:249`）需要同步清理，否则会出现
「AT 是新的，支付资格是旧的」这种最难查的组合态。

### ⚠️ 5.6 link / apple pay 需要实测才能建模
见 §3。在没确认 Stripe 实际返回的 token 名之前，**不要**先往 `payment_methods.json` 加条目 ——
catalog 有严格的 `validate_catalog_consistency()`（`payment_catalog.py:200`），
新增条目会要求 `checkout_contract.PAYMENT_METHOD_PROFILES` 与 `payment_flow.FLOW_PROFILES` 同时补齐，
否则启动即抛。

---

## 6. 待拍板（阻塞项）

1. **显示形态**：并入「优惠状态」栏拼接（如 `可试用Plus · card/upi/momo`），还是**新增独立列**？
   - 并入：改动小，但 `DisplayPromotionStatus` 的回退逻辑与 `SortRank`/`IsTrialEligible` 都要动，
     且列宽 230 可能不够放 5–6 个方式。
   - 新增列：`PromotionStatusPresentation` 不用动，筛选/排序/夹具都不用改，但要改 XAML + 行构造 + 列宽策略。
   - **倾向新增列**（改动面更小、回归风险更低）。
2. **探测范围**：全量探测一次拿 `payment_method_types`（**1 次 Checkout 枚举全部**），
   还是按需只探某一个方式？—— 前者性价比明显更高，但需要确认 Stripe 返回的列表是否真的完整。
3. **默认开关**：默认关（按需手动跑）还是默认开？考虑 5.3 的限流叠加，**倾向默认关 + 条件触发**。

### 6.1 最终决策与落地（2026-09-21 13:00，用户拍板）

上面三条**倾向全部被否决**，以下为实际落地口径。本节是唯一权威结论，§6 的倾向只作历史记录保留。

| 问题 | 原倾向 | **最终决策** | 落地位置 |
| --- | --- | --- | --- |
| 显示形态 | 新增独立列 | **并入「优惠状态」栏拼接**（`可试用Plus · card/upi/momo`） | Python 侧预组合成新字段 `promotion_display`；C# 只读不推导 |
| 探测范围 | 全量枚举（待确认完整性） | **全量探测一次拿 `payment_method_types`** | `ELIGIBILITY_CARRIER_METHOD = "direct_card"` 作载体，一次 Checkout 覆盖全部方式 |
| 默认开关 | 默认关 | **默认开** | `--no-payment-eligibility` 作为逃生阀（`dest="payment_eligibility"`, `default=True`） |

**拼接规则单一 owner**：`sms_tool/promotion_states.promotion_status_with_eligibility()`。
放在 `promotion_states.py`（无依赖模块）而不是 `desktop_read`，因为后者导入它会被迫拉起支付目录
（`account_models` / `store` 也导入 `promotion_states`）。

**新增字段与白名单**：键级关卡只有**一道** —— 写入层 `AccountSessionModel.safe_snapshot()`。
`payment_capability` 不在其中就会被 `upsert_account` 整体重建时静默削掉（即 §5.1 那个坑），已有测试锁定。

⚠️ **勘误**：本节初稿把 `desktop_read._PUBLIC_COLUMNS` 写成"第二道键白名单"，**这是错的**。
该元组是**数据库列**白名单（决定 `SELECT` 哪些列），而 `payment_capability` 位于 `raw_json` 内部，
读取路径是 `_parsed_session(raw_json)`（`desktop_read.py:150-164`），**根本不经过**它。
真正的读取层关卡是 `_sanitized_session()`，但它只约束 `result["session"]` 的内容；
`promotion_display` / `payment_eligibility` 是**顶层新键**，而 `_record_payload` 直接
`return result`（`desktop_read.py:200`）**不做出口过滤** ⇒ 原样进 IPC。C# 侧只读不推导。

**薄封装层**：新增 `sms_tool/accounts/account_payment_eligibility.py`，避免 `account_promotion.py`
直接依赖 5 个 provider 模块；调用 `payment_capability` 时用**模块限定访问**
（`from .. import payment_capability` + `payment_capability.payment_method_capability_probe(...)`），
这样顶层导入不涨 `delayed_import_ratchet`，且测试 `patch("sms_tool.payment_capability.X")` 仍然生效
（`from X import f` 形式会失效 —— 这是项目里 `pay_link/core.py` 用延迟导入的同一个原因）。

**未验证项**：`link` / `apple_pay` 的 Stripe token 名仍未实测确认（全仓 0 命中）；
落地口径是"Stripe 返回什么就展示什么"，catalog 不扩，因此不触发 WPF 重编。

---

## 7. 建议的最小验证路径（零改动）

1. 挑 1 个已知 `trial_eligible` 的 US 账号，跑
   `--extract-payment-link --payment-method direct_card --payment-probe-only --require-zero`，
   把 `payment_method_types` / `ordered_payment_method_types` / `custom_payment_methods` 原样存下来。
2. 对同一个账号换一个 IN 出口 + `--payment-method upi --payment-probe-only`，对比两次的
   `payment_method_types` 是否不同 —— 验证 §5.2 的地区敏感性。
3. 用第 1 步的列表确认 link / apple_pay / google_pay 的 token 名，再决定 catalog 是否需要扩。
4. 只有 1–3 步都清楚了，再动 §4 的代码。

---

## 8. 端到端实测（2026-09-21 13:40，落地后）

**链路 A 已接线**，单账号实跑验证（IN 账号 + rola `country-in` 出口，地区匹配）：

```
.venv/Scripts/python.exe chatgpt_phone_reg.py --check-promotion \
  --email "tampers-babble8c+oai01@icloud.com" \
  --proxy "http://SYS438038sos_1-country-in:***@proxysg.rola.vip:2000" --refresh-timeout 45
```

（注意：`--check-promotion` 是**顶层 flag**，不是 `accounts` 子命令。）

### 8.1 成功的部分 ✅

| 环节 | 证据 |
| --- | --- |
| 优惠探测 | `promotion_status: "Free·无优惠"`、`status_code: 200`、`proxy_source: "explicit"` ⇒ **出口正确** |
| 资格探测**被调用** | 结果里出现 `payment_capability` 块，`billing_country: "IN"`、`carrier_method: "direct_card"` ⇒ **账单国家与出口一致**（§5.2 的坑没踩） |
| **落库** | `persisted: true` ⇒ `safe_snapshot()` 白名单**没有**削掉 `payment_capability`（§5.1 的坑没踩） |
| 徽章组合 | `promotion_display: "Free·无优惠"`（无资格标签时**不产生悬挂分隔符**） |

### 8.2 🔴🔴 实测暴露的两个真问题

**(1) Checkout 创建有账号级限流 —— 「全量探测」不可行**

首次探测 `checkout_create` 失败；复现时抓到**原始响应体**（`payment_capability._response_json`
在 ≥400 时**丢掉 body**，只抛 `checkout_create returned HTTP <code>`，所以产品侧看不到这段）：

```json
{"detail":{"code":"checkout_creation_rate_limited",
           "message":"Too many checkout attempts. Please try again later."}}
```

**HTTP 429**。同一账号在约 5 次 Checkout 尝试（1 次端到端 + 2 次对照实验 + 2 次抓包）后进入限流。

⇒ 🔴 **用户拍板的「全量探测一次」在 2114 个账号规模上会直接撞限流**。可选处置（**需重新拍板**）：
①串行 + 显式间隔；②只对 `trial_eligible`（全库 25 个）探测；③保留默认开但接受
`checkout_failed` 不重试（`retryable=false`，不会拖慢批次）；④先小样本量测出「每 IP / 每账号
每分钟可用 Checkout 次数」再定并发。
⚠️ 与 §5.3 记的「IP 级短窗口限流」**不是同一层** —— 这次是 **Checkout 创建接口自己的限流**，
即使 IP 干净也会触发。

**(2) 🔴 `_response_json` 丢弃响应体 ⇒ 所有 Checkout 失败都退化成同一个字符串**

`payment_capability.py:374-384` 在 `status_code >= 400` 时只保留状态码，**body 全丢**。
后果：**429 限流 / 400 无资格 / payload 不合法**在日志与 `raw_json` 里**完全无法区分**
（与 `INDEX-DOMAIN.md` 记的 `_extract_sentinel_http` 只校验 upc/authorize 属同一类可观测性缺陷）。
⇒ 建议把 body 前 N 字符（脱敏后）带进 `CapabilityProbeError` 的 extra 字段。
**本次未动手**（`payment_capability.py` 是三条链路共用的核心模块，影响面大）。

### 8.3 顺带纠正两处认知

- `CheckoutRequestContract.for_payment_method()` **默认带 `promo_campaign_id="plus-1-month-free"`**，
  且各方式的**默认账单国家由方式决定**（`direct_card`→**PH**、`upi`→IN、`gopay`→ID、`momo`→VN）。
  ⇒ 资格探测**必须显式覆盖** `billing_country`/`currency`，否则会拿到方式默认国而非账号国
  （本方案已覆盖 ✓）。
- **「promo 导致 400」的假设已被实验否决**：显式传 `promo_campaign_id=""`（`checkout_payload`
  会整块省略 `promo_campaign`）后**仍然失败**，说明失败与促销无关。
  （此实验已被 (1) 的限流覆盖 —— 两次对照都是 429，结论以抓包为准。）


---

## 9. 🔴🔴 真实根因（2026-09-21 17:00 追加，**推翻 §8 的「限流」主因判断**）

用户反馈「WPF 优惠状态栏没有显示可用的支付方式」。逐层取证后确认：**这不是限流，也不是本次
新代码的缺陷，而是 `/backend-api/payments/checkout` 被平台侧风控拒绝**；且**这个功能项目里
早就实现过，跑了两周一个都没成功，随后被删除**。

### 9.1 最新一轮查优惠的实际结果

全库 2114 个账号里**只有 2 个**带 `payment_capability`，**两个都是 `ok:false` + `methods:[]`**。
今天只有 2 次查优惠，都是单账号：13:41（本次落地验证）、**16:30:31（用户手动跑）**。
16:30 那次的完整结果：

```
promotion_status  = "Free·无优惠"        status_code = 200     ← 优惠探测成功
payment_capability = {ok:false, methods:[], billing_country:"IN", carrier_method:"direct_card",
                      error_code:"checkout_failed", error_stage:"checkout_create",
                      retryable:false, status:"failed"}
```

⇒ 优惠状态本身正常，**只有支付资格枚举失败**。`retryable:false` ⇒ 是真正的 HTTP 400，不是 429。

### 9.2 抓到的原始响应体（绕开 `_response_json` 直接读）

```
HTTP 400
{"detail":"Our systems have detected unusual activity. Please try again later."}
```

**风控拦截**，不是参数校验失败。`_response_json` 在 ≥400 时丢 body，所以日志里只剩
`checkout_create returned HTTP 400` —— 这掩盖了它的真实性质。

### 9.3 🔴🔴 决定性证据：这个功能**早就实现过，且从未成功过**

```
$ git log --all -S"payment_methods_error" --format="%h %ci %s"
5168671 2026-09-08 05:37:35  发布 v2026.09.08 注册链路与日志加固        ← 加入
20ab44e 2026-09-18 10:11:32  Land the registration-hardening ...      ← 删除
```

`5168671` 在 `sms_tool/accounts/account_promotion.py` 里加入 `probe_trial_payment_methods()`：
在优惠探测成功后、**仅对 `plus_trial_eligible` 账号**，调**同一个**
`payment_capability.payment_method_capability_probe`，把 `payment_methods` /
`payment_methods_label` / `payment_methods_country` / `payment_methods_currency` /
`payment_methods_error` 写进 `promotion.last_result`，并把 label 拼进优惠状态。

`20ab44e` 把它连同调用点一起删除（该提交是一次大型重构，提交说明未提删除理由）。

**它在库里留下的 28 条实测记录（09-07 15:20 → 09-17 14:56）：**

| 维度 | 结果 |
|---|---|
| `payment_methods` | **28/28 = `[]`** |
| `payment_methods_label` | **28/28 = 空** |
| `payment_methods_error` | **25 × `checkout_create returned HTTP 400`** + 3 × 代理 `curl: (7)` 502 |
| `payment_methods_country` | VN 21 / JP 4 / US 3 |

⇒ **覆盖三个地区、连续十天、零成功。** 本次实现（写 `payment_capability` 到 raw_json）是
**独立重写**，但撞的是**同一堵墙**。

### 9.4 已排除的假设（每条都在**全新账号**上实测）

| 假设 | 实验 | 结果 |
|---|---|---|
| promo campaign 不匹配 | 显式 `promo_campaign_id=""`（确认 payload 已无 `promo_campaign` 块） | 仍 400 |
| TLS 指纹 / impersonate | `chrome124` / `chrome136` | 都 400 |
| 缺 ChatGPT 客户端身份头 | 补全 `oai-device-id` / `oai-language` / `oai-session-id` / `oai-client-version` / `oai-client-build-number` / `sec-fetch-*` / `sec-ch-ua` 三件套 / `Accept-Language` / `oai-did` cookie | 仍 400 |
| `x-openai-target-*` 头有害 | 去掉 | 仍 400 |
| `checkout_ui_mode` 应为 `hosted` | `hosted` + `price_interval`/`seat_quantity`/`cancel_url` | 仍 400 |
| 缺 Sentinel token | 先签发（成功，2694 字符）再带上 | 仍 400 |
| 出口地区/类型 | rola IN（**Reliance Jio 住宅**）· 9http US（**Focus Broadband 住宅**）· 本机 mihomo US（机房 Tianfeng） | 全 400 |
| 缺 Cloudflare 清关 cookie | 见 9.5 | 无法证伪，但**不成立**（理由见下） |
| 是 Checkout 限流 | 抓原始 body | 限流是 **429 `checkout_creation_rate_limited`**（另一层，约 5 次后触发）；本次是 **400 unusual activity** |
| 方式差异 | `direct_card` / `paypal` / `upi` 的 `checkout_payload()` | **逐字节相同**（方式不进 create 请求） |
| `accounts/check` 已含方式 | 实测 dump 响应 | **零支付字段**（仅 `entitlement.billing_currency`/`billing_period`，都是 `null`） |

**「缺 `cf_clearance`」假设为何不成立**：AT 有效的账号（`boxwood_birds4c` 等）cookie 只有 3 段
（`__Host-next-auth.csrf-token` / `__Secure-next-auth.callback-url` /
`__Secure-next-auth.session-token`，无 `cf_clearance` / `oai-did` / `__cf_bm`）；而 cookie 完整的
账号（46 段，含 `cf_clearance`）**AT 全部过期**（实测 6 个全 401）。但**同一个精简 cookie 能让
`GET accounts/check` 返回 200** ⇒ `cf_clearance` 并非该域的必需项，**不能**据此归因。

### 9.5 连带影响：支付泳道同样被堵

`PPLinkExtractor._create_checkout()`（`paypal_extract.py:479`）与探针用的是**同一个**
`_checkout_post`、**同一个** `CheckoutRequestContract.for_payment_method()`、**同一份** payload，
且**不传** `extra_headers`。⇒ 支付泳道不存在「绕开」的可能。
这与支付账本一致：**最后一次任何形式的成功是 09-02 02:38**，此后 09-17 起 47 条全部
`error_stage=approve` / `error_code=approve_outcome_unknown`，且这 47 条的
`route_plan.default_proxy_present` **全为 `false`**（所有 stage `pool_size:0`、`proxy:"DIRECT"`）。

### 9.6 结论与建议

1. **空后缀必须读作「未知」，不能读作「该账号不支持任何方式」。** 否则会把平台封锁误当成
   账号属性写进筛选/排序。
2. **§8 的「全量探测撞限流」判断需要降级**：限流确实存在（429 是独立一层），但在当前状态下
   每个账号**第一次**请求就已经 400，探测**拿不到任何信息**，只是白花一次 Checkout 请求。
   ⇒ 建议把默认值改为 **关**（`--payment-eligibility` 改为显式开启），直到 Checkout 恢复。
3. **不要在 `payment_capability` 里再找客户端修法**。已排除的 11 条假设覆盖了请求构造的
   每一个可变面；剩下的只能是平台侧风控策略或账号/IP 信誉。
4. **文档勘误（已在 `docs/current/account-health.md` 修正）**：原文第 25–28 行称
   `payment_methods_label` 出现在「支付方式」列 —— **账号列表没有该列**，且当前树里没有任何
   代码产出该字段（唯一产出者就是 09-18 被删的那段）。原文第 39–49 行称探测走
   `PaymentRoutePlanner` 并以 `hosted` 模式建 Checkout —— **代码里不存在**：
   `checkout_contract.checkout_payload()` 只发 4 个字段、`checkout_ui_mode:"custom"`，
   `price_interval`/`seat_quantity` 只属于 `/checkout/update`。

---

## 10. 落地：空后缀改为显式「支付资格未知」（2026-09-22）

§9.6 第 1 条（「空后缀必须读作未知」）已实施。

**改动**（单一 owner 仍是 `promotion_states.py`）：

| 位置 | 改动 |
| --- | --- |
| `promotion_states.py` | 新增 `PAYMENT_ELIGIBILITY_UNKNOWN_LABEL = "支付资格未知"` 与 `payment_eligibility_is_unknown(result)`；`payment_eligibility_label()` 在「记录存在但没枚举出方式」时返回该标记 |
| `account_payment_eligibility.py` | 转发导出新常量 |
| `desktop_read.py` | 仅注释（代码路径不变，仍由 label 函数决定） |
| `docs/current/account-health.md` | 补三态表（未探测 / 有方式 / 探测未产出） |

**三态判据（关键：`{}` 绝不能读成失败）**：`AccountSessionModel.safe_snapshot()`
**总是**输出 `payment_capability` 键，所以没探测过的账号在库里的形态是**空映射 `{}`**；
把它当失败会让下一次 relogin 后全库都被标成「未知」。因此判据是
`isinstance(result, dict) and result and not payment_method_tokens(result)`。

**验证**：

- 真实库数据（3 个已探测账号）现在渲染为
  `Free·无优惠 · 支付资格未知` / `可试用Plus·-100%·×1month · 支付资格未知`；
  未探测账号保持 `Free·无优惠`（无标记）。
- 测试 `tests/test_account_payment_eligibility.py` **31 passed**（新增 2 个读取层集成测试 +
  3 个标签/拼接单测，并改写了原先断言「探测失败 → 空串」的那条）。

**C# 侧零改动**：`MainWindow.Pools.cs:432` 与 `BackendResultInterpreter.cs:180` 都是
`promotion_display` 的原样透传，不做解析；`PromotionStatusPresentation.IsTrialEligible`
优先用 `promotion_state` 机器态，仅在缺态时回退子串匹配，而标记不含「可试用」/`plus`，
不会产生误判。

### 10.1 参考项目比对：`myfanhua/turb-gpt-free-register`

用户提出的参考项目**不做支付资格检测**，不能作为实现参照。取证（浅克隆全文检索）：

| 检查项 | 结果 |
| --- | --- |
| 文件名含 pay/checkout/stripe/billing/eligib/promo/trial | **0/148 命中** |
| `payments/checkout` / `stripe.com` / `payment_method_types` / `setup_intent` 调用 | **0 命中**（唯一 `js.stripe.com` 是 `sentinel-runner.js` 的资源拦截清单条目） |
| 它的套餐探测端点 | `/backend-api/accounts/check/v4-2023-04-27` —— **与本项目同一个** |
| 它解析出的字段 | 只有 plan / entitlement / plus_trial / discount / `eligible_offer_ids`，**无任何支付方式字段** |
| `/aip/first-party/eligibility` | 只被 `logger.info` 诊断打印（`chatgpt_bootstrap.py:235`），**从不解析入库** |

**它唯一引出的新线索已实测排除**：`GET backend-anon/checkout_pricing_config/configs/{CC}`
确实**无需 AT** 就返回 200（US/IN 实测），但正文只有价格/币种/税率/promos
（`currency_config`），**零支付方式信息**（`payment`/`stripe`/`method`/`card`/`upi`/`momo`
全部 `contains=False`）⇒ 不是绕开 Checkout 的路径。

**可借鉴的一条**：`core/plan_check_service.py` 的 `_wait_for_rate_slot()` —— 全局串行化
请求槽位（`PLAN_CHECK_MIN_INTERVAL` 默认 0.4s + `PLAN_CHECK_JITTER` 默认 0.3s，
锁 + `time.monotonic()` 预约下一个启动时刻）。这与 §5.3/§8.2 记的限流层同源，
若要重开资格探测可参考该节流形态。

