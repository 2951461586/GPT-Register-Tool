# 落地记录：P0-B S0 + S1（流程内挑战判据，只读）（2026-10-07）

> 落地来源：`docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` §6 的 **S0 + S1**。
> 两者按计划都是**零控制流变更**：只给 403/429 的**失败名**加一个后缀，再加三条不变式的单测。
>
> **S2（换出口）/ S3（线上 A/B）/ S4（浏览器交棒）未动**，`rotate_exit` / `handoff` /
> `handoff_resume` 三个开关**尚未存在**（S2 才引入），`p0-2b-inflow-challenge-handoff`
> 实验也**尚未注册**（属 S3 的预注册动作）。

---

## 1. 落地内容

### S0 — 只读判据 + 配置键 + 错误串后缀

**判据**：`sms_tool/proxy_edge_probe.edge_challenge_verdict(response)` → 三态
`challenge` / `not_challenge` / `unknown`。纯函数、全定义（不抛异常、不改状态）。三条契约：

1. **只有 403/429 回答 yes/no**，其余状态码一律 `unknown` —— 成功的 OTP 响应体里合法地
   含 `challenge` 字样（`auth_flow/otp.py` 的 `login_challenge` 事务臂），文本匹配会把
   OTP 概念误判成出口级挑战。
2. **判据复用 `classify_edge_response`**，不新写 host/header 匹配。该探针自己的契约是
   "登录边界上的任何 403 都是拒绝（可达但对注册无用）" ⇒ `BLOCKED` 就是"该换出口"的信号。
   代价：裸 403 读作 `challenge`；403 读作 `not_challenge` 的**唯一**情形是账号已废。
3. **先排除账号已废**（`accounts/account_terminal.py` 是词汇的唯一 owner）：换出口改变不了
   `account_deactivated` 的答案。顺序与 SunnyRegister `_is_challenge_response`
   （`protocol_auth.py:609`）一致。

429 无需特判：普通限流被 `classify_edge_response` 判 `CLEAN`（源站答了）⇒ `not_challenge`，
继续归 `rate_limit` 处置者；带 CF 挑战标记的 429 读作 `challenge`，**仅作观测** ——
§3.5 禁止控制流在 `rate_limit` 类上换出口。

**配置键**：`registration.edge_challenge_discrimination`（默认 **true**）。它只决定错误名是否
带后缀，没有控制流读它。默认开：错误名是离线 A/B 判断"到底有没有发生挑战"的**唯一**数据源，
一个关着发布的观测测不到任何东西；而后缀猜错只丢一行日志，不丢邮箱。

**后缀**：`SessionCircuitOpen` 的消息尾部追加 `:edge_challenge`：

```text
session_circuit_open:http_403:retry_after=900s:edge_challenge
```

前导 `session_circuit_open` token 不移动 ⇒ `failure_registry.classify_error` 分类与终结性
完全不变（不变式 3）。后缀写在**末尾**（字面意义上的"后缀"），`retry_after=Ns` 的形状不变。

### S1 — §3.5 三条不变式，各一条变异用例

| # | 不变式 | 用例 | 变异会被抓住的情形 |
|---|---|---|---|
| 1 | 熔断只在挑战路径**失败之后**写入，绝不提前 | `test_a_challenge_labelled_403_still_opens_the_circuit` + `test_the_read_only_helper_does_not_touch_the_circuit` | 用判据去**抑制**熔断写入（S0 只许改名，不许改控制流）；或把只读判据变成控制流决策 |
| 2 | 换出口后必须 `clear_session_circuit`，**含挑战标记** | `test_clear_session_circuit_resets_the_challenge_label` + `test_a_cleared_circuit_labels_the_next_403_freshly` | 只清 `blocked_until`/`retry_after` 而留下旧 `edge_challenge` ⇒ 下一个出口继承一个从没发生过的挑战，A/B 的 `edge_challenge_count` 会虚高 |
| 3 | 挑战路径**不新增失败类别** | `test_the_suffix_does_not_change_the_failure_class` + `test_the_suffix_does_not_change_terminality` + `test_no_failure_class_owns_an_edge_challenge_marker` | 把 `edge_challenge` 注册成失败类/标记（违反"一件事一个处置者"），或让后缀改变分类/终结性 |

---

## 2. 与计划的两处偏差（都有硬约束背书）

**① 判据落在 `proxy_edge_probe.py`，不在计划建议的 `auth_state.py`。**
`auth_state.py` 模块级 import `http_client`（`sms_tool/auth_state.py:11`），而
`http_client` 是 `session_circuit_open` 这个名字的 owner（后缀要挂在那里）——反向 import
就是环。判据因此与 `classify_edge_response` 同处（也正合 §3.2 规则 2"复用判据"）。
`auth_state` 的 re-export 留到 S2：现在没有消费者，提前加就是一条 unused import，
而 `scripts/unused_import_ratchet.py` 的存在就是为了挡住这种"先放着"。
S2 的接线点是 `auth_flow/deps.py` **既有的** `from ..auth_state import (...)` 语句
（加名字不增加跨目录边；新开一条 `from ..proxy_edge_probe import ...` 会 +1）。

**② 后缀经 `http_client` 的熔断名落地，而不是经 `auth_flow` 步骤。**
`session_circuit_open` 这个名字是 `http_client` 在 403/429 时写进熔断状态、
在**下一次**请求由 `_raise_if_circuit_open` 抛出的；`auth_flow` 只是让它穿过。
`auth_flow` 侧的消费者随 S2 的 `rotate_exit` 到来。

**代价（已显式记账）**：`edge_challenge_verdict` 需要 `account_terminal` 的停用词汇。
模块级 import 会让 `sms_tool -> sms_tool/accounts` 这一对被 `scripts/import_layer_ratchet.py`
冻结的边数从 9 涨到 10；该 ratchet **自己的失败提示**把"改成函数内 import"列为合法补救，
`payment_auth` / `payment_batch` / `proxy_routing` / `cli` 对同一个包就是这么做的。因此：

- `scripts/delayed_import_baseline.json`：`sms_tool/proxy_edge_probe.py` 1 → 2，总数 419 → 420；
- `tests/test_import_layer_ratchet.py` 钉的 `delayed_cross_dir_edges` 184 → 185
  （连同它的注释，写明为什么涨）。

两处都只涨这 1，且 ratchet 的输出把"哪个文件涨了"打出来 —— 这正是它存在的目的。

---

## 3. 验证证据（本轮）

| 门禁 | 结果 |
|---|---|
| `pytest -q`（全量） | **5983 passed, 8 skipped** |
| `ruff check` + `ruff format --check`（本轮改动文件） | All checks passed / already formatted |
| `scripts/import_layer_ratchet.py` | OK（288 module 边 / 31 对 / 11 mutual / 60 minority；**185 delayed**） |
| `scripts/delayed_import_ratchet.py` | OK: 420 ≤ baseline 420 |
| `scripts/config_key_ratchet.py` | OK（`registration=36`, `email_registration=33`） |
| `scripts/bare_print_ratchet.py` | OK（514，未新增裸 print） |
| `scripts/unused_import_ratchet.py` | OK |
| `scripts/docs_consistency_scan.py` | passed |
| 新增离线测试 | `tests/test_edge_challenge_verdict.py`（**27 例 / 24 子测试**：真值表 + 全定义性 + 配置门 + 后缀 + 三条不变式） |

---

## 4. 明确未做

- **S2 `rotate_exit`**（同池换出口一次 + `proxy_audit` 前后出口 + 单槽池短路）：未动。
  它改控制流，必须先有本节的不变式单测。
- **S3 线上 A/B**：未跑；`p0-2b-inflow-challenge-handoff` 也**尚未注册**进
  `scripts/registration_ab.py`（属 S3 的"跑之前先注册"）。
- **S4 浏览器交棒**：未动；§7 的未决问题 1b/2/3/4/5 仍未答。
- **`auth_state` re-export**：未加（无消费者，会成 unused import），留到 S2。
- **未改任何控制流**：403/429 仍写熔断，仍不换出口，仍不交棒。唯一的行为差异是
  **错误名多了一个后缀**，且可用 `registration.edge_challenge_discrimination=false` 完全复原。

---

## 5. 相关

- `docs/audits/plan-2026-10-05-inflow-challenge-handoff.md` — 契约、开关、A/B 设计、落地顺序
- `docs/current/protocol-registration.md` — "In-flow edge-challenge judgement" 一节（判据/后缀/不变式）
- `sms_tool/proxy_edge_probe.py` — `edge_challenge_verdict`
- `sms_tool/http_client.py` — `edge_challenge_discrimination_enabled` / `SessionCircuitOpen`
- `tests/test_edge_challenge_verdict.py` — S0 + S1 的全部断言
