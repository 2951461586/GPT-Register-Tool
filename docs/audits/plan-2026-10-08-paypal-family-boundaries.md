# PayPal 家族边界设计（2026-10-08）

> 立项/方案文档，**不含实现**。目标是把 ~7.4k 行、19 个模块的 PayPal 家族按
> 「谁拥有 wire、谁拥有编排、谁拥有浏览器」划清边界。
>
> 盘点基于当前工作区（2026-10-08），行数为实测。

---

## 1. 现状盘点

### 1.1 浏览器泳道 `sms_tool/paypal/`

| 模块 | 行 | 自称职责 |
| --- | --- | --- |
| `orchestrator.py` | 509 | 策略选择 + 结果持久化 |
| `form_steps.py` | 434 | 结账表单字段填写 |
| `dom_fields.py` | 429 | 结账页底层 DOM 原语 |
| `flow_steps.py` | 367 | 流程控制：闸门、短信验证、step runner |
| `session.py` | 151 | Camoufox / CloakBrowser 共用的会话辅助 |
| `__init__.py` | 153 | 门面 |
| `config_picker.py` | 97 | 卡/地址输入选择 + 结果持久化 |
| `errors.py` | 13 | 异常类型 |

### 1.2 协议泳道（顶层）

| 模块 | 行 | 自称职责 |
| --- | --- | --- |
| `paypal_extract.py` | 1351 | 支付链接提取（协议式） |
| `paypal_reverse.py` | 1158 | 逆向 HTTP 协议自动支付 |
| `paypal_proxy.py` | 812 | 代理辅助（**无 docstring**） |
| `paypal_authorization.py` | 318 | 只读 BA 授权响应解析/归一化 |
| `paypal_authorization_queue.py` | 183 | BA 后续授权的持久队列 |
| `paypal_protocol.py` | 164 | 重定向解析 + 传输辅助 |
| `paypal_fingerprints.py` | 24 | 指纹常量 |

### 1.3 链接/对账 `sms_tool/paypal_link/`

| 模块 | 行 | 自称职责 |
| --- | --- | --- |
| `gen_link.py` | 1402 | 链接生成（`gen_pp_link.py` 机械拆分，body 未改） |
| `reconciliation.py` | 1305 | 对账（`paypal_reconciliation.py` 机械拆分，body 未改） |
| `__init__.py` | 296 | 再导出 |

### 1.4 兼容壳

| 模块 | 行 | 说明 |
| --- | --- | --- |
| `paypal_auto.py` | 27 | 指向 `paypal/` 包的兼容 shim |
| `paypal_reconciliation.py` | 7 | 指向 `paypal_link/reconciliation` 的薄壳 |

---

## 2. 边界问题（按严重度）

### 🔴 P0 — `paypal_extract.py` 已变成**事实上的支付协议共享库**

它被**非 PayPal 泳道**直接依赖：

| 依赖方 | 取的符号 |
| --- | --- |
| `sms_tool/upi_link/extract.py`、`session.py`、`verify.py`、`pipeline.py` | `_new_session`（+ `pipeline` 还要 `CURRENCY_MAP`） |
| `sms_tool/accounts/account_payment_eligibility.py` | `CURRENCY_MAP`（文档明写它是「canonical country→currency map」） |
| `sms_tool/payment_capability.py` | `PPLinkExtractor` |
| `sms_tool/paypal_link/gen_link.py`、`__init__.py` | `_checkout_get` 等 |
| `sms_tool/sentinel/client.py`（注释） | 记 `_create_checkout` / `_checkout_post` 是 Sentinel 对的挂载点 |

⇒ 一个**以 PayPal 命名**的模块，装着 UPI 泳道也需要的会话工厂、币种表和 Checkout 请求原语。
这是典型的「共享内核被埋在某个具体实现里」：任何 UPI 改动都要 import PayPal，方向是反的。

### 🟡 P1 — 浏览器泳道依赖协议泳道

`sms_tool/paypal/orchestrator.py` 模块级 `from ..paypal_reverse import try_reverse_pay`。
「策略选择」让浏览器编排器直接握住协议泳道的入口，于是两条泳道在 import 图上互指，
`paypal/` 无法被单独测试或替换。

### 🟡 P2 — 代理包装链多一跳

`paypal_proxy.py` 自己定义 `normalize_proxy_url` / `redact_proxy_url` / `rotate_proxy_session`，
但它们的上游是 `phone_proxy`（`from .phone_proxy import normalize_proxy_url as _normalize_proxy_url, ...`），
而不是 `proxy_entry`。`docs/architecture.md` 的 Boundary Rule 13 写的是「`phone_proxy` 与
`paypal_proxy` 都是 `proxy_entry` 的薄包装」—— 实际是 `paypal_proxy → phone_proxy → proxy_entry`，
**包装了一层包装**。812 行里真正属于 PayPal 的部分（探测结果、轮换策略）与
属于通用代理的部分没有分开。

### 🟢 P3 — 命名与兼容壳

- 两个 `reconciliation`：`paypal_reconciliation.py`（7 行壳）与 `paypal_link/reconciliation.py`（1305 行实现）。壳本身是**已记录的决策**，不是缺陷；但名字仍然混淆。
- `paypal_auto.py`（27 行）是 `paypal/` 包的兼容 shim。
- `paypal_fingerprints.py`（24 行）只是一个常量模块，却挂在顶层。

---

## 3. 目标布局

原则：**共享内核中性化、两条泳道单向依赖、代理只包 `proxy_entry`。**

```text
sms_tool/
  payment_wire.py            # 新增：中性命名。_new_session / CURRENCY_MAP /
                             #   _checkout_get / _checkout_post / _create_checkout
                             #   （含 Sentinel 对挂载点）—— 不出现 "paypal" 词汇
  paypal/
    __init__.py              # 门面（保持现有再导出）
    errors.py
    fingerprints.py          # ← paypal_fingerprints.py
    protocol.py              # ← paypal_protocol.py
    proxy.py                 # ← paypal_proxy.py，直接包 proxy_entry
    authorization.py         # ← paypal_authorization.py
    authorization_queue.py   # ← paypal_authorization_queue.py
    reverse.py               # ← paypal_reverse.py（协议泳道自动支付）
    extract.py               # ← paypal_extract.py，**只剩** PPLinkExtractor 与 PayPal 专属流程
    browser/                 # ← 现 paypal/ 的浏览器部分
      __init__.py, orchestrator.py, flow_steps.py, form_steps.py,
      dom_fields.py, session.py, config_picker.py
  paypal_link/               # 保持不变（gen_link + reconciliation 已机械拆分）
```

**分层规则（新增 Boundary Rule）**：

1. `payment_wire.py` 不 import 任何 `paypal*` —— 它是所有支付泳道的**下界**。
2. `paypal/browser/*` 不 import `paypal/reverse.py`；协议策略经**显式注入**（调用方传 callable）进入编排器。
3. `paypal/proxy.py` 只 import `proxy_entry`；`phone_proxy` 与它平级，不再互相包装。
4. `paypal_link/` 只 import `paypal/` 的公开门面与 `payment_wire`，不 import 顶层 `paypal_*`。

---

## 4. 迁移顺序（每步独立可验证，禁止合并批次）

| 步 | 内容 | 行为变更 | 门禁 |
| --- | --- | --- | --- |
| **S0** | 把 P0 的共享原语搬到 `payment_wire.py`，**并在 `paypal_extract.py` 保留同名再导出** | 无 | 全量 pytest + import-layer / delayed / unused 三个 ratchet |
| **S1** | 逐个迁移调用方到 `payment_wire`（先 `upi_link/*`，再 `accounts/account_payment_eligibility`，最后 `paypal_link/*`），每迁一个删一条 S0 的再导出 | 无 | 同上；`unused_import_ratchet` 会逼出没迁干净的再导出 |
| **S2** | 断开 `orchestrator → paypal_reverse`：把「策略选择」改为调用方注入的 callable | 无（纯重构） | 全量 pytest；`paypal/orchestrator` 的单测要能只装浏览器 seam |
| **S3** | `paypal_proxy` 直接包 `proxy_entry`，把通用代理部分与 PayPal 专属部分分开 | 无 | `tests/test_registration_proxy_scheme.py` / `test_proxy_registry.py` + 全量 |
| **S4** | 物理搬家到第 3 节的布局（`git mv` + 兼容壳） | 无 | 全量 + `docs_consistency_scan` + 目录表 |

**S0/S1 是收益最大、风险最低的一步**：它把「UPI → PayPal」这条反向依赖变成「两条泳道 → 中性内核」，
且因为保留再导出，任何一步都能单独回滚。

---

## 5. 风险与约束

- **测试按模块路径 patch**：仓库里大量 `patch("sms_tool.<module>.<symbol>")`。物理搬家（S4）必须
  同步改这些字符串，且不能只靠 `__init__` 再导出兜底 —— `patch` 打的是**定义模块**。
- **`paypal_link/gen_link.py` / `reconciliation.py` 的 body 是机械拆分的产物**，注释明写「bodies
  unchanged」。S0–S4 **不得**顺手改它们的逻辑。
- **`paypal_extract.py` 的 `_checkout_*` 是 Sentinel 对的唯一挂载点**（`sentinel/client.py` 的注释）。
  搬动时必须把这段注释一起搬，否则下一个人会在新位置重新挂一遍。
- **顶层 `paypal_*.py` 的家族 `.gitignore` 规则**（`proxy.json*` 那一族）与本次无关，但
  `paypal_*.py` 里有凭据常量吗？S0 前先跑一次 `sensitive_field_scan`。
- **不要在 S0 同时改命名**：`paypal_extract` 在被 UPI 依赖期间改名，等于把两个变量（位置、名字）
  混在一次改动里。

---

## 6. 明确不做

- **不合并 `gen_link.py` / `reconciliation.py`**：两者是 2026-09 机械拆分的产物，合并会重开
  一次 2700 行的评审面。
- **不把 `paypal/` 与 `paypal_link/` 合成一个包**：前者是浏览器泳道，后者是链接/对账关注点，
  合并只会再造一个 7k 行的包。
- **不改 `paypal_extract.PPLinkExtractor` 的公开契约**：`payment_capability` 依赖它做支付资格探测；
  换契约需要先有该探测的受控对照。
- **不引入新的第三方依赖**（`paypal_proxy.py` 已直接 `import requests`，与仓库其余部分的
  `curl_cffi` 不一致；这属于独立议题，见 §7）。

---

## 7. 顺带记录（本轮不改）

- `paypal_proxy.py` 直接 `import requests`，而仓库其余 HTTP 走 `curl_cffi`。两条传输栈意味着
  TLS 指纹与代理语义不统一 —— 值得单独立项，但**不要**混进本次边界重构。
- `paypal_proxy.py` 缺模块 docstring（19 个 PayPal 模块里唯一一个）。

---

## 8. 相关

- `docs/directory-map.md` — PayPal 家族当前的目录归属行
- `docs/architecture.md` — Boundary Rule 13（代理唯一权威）与依赖方向
- `plan-2026-09-17-protocol-payment-extractor-consolidation.md` — `services/protocol-payment/` 的
  提取器合并立项（**不同议题**：那是子进程提取器，本文是 `sms_tool/` 内的 PayPal 家族）
