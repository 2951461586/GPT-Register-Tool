# ADR-0011: 代理脱敏 helper 的归属，与 `paypal_proxy` 包装链的取舍

- Status: Accepted
- Date: 2026-10-08

## 决定

`normalize_proxy_url` / `redact_proxy_url` / `redact_proxy_text` 三个 helper 的
**定义模块是 `phone_proxy`，不是 `proxy_entry`**。`paypal_proxy` 对它们的包装
（`:17` 模块级 import，`:63` / `:59` / `:69` 三个薄壳）**保留**。

「把 `paypal_proxy` 的代理包装链收一跳、改为直接依赖 `proxy_entry`」这一提案
**不予采纳**。若日后重提，必须先作为 Rule 13 与 Rule 19 的联合变更立项。

## 为什么这条需要记录

一个读者（或一次架构扫描）看到 `sms_tool/paypal_proxy.py:17`

```python
from .phone_proxy import normalize_proxy_url as _normalize_proxy_url, ...
```

会很自然地推断这是**冗余中转**：既然 `docs/architecture.md` 的 Boundary Rule 13
写「`phone_proxy` 和 `paypal_proxy` 是 `proxy_entry` 的 thin wrapper」，那么
`paypal_proxy → phone_proxy → proxy_entry` 就该能压成 `paypal_proxy → proxy_entry`。
`docs/audits/plan-2026-10-08-paypal-family-boundaries.md` 的 P2 正是这样写的。

**这个推断不成立**，理由如下（2026-10-08 逐调用点核对）。

## 证据

### 1. Rule 13 的单一权威名单不包含这三个 helper

Rule 13 的权威名单是 `parse_proxy` / `rebuild_proxy_credentials` /
`retarget_region` / `rotate_session` / `infer_region`，这五个**只**由
`proxy_entry` 定义。三个脱敏 helper **不在名单内**：

| helper | 定义处 | `proxy_entry` 是否定义 |
| --- | --- | --- |
| `normalize_proxy_url` | `sms_tool/phone_proxy.py:103` | ✗ |
| `redact_proxy_url` | `sms_tool/phone_proxy.py:130` | ✗ |
| `redact_proxy_text` | `sms_tool/phone_proxy.py:154` | ✗ |

⇒ 「收一跳」不是删一行 import，而是**把三个定义搬进 `proxy_entry`**。这是
Rule 13 权威名单的扩张，不是一次清理。

### 2. 搬运会撞上 Rule 19 的有意同名分叉

Rule 19 已把 `redact_proxy_url` 记为**有意分叉**：

| 定义处 | 元数 | `empty_placeholder` |
| --- | --- | --- |
| `phone_proxy.py:130` | 2（含默认参数） | `"DIRECT"` |
| `paypal_proxy.py:63` | 1 | 显式传 `"DIRECT"` |
| `accounts/account_liveness.py:41` | 1 | 显式传 `""` |

`phone_proxy.redact_proxy_url` 的 docstring 自己写明了这个参数的存在理由：

> ``empty_placeholder`` lets callers signal a missing proxy (``"DIRECT"`` for
> paypal-proxy paths, ``""`` for phone-registration paths).

即：**`phone_proxy` 已经是那个共享实现**，`paypal_proxy` 的 1 元壳存在的价值是
「把默认值固定成 `DIRECT` 并保留历史导入路径」（`:64` 的 docstring 原文：
`Canonical (phone_proxy); preserved here for historical importers.`），
`account_liveness` 的 1 元壳则固定成 `""`。两条泳道对「没有代理」的日志语义
**不同**，这正是 Rule 19 要求不得在无新证据下合并的那一类。

### 3. 真实的链长比提案描述的更长，且同文件内不一致

`sms_tool/paypal_extract.py` 同时以两种方式取这两个 helper：

| helper | 取用路径 | 跳数 |
| --- | --- | --- |
| `normalize_proxy_url` | `paypal_extract.py:36` → `.phone_proxy` | 1 |
| `redact_proxy_url` | `paypal_extract.py:88` → `.paypal_proxy`（`:92`）→ `.phone_proxy` | **3** |

所以真实情况是：`redact_proxy_url` 走
`paypal_extract → paypal_proxy → phone_proxy`，**`proxy_entry` 全程不参与**。
提案说的「`paypal_proxy → phone_proxy → proxy_entry` 多一跳」把归属搞错了。

## Consequences

- **`paypal_proxy` 的包装链保留**，它不依赖 `proxy_entry` 中转，而是消费
  `phone_proxy` 定义的真实实现。Rule 13 的散文对它保持沉默是**正确**的
  （Rule 13 管的是那五个解析/改写函数，脱敏不在其列）。
- **`paypal_extract.py:88` 经由 `paypal_proxy` 取 `redact_proxy_url` 这件事，
  本次不动**。它是一条可消除的 3 跳，但消除它属于「统一脱敏取用路径」，
  与「收 `paypal_proxy` 的包装链」是两件不同的事，应独立立项。
- **本 ADR 不覆盖传输栈**：`paypal_proxy.py:14` 的模块级 `import requests` 与
  `:188-205` 的 `curl_cffi → requests` 回退是**有意**的（该函数 docstring 原文：
  `Falls back to requests when curl_cffi is unavailable.`）。它是全仓支付模块里
  唯一一处 plain `requests`，但它是探测回退而非业务传输，不得以「统一传输栈」
  为名删掉。若日后要统一，需单独评估「curl_cffi 不可用时探测是否应直接失败」。
- 未来架构评审**不应**再把这条列为解耦机会。若确要收敛，正确的前置动作是：
  ① 把三个 helper 的定义迁入 `proxy_entry`（Rule 13 扩张）；
  ② 重新评估 `account_liveness` / `paypal_proxy` 两个 1 元壳的默认值分叉（Rule 19）；
  ③ 顺带统一 `paypal_extract` 的取用路径。三者必须同一批完成，否则只增混乱。

## 相关

- `docs/architecture.md` Boundary Rule 13（代理字符串单一权威）、Rule 19（同名函数非重复）
- `docs/directory-map.md` 的「Proxy authority, lanes, health and bridge」行
- `docs/audits/plan-2026-10-08-paypal-family-boundaries.md` P2（本 ADR 驳回其结论）
- `sms_tool/phone_proxy.py:103,130,154`（定义）· `sms_tool/paypal_proxy.py:17,59,63,69`（包装）
- `sms_tool/accounts/account_liveness.py:41`（另一条泳道的 1 元壳）
