# 扫描 2026-09-22：代码耦合 / 架构设计 / 目录结构 / 文档规范

> **性质**：扫描阶段只读；**同日落地阶段有代码改动** —— 各处标 `✅ 已落地 09-22` 的条目即改动面。
> **基线**：HEAD `7b4a4e1`，**扫描时**工作树干净（`git status --short` 为空）。
> **与既往审计的关系**：本仓已有 19 条可执行边界规则（`docs/architecture.md`）、
> 8 道门禁、4 个 ratchet。本轮**不重复已决策项**，并对第 6 轮（`audit-2026-09-02-round6-architecture-hygiene.md`）
> 与同轴扫描（`scan-2026-09-09-round3-architecture-coupling-docs.md`）的结论做了**两处纠正**（见 §7）。
> 探针脚本在 `runtime/tmp/`（`a7_*` 前缀），未入库。

---

## 0. 门禁基线（全绿，无新增回归）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 架构守卫 | `scripts/architecture_scan.py` | `Architecture scan passed`（0 warning） |
| 文档一致性 | `scripts/docs_consistency_scan.py` | `passed (release-v2026.09.14.md)` |
| 配置 schema | `scripts/config_schema_check.py` | `passed` |
| IPC schema | `scripts/ipc_schema_check.py` | `passed` |
| 提取器同构 ratchet | `scripts/extractor_parity_report.py` | `OK (ideal:blik=66, ideal:twint=97, momo:kakao=0, twint:blik=60)` |
| 配置键 ratchet | `scripts/config_key_ratchet.py` | `OK (registration=30, email_registration=33)` |
| bare-print ratchet | `scripts/bare_print_ratchet.py` | `OK (523 bare prints, baseline frozen)` |
| 延迟导入 ratchet | `scripts/delayed_import_ratchet.py` | `OK: 406 <= baseline 406` |
| 行尾守卫 | `scripts/line_ending_guard.py` | `no mixed line endings` |

**结论：四个轴都没有新增回归。** 下列条目全部属于「已知但未推进」或「本轮新测出」。

补充核实：三个**不在** `ci.yml` 里的 ratchet（`bare_print` / `delayed_import` / `config_key`）
**已由 pytest 接线**（`tests/test_operator_output.py`、`tests/test_delayed_import_ratchet.py`、
`tests/test_config_key_ratchet.py`），因此随 CI 的 pytest 步骤执行 —— **不是**「门禁存在但从不运行」，
本轮驳回该假设。

**09-22 落地后新增第 10 项**：`scripts/unused_import_ratchet.py`（逐文件 F401 基线，**只禁增长**），
同样由 pytest 接线（`tests/test_unused_import_ratchet.py`，7 项）。
它**不替换**任何现有门禁，补的是「`F401` 无任何自动化会检」这个缺口（§5.3）。

**全量验收（回滚 + ratchet 状态）**：`pytest -q` → `4718 passed, 6 skipped, 842 subtests passed`（**0 failed**）。
相对当日早先基线 `4705` 的 **+13** = 本次新增的 13 项测试
（ratchet 7 + `tests/test_audits_readme_index.py` 2 + `tests/test_source_hygiene.py` 4）。

---

## 1. 结论摘要（按性价比排序）

| # | 轴 | 结论 | 证据 | 成本 |
|---|---|---|---|---|
| A1 | 耦合 | `account_recovery` 兼容壳只被 6 个文件消费，却制造了 SCC-8 并迫使 `recovery_batch` 在**函数体内 import 25 个名字** | §3.3 | 小（6 文件改导入路径） · ✅ **已落地 09-22**：SCC 8→7，残留 0，ruff 全过 |
| A2 | 文档 | `docs/audits/README.md` 自述「31 files」，实际 38 行 / 39 个跟踪文件 | §5.1 | 极小（1 行） · ✅ **已落地 09-22**：计数改为 **40**；并新增 `tests/test_audits_readme_index.py` 钉住（漂移从「无人可见」变成「测试失败」，见 N7） |
| A3 | 文档 | 13 个库模块在任何 `.md` 中从未出现（含 `cli_parsers/`、`geo/`、`pay_link/` 三个整包） | §5.2 | 小 · ✅ **已落地 09-22**：三个包已补模块组行，**并补上原报告漏记的 `store/` 整包**；未文档化模块探针 **13 → 0** |
| A4 | 架构 | `sms_tool/upi_link.py`（2914 行，sms_tool 最大模块）**不在**任何拆分/合并计划范围内 | §4.2 | 中 · ✅ **测量已落地 09-22，结论是「不立项拆分」**（零改动同构度探针，详见 §4.2） |
| A5 | 死代码 | 23 个零引用模块级定义；其中 9 个是 blik/ideal/twint 提取器三胞胎 | §6 | 小 · ✅ **已落地 09-22**：实删 **10** 项；blik 4 项**有意不删**（属 `plan-2026-09-17` §2.5 未决的 Q1，删 = 替架构决策投票） |
| ~~A6~~ | 架构 | ~~C# 第 6 轮死成员抽样 16 项，10 项 20 天后仍 def-only~~ → 🔴 **落地阶段推翻**：第 6 轮当天已全删，文件里只剩墓碑注释；我的「数命中次数」判定把注释当成了定义 | §4.3 · §7.3 | 无（无可落地） |
| A7 | 文档 | 第 6 轮把「门面冗余导入」13 项**明确留待单独一轮**，至今未开 | §5.3 | ✅ **已落地 09-22（以 ratchet 形式）**：原案「`select` 加 `F401` 要求 456 → 0」**被两次实测证伪**（判定器无法穷举消费面，3 → 6 → 8 通道，每轮代价一次全量回滚）；`per-file-ignores` 亦被否决。最终落地 `scripts/unused_import_ratchet.py` + 逐文件基线（冻结 456 处 / 84 文件，**只禁增长、零删除**） |
| A8 | 目录 | `local/` 是顶层目录但不在 `docs/directory-map.md` 顶层表内 | §2.2 | 极小 · ✅ **已落地 09-22**（随 A3 一起补） |
| — | 耦合 | 8 个 SCC **全部**由懒加载/门面断开，**0 个 import-time 环** | §3.1 | — |

---

## 2. 目录结构

### 2.1 物理布局盘点（git 跟踪面）

| 顶层 | 跟踪文件 | 说明 |
|---|---|---|
| `tests/` | 307 | 含 C# 测试 `tests/SmsWorkbench.Tests/` |
| `sms_tool/` | 243 | |
| `docs/` | 95 | |
| `SmsWorkbench/` | 79 | |
| `scripts/` | 52 | |
| `services/` | 33 | |
| `SmsWorkbench.Contracts/` | 14 | |
| 根文件 | 26 | |

合计 **849** 个跟踪文件（其中 `.py` **573**，生产面 `sms_tool/`+`services/`+`scripts/` 共 **304**）。
被忽略目录（`runtime/`、`sessions/`、`dist/`、`.venv/`、`.dotnet/`、`__pycache__/`）均**未跟踪**，
与 `docs/directory-map.md`「Runtime and generated directories」表一致 —— **目录纪律本身是干净的**。

`config.json` / `proxy.json` / `runtime.json` / `payment.json` / `mailbox_tokens.txt` / `session.json`
均**不在**跟踪面内（只在工作区存在）。凭据面没有泄漏进 git。

### 2.2 A8：`local/` 未被目录表收录

`git ls-files local/` → 仅 `local/README.md`。`docs/directory-map.md` 的
「Top-level source directories」表（6 行：`sms_tool/`、`SmsWorkbench/`、`services/`、
`tests/`、`docs/`、`scripts/`）**没有 `local/`**。

同样未在该表出现的还有 `.githooks/`、`.github/`。属**低危文档缺口**：
目录表自称是「物理放置」的真源，却漏了三个真实存在的顶层目录。

**✅ 已落地 09-22**：顶层表已补 `local/`、`.githooks/`、`.github/` 三行（随 A3 一起补）。

---

## 3. 代码耦合

### 3.1 SCC 分类：8 个静态环，0 个 import-time 环

按「每个 alias 一条边」建图（`from pkg import a, b` 记两条），Tarjan 求 SCC，
再逐边判定边性质（模块级 / `TYPE_CHECKING` / 函数体内懒加载）：

| SCC | 模块 | 模块级边 | 断环的懒加载边 | 判定 |
|---|---|---|---|---|
| 1 | 9 个：`storage`(8 行门面) + `store/*`(6) + `accounts.account_events` + `providers.mailbox_remail` | `storage`→`store`（`from .store import *`） | `store/connection.py:57,89` → `sms_tool.storage`（**有意**，见 §7.1）；`store/accounts.py:209`→`account_events`；`account_events:19`→`mailbox_remail`；`mailbox_remail:257`→`storage` | 有意维持 |
| 2 | `account_liveness` ↔ `browser_flow`(orchestrator/session) ↔ `registration_outcome` | `orchestrator:17`→`session`；`orchestrator:20`→`registration_outcome`；`session:12`→`account_liveness`；`registration_outcome:17`→`account_liveness` | `account_liveness:102`→`browser_flow`；`browser_flow/__init__:42` 的 `__getattr__` + `:23` `TYPE_CHECKING` | 靠懒加载断开，**但分层观感不佳**（见 §3.2） |
| 3 | `checkout_contract` ↔ `payment_catalog` ↔ `payment_flow` | `checkout_contract:10`→`payment_catalog`；`payment_flow:9`→`payment_catalog` | `payment_catalog:193,194`（仅 `validate_catalog_consistency()` 内） | 校验专用反边，可接受 |
| 4 | `payment_routing` ↔ `paypal_proxy` | 无 | 双向**全部**在函数体内（4 + 1 处） | 静态环、运行期无环 |
| 5 | `sentinel.client` ↔ `sentinel_tokens` | 无 | 双向**全部**在函数体内 | 同上 |
| 6 | `mailbox` ↔ `mailbox_strategies` | `mailbox:68`→`mailbox_strategies` | `mailbox_strategies` 11 处在函数体内 | **Rule 16 已 ratchet 的 hub 模式**，预期 |
| 7 | `registration_finalize` ↔ `registration_handlers` | `registration_handlers:38`→`registration_finalize` | `registration_finalize:110`→`_login_probe_password` | 断环合理 |
| 8 | `accounts.account_recovery` ↔ `accounts.recovery_batch` | 无 | `account_recovery:1313` `__getattr__`；`recovery_batch` 5 个函数体内共 **31 处**导入 | 🔴 **A1，见 §3.3** |

**关键判据**：SCC 4/5/8 的两个方向**全部**是函数体内导入 ⇒ 静态图有环、`import` 期无环。
SCC 1/2/3/6/7 有一个模块级方向 + 一个懒加载反边 ⇒ 同样在 import 期无环。
**所以「8 个循环依赖」这句话本身是错的** —— 正确表述是「8 组静态边互指，全部由懒加载或门面断开」。

### 3.2 SCC 2 的分层观感问题（低危，仅记录）

`registration_drivers/browser_flow/session.py:12` 从**账号测活层**
（`accounts.account_liveness`）导入 `CODEX_USAGE_URL`、`account_chatgpt_id`、
`quota_result_from_payload`。按 `docs/architecture.md` 的分层
（UI → CLI → workflow → domain contracts → provider/persistence adapters），
「浏览器注册驱动」依赖「账号测活」属于**同层横跳**，而非向内依赖。

不改的理由：三个符号里两个是纯数据/纯函数（URL 常量、payload 解析），
且 `registration_outcome:17` 也直接依赖 `probe_account_liveness`。
真要收敛需要把 `CODEX_USAGE_URL` / `quota_result_from_payload` 下沉到无依赖词汇层
（`promotion_states.py` / `account_terminal.py` 是现成先例），
但收益低于改动面 —— **记录，不建议本轮动**。

### 3.3 🔴 A1：`account_recovery` 兼容壳只被 6 个文件消费，却逼出 31 处函数体内导入

**现状**：

- `sms_tool/accounts/account_recovery.py:1298-1316` 有 `_BATCH_EXPORTS`（9 个名字）
  与 PEP 562 模块级 `__getattr__`，把调用转发到 `recovery_batch`。
- `sms_tool/accounts/recovery_batch.py` 反过来在 **5 个函数体内共 31 处**
  从 `account_recovery` 导入 25 个名字（`refresh_local_quota_statuses` 内 25 个、
  `_probe_liveness_with_retries` 内 5 个、另 3 处单发）。

**消费面（全仓检索，含多行 import）**：

| 消费方 | 形式 | 性质 |
|---|---|---|
| `sms_tool/commands/accounts.py:247` | `from ..accounts.account_recovery import refresh_local_quota_statuses` | **生产**（应改为 `recovery_batch`） |
| `sms_tool/accounts/account_scan.py:23` | 同上（多行 import 列表内） | **生产**（应改为 `recovery_batch`） |
| `tests/test_account_recovery.py` | `account_recovery.refresh_local_quota_statuses(...)` ×约 30 处 | 测试 |
| `tests/test_cli_quota.py:48,75,96,120` | `patch("sms_tool.accounts.account_recovery.refresh_local_quota_statuses")` | 测试 |
| `tests/test_account_terminal.py:50` | `from ...account_recovery import _prune_liveness_snapshots` | 测试 |
| `tests/test_heavy_lane_slots.py:20` | `from ...account_recovery import _heavy_lane_slots` | 测试 |

**为什么值得做**：删掉 `__getattr__` 后，`account_recovery` 不再（惰性地）依赖 `recovery_batch`，
**SCC-8 消失**。

**✅ 已落地 09-22**：`commands/accounts.py:247` 与 `accounts/account_scan.py:23` 的导入已改到
`recovery_batch`，4 个测试模块已重定向，`account_recovery.py:1298-1316` 的
`_BATCH_EXPORTS` + `__getattr__` 已删。
**验收实测**：SCC **8 → 7**；`git grep 'account_recovery\.\(refresh_local_quota_statuses\|_prune_liveness_snapshots\|_heavy_lane_slots\)'`
只剩注释；全量 pytest 计数不降。

**风险与已排除项**：

- 改动是**显式的**：漏改的消费点会 `AttributeError` 而非静默走错路径。
- 生产只有 2 个文件要改，测试 4 个 —— 消费面**可枚举、可验证**。

🔴🔴 **但本节原计划的最后一步「把 31 处函数体内导入提到模块级」被落地阶段否决 ——
而否决它的是本节自己那条「已排除项」的漏洞。** 原判断写的是：

> 已确认**没有**测试 patch `sms_tool.accounts.recovery_batch.<内部名>`
> （`git grep 'patch("sms_tool.accounts.recovery_batch\.' tests/` 为空），
> 所以**不存在**「patch 到函数体内导入的名字上、静默失效」的假绿风险。

**这条 grep 太窄**：真实的 patch 面根本不在 `recovery_batch` 上，而在它的**上游** ——
`tests/test_account_recovery.py:1308` patch 的是 `account_recovery.CFG`。
那 31 处函数体内导入之所以存在，正是为了让调用发生在 `account_recovery.CFG`
**被读到之后**；提到模块级 ⇒ 该 patch **静默失效**（测试仍绿，但测的不再是同一件事）。
**结论：函数体内导入保留原样。**

**⚠️ 教训（已收录进 §9.25）**：**「我用某个模式 grep 过、结果为空」只证明那个模式为空，
不证明风险不存在。** 排除一条风险时必须同时写明**在哪个命名空间、用什么模式**排除的，
并反问「如果真实 patch 面在上游 / 在别名侧 / 在字符串字面量里呢」。

---

## 4. 架构设计

### 4.1 模块与函数体量

| 指标 | 数量 |
|---|---|
| 生产模块（`.py`） | 304 |
| 最大的 5 个生产模块 | `services/protocol-payment/blik/blik_qr_extract.py` 3434 · `ideal/ideal_qr_extract.py` 3184 · `twint/twint_extract.py` 3168 · `sms_tool/upi_link.py` **2914** · `momo/momo_qr_extract.py` 2046 |
| 函数体 ≥ 80 行 | **181** |
| 函数体 ≥ 150 行 | **43** |

最长的 8 个函数：

| 行数 | 位置 | 函数 |
|---|---|---|
| 580 | `sms_tool/registration_drivers/browser_flow/orchestrator.py:69` | `run_browser_registration` |
| 572 | `sms_tool/upi_link.py:2343` | `generate_upi_qr_link` |
| 493 | `sms_tool/batch_runner.py:425` | `run_batch_impl` |
| 478 | `sms_tool/payment_batch.py:81` | `run_payment_batch` |
| 424 | `sms_tool/paypal_link/reconciliation.py:301` | `reconcile_paypal_return` |
| 422 | `sms_tool/accounts/recovery_batch.py:84` | `refresh_local_quota_statuses` |
| 317 | `sms_tool/phone_registration.py:31` | `run_phone_register` |
| 313 | `sms_tool/paypal_link/gen_link.py:524` | `generate_pp_link` |

⚠ 按 `docs/architecture.md` 的既有纪律：**拆分文件不缩短函数**。
上述长度是「候选」，不是「计划」；且 `recovery_batch.refresh_local_quota_statuses`
（422 行）正是 §3.3 里那个带 25 名字体导入的函数，动它要连着 §3.3 一起做。

### 4.2 🔴 A4：`upi_link.py` 不在任何拆分/合并计划内

- `sms_tool/upi_link.py` **2914 行**，是 `sms_tool/` 内最大模块，含第 2 长函数（572 行）。
- `docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`
  全文检索 `upi` **命中 0 次** —— 该计划的覆盖面是 `services/protocol-payment/`
  的 blik / ideal / twint / kakao / momo / pix。
- 该计划的实测结论是 ideal/twint **81% 顶层函数 AST 同构**，但**明确否决**「合并为单一参数化引擎」，
  并留了 4 个待答开放问题（首要：blik 独有的 32 个代理选址函数是「特有需求」还是「ideal/twint 落后版本」）。
- `extractor_parity_report.py` 正在 ratchet 这四个 pair，且**当前全绿**。

**结论**：提取器重复度是**已决策、已被门禁守住**的项，本轮不重复报告。
真正的新缺口是 **`upi_link.py` 从未被纳入该计划的测量面**。

#### ✅ 09-22 落地：零改动测量已完成，结论是**不立项拆分**

用零改动探针（不改门禁、不改基线，只 import `extractor_parity_report` 的函数并临时扩 token 表）实测：

| 项 | 实测值 |
|---|---|
| `upi_link.py` | **2893 行 / 75 个顶层函数 / 0 个转发壳** |
| `common/`（14 个模块） | 63 个去重体指纹 |
| `upi_link` 与 `common/` 的**体级**重合 | **1 / 75** |
| `upi_link` 与 `common/` 的**名级**重合（去 `_`/`upi_` 前缀后） | 7 对 |

🔴 **先排除一个测量假象**：与 blik / ideal / twint 直接比得到 `identical=0`、`only_a=75`，
看着像「配不上对」。实因是**提取器侧已把共享实现抽到 `common/`，provider 文件只剩转发壳**
（`blik.env_bool` 已经是一行 `return common_env_bool(...)`），而 `upi_link` 的 **75 个函数里
0 个是转发壳** —— 它根本没进过那次抽取。所以「0 重合」不是假零，是**两个不同的东西**。

7 对名级重合的**逐对相似度**（`difflib` 对 `ast.unparse` 后的源码）：

| 相似度 | `upi_link` | `common/` | 行数 |
|---|---|---|---|
| **99.7%** | `_env_bool` | `protocol_core.py:env_bool` | 5 / 5 |
| **94.7%** | `_upi_find_submission_attempt` | `protocol_core.py:find_submission_attempt` | 16 / 15 |
| **83.9%** | `_upi_first_value_by_key` | `protocol_core.py:first_value_by_key` | 15 / 14 |
| 54.1% | `_upi_collect_urls` | `protocol_core.py:collect_urls` | 16 / 14 |
| 24.9% | `_upi_dump_http` | `http_dump.py:dump_http` | 46 / 23 |
| 20.3% | `_env_int` | `protocol_core.py:env_int` | 8 / 28 |
| 17.9% | `_upi_extract_redirect_url` | `protocol_core.py:extract_redirect_url` | 69 / 27 |

**判定**：

1. **不立项拆分。** `upi_link.py` 是**自足实现**，与提取器家族不共享代码 ——
   那 81% 同构的结论**不适用于它**，因为它不是那个家族的成员。原报告「建议立项」的前提不成立。
2. 真正的重复面**小而有界**：`protocol_core.py` 里 3 个 helper（`env_bool` /
   `find_submission_attempt` / `first_value_by_key`，合计约 36 行）与 upi 近乎逐字相同。
   其中 `_env_bool` **只差函数名**。
3. **但这 36 行现在不能收敛**：`extractor_parity_report.py` 的 docstring 明写
   「只有 `services/protocol-payment/` 下的文件合格，`sms_tool/` 有自己的守卫」——
   跨边界 import 会打破那条边界。**要收敛得先决策「`sms_tool/` 是否允许依赖
   `services/protocol-payment/common/`」，那是架构决策，不是清理动作。**

### 4.3 🔴 A6 结论反转：C# 第 6 轮死成员清单**已全部落地**（原判「10/16 项仍在」是我的误报）

**本条在落地阶段被推翻，保留在此以免后人重犯。**

`audit-2026-09-02-round6-architecture-hygiene.md:160-179` 列了 28 个 C# 未使用成员。
我最初用「在 `*.cs` 里数符号命中次数 + 在 `*.xaml` 里查绑定」判定，得到
「10 项仍 def-only 且无 XAML 引用 ⇒ 20 天后仍在」。**该结论是错的。**

落地阶段逐个**打开命中行**复核后，真实状态是：

| 第 6 轮列的项 | 我的初判 | 读行后的真实状态 |
|---|---|---|
| `SelectedTabIndex`、`NormalizeListText`、`AddDetailRow`、`RerunFailed_Click`、`RebuildSqlite_Click`、`ShowPaymentMethodDialog`、`RegisterFromPool_Click`、`AddRegistrationAtOnlyArgs`、`ClearHistory`、`IsValid`（10 项） | 「仍死」 | ✅ **第 6 轮当天已删**；文件里只剩一行「`// X removed (2026-09-02, round 6)`」注释 —— **那 1 次命中就是注释本身** |
| `GetQuotaStatus`、`JsonValueToObject`、`DomainLabel` | ✅ 已删 | ✅ 已删 |
| `CreateQuotaUsageProbe`、`CreateDeleteAccount`、`CreateSingleAccountImport`、`CreateRefreshSession`、`CreateViewInbox` | — | ⚠ **仍是活定义**，但第 6 轮已定性为「**仅被测试引用**的契约成员」= 产品决策，**不是死代码** |
| `MailboxArgumentForLine` | — | ✅ 已接线（生产 + 测试共 20 处引用） |
| `OpenConfig`、`OpenReport` | ⚠ 不是死代码 | ✅ 同一结论（被 `.xaml` 事件属性绑定） |
| 死参数 `RunUiTaskAsync(…, CancellationToken ct)` | — | ✅ **`ct` 已删**，并补了「No cancellation token by design」的设计说明 |
| `MainWindow.Payment.cs` 6 个方法死 5 个 | — | ✅ 该文件从 6 个方法缩到 **1 个**（37 行） |

**判定口径**（本轮修正）：

```bash
# 活定义
git grep -nE "^[[:space:]]*(public|private|internal|protected|static).*\bNAME\b" -- '*.cs'
# 注释提及（用来识别「已删但留了墓碑注释」）
git grep -nF NAME -- '*.cs' | grep -c "^[^:]*:[0-9]*:[[:space:]]*//"
```

**命中数 ≠ 活定义数**：`hits=1` 既可能是「仅定义」，也可能是「仅注释」。
不读命中行就给判定，是本轮 A6 误报的唯一原因。

**仍然成立的那半条**：`OpenConfig` / `OpenReport` 确实是第 6 轮清单里的误报（被 `.xaml`
事件属性引用）—— 即 **C# 死成员判定必须 `.cs` + `.xaml` 双面检索**。这条保留。

**A6 无可落地内容。**

---

## 5. 文档规范

### 5.1 A2：`docs/audits/README.md` 自述计数陈旧

| 量 | 值 |
|---|---|
| README 自述 | 「**31 files**」（`:42`） |
| README 实际列出的表格行（`^\| \`` 开头） | **38** |
| `git ls-files docs/audits/` | **39**（38 个 `.md` + 1 个 `.txt`） |

**条目覆盖是完整的**（39 个文件逐个检索，无一未被 README 提到），
**只有自述的计数是错的**。`scripts/docs_consistency_scan.py` 检查的是
行号引用、符号表、release 指针、`sms_tool/providers/*.py` 路径存在性 ——
**不检查任何文档的「自述条目数 vs 实际条目数」**，所以这类陈旧计数可以长期存活。

这是 `docs/audits/README.md` 自称的「Contents」索引表：一张自称是清单的表，
**行集对了、计数错了**，且没有任何门禁能发现。

**✅ 已落地 09-22**：计数行改为 **40**（39 个已跟踪 + 本报告入库后 1 个），并补了复算命令；
另新增 `tests/test_audits_readme_index.py`（2 项）把「自述计数 == 目录条目数」钉住 ——
**漂移从「无人可见」变成「测试失败」**（见 §8.1 N7）。
⚠️ **本节的原始观察仍然成立且值得保留**：`docs_consistency_scan.py` 至今不检查
「自述条目数 vs 实际条目数」，这次是靠**新增测试**补的洞，不是靠加宽那个扫描器。

### 5.2 A3：13 个库模块在任何 `.md` 中从未出现

**✅ 已落地 09-22**：`cli_parsers/`、`geo/`、`pay_link/` 三个包已在 `directory-map.md` 补模块组行；
**并顺带补上原报告漏记的 `store/` 整包（8 个模块）** —— 比本节的「13 个模块」范围更大。
**验收实测**：未文档化模块探针 **13 → 0**；`scripts/docs_consistency_scan.py` 绿。
⚠️ 但 `store/environment_ledger.py` 是 `f987742` 当天新增的，**下一次新增模块仍会重现本节问题** ——
本节记录的是「文档滞后于代码」这个**机制**，不是一次性缺口。

对 266 个库模块逐个在全部 95 个跟踪 `.md` 中检索文件名：

| 模块 | 备注 |
|---|---|
| `sms_tool/cli_parsers/` 整包（`codex.py`、`quota.py`、`sub2api.py`） | 新拆出的 CLI 解析层，`docs/directory-map.md` 的「Entrypoints/config」行仍只列 `cli.py` |
| `sms_tool/geo/` 整包（`clock.py`、`profiles.py`、`resolver.py`） | 无任何文档提及 |
| `sms_tool/pay_link/` 整包（`adapters.py`、`normalize.py`、`persistence.py`、`registry.py`、`core.py`） | `architecture.md` Rule 19 只在正文里提过 `pay_link/registry.payment_method_label` |
| `sms_tool/store/environment_ledger.py` | **2026-09-22 新增**（HEAD 前一提交 `f987742`），尚未入文档 |
| `sms_tool/driver_env.py`、`operator_output.py`、`regional_payment_adapter.py`、`stdout_mirror.py`、`timeouts.py` | 无文档提及 |
| `services/protocol-payment/common/` 的 `file_loading.py`、`http_dump.py`、`stripe_flow.py`、`timeouts.py` | 该目录在 `directory-map.md` 只列了 `common/protocol_core.py` |

### 5.3 A7：第 6 轮明确「留待单独一轮」的项，至今未开

`audit-2026-09-02-round6-architecture-hygiene.md:509-512` 原文：

> **13 个 RISKY 未动**，因为它们是「导入了但没用」而非「没人引用」——
> `_parse_mailbox_password_file`、`one_click_sms_max_reuse`、`fetch_client_auth_session_dump`、
> `run_phone` 等在 `cli.py` / `registration.py` 的门面里被 import。这属于**门面冗余导入**，
> 与死代码是不同问题，**需要单独一轮**。

**20 天后这一轮仍未开。** 本轮独立复现了同一现象，并给出了根因。

#### 🔴 根因：F401 根本不在 lint 门禁里

`pyproject.toml` 的 ruff 配置是 `select = ["E9", "F63", "F7", "F82"]` —— **F401（未使用导入）未启用**。
配置注释自己解释了为什么不启用：

> Wider rule sets would flag the documented facade re-exports (ADR-0009) —
> widen only together with per-file-ignores for the compat facades.

所以这批导入 20 天无人发现，不是没人看，而是**没有任何自动化会看它**。

量化（`ruff check --select F401 sms_tool services scripts tests`）：

| 桶 | 处数 |
|---|---|
| 门面 / 兼容壳（ADR-0009 记录在案） | 332 |
| 其余模块 | 127 |
| **合计** | **459 / 85 文件** |

#### 第 6 轮点名的 4 项：3 项已是活契约，1 项仍是遗留

| 名字 | 现状 |
|---|---|
| `one_click_sms_max_reuse` | ✅ 活 —— `cli.py:20` 别名导入、`:891` 使用 |
| `fetch_client_auth_session_dump` | ✅ 活 —— `registration.py:144` 再导出，被 `registration_handlers.py` 等 4 处调用 |
| `run_phone` | ✅ 活 —— `registration.py:54` 在 `__all__` 内 |
| `_parse_mailbox_password_file` | ⚠ 仍零消费，但属 `mailbox.py` 的**门面再导出块**（见下） |

#### ✅ 已落地（判据严格，逐条核实）

1. `recovery_batch.py` 删 **5** 个函数体内死导入（`_safe_relogin_result`、`clear_stale_promotion_at_marker`、
   `proxy_pool_for`、`browser_fetch_for_account`、`CFG`）—— 逐个核实「所在函数体内零引用」。
2. `mailbox.py` 删 **3** 个 stdlib 死导入（`json` / `time` / `datetime`）—— 全仓零 `mailbox.<name>`
   引用，且 stdlib 导入不构成门面契约。
3. 顺带抓到 **3 处失效 docstring**（§8.1 N5）。

#### ✅ 已落地 09-22：`F401` 进**自动化**，以 **ratchet（只禁增长）** 形式，**零删除**

**最终方案是 ratchet，不是「`select` 加 `F401` 要求清零」——后者被两次实测证伪（见下节）。**

报告原案是「`select` 加 F401 + 门面配 `per-file-ignores` + ratchet 基线 = 非门面桶的 127」。
落地过程中原案的**两个要素各被否决一次**：

| 原案要素 | 否决理由 |
|---|---|
| `per-file-ignores` 豁免门面文件 | 门面文件里往往**同时**有真契约与真死导入（`payment_link_manager.py` = 8 契约 / 34 死），文件级豁免会把真死的一起放过 ⇒ 门禁形同虚设 |
| `select` 加 `"F401"`（要求 456 → 0） | 「AST 静态分析能穷举消费面」被实测证伪：判定器从 3 通道补到 8 通道，**每补一轮就暴露一批新的隐式消费形式**，每轮代价是一次全量回滚（下节） |

**量化口径也一并纠正** —— 原报告的「332 门面 + 127 其余」按**文件角色**粗分，**不可复算且严重高估契约面**：

| 口径 | 结果 |
|---|---|
| 原报告（按**文件角色**粗分） | 332「门面」+ 127「其余」 |
| 实测（按**名字级消费**判定，可复算） | **87 处真契约** + **369 处真死** |

差别集中在 `pay_link/` 与 `paypal_link/` 这两个**机械拆分包**：原报告把整包当门面，
实际是「子模块保留了原文件的完整头部 import」——那些 stdlib 名（`json`/`logging`/`os`/`uuid`/`dataclass`）
**谁也不会** `from pay_link.base import json`，是真死导入。

**最终落地方式**（零删除、零语义风险）：

1. **`pyproject.toml` 的 `select` 保持原样** `["E9", "F63", "F7", "F82"]` —— **不加 `F401`**。
   注释已改写为说明为何**不用** `per-file-ignores`，与 `select` 当前值一致。
2. 新增 **`scripts/unused_import_ratchet.py`**：用
   `ruff check --select F401 --output-format json` 采集**逐文件**计数，与
   `scripts/unused_import_baseline.json` 比对，**只禁止增长**。三个设计要点：
   - **逐文件基线（非总数）**：一个文件的下降**不能**掩盖另一个文件的增长。
   - **ruff 退出码不是 `0`/`1` 时硬失败**（`raise SystemExit`）——
     否则「ruff 本身跑不起来」会被当成「0 findings」而**静默转绿**。
   - 基线写盘带 `newline="\n"`（当日 N1 的教训，见 §9.10）。
3. 新增 **`tests/test_unused_import_ratchet.py`**（7 项）把门禁接线进 pytest，含
   **mutation 测试**（把某文件基线 −1 后必须返回失败）、**缺基线必须硬错**、
   **LF 字节断言**、以及「一个文件缩小不得掩盖另一个文件增长」的**反掩盖测试**。

**验收实测**：

```
$ python scripts/unused_import_ratchet.py --update-baseline
baseline updated: 456 unused imports across 84 files
$ python scripts/unused_import_ratchet.py
unused-import ratchet OK (456 <= baseline 456, 84 files)
$ pytest -q tests/test_unused_import_ratchet.py
7 passed
```

**达成**：F401 的**增长**从此被自动化拦住 —— 这正是第 6 轮点名的缺口。
**未达成**：456 处存量**一处未删**。这不是妥协，而是本轮实测得出的结论：
静态判定不足以支撑这个规模的删除（下节给出证据），存量清理需要先回答文末那三个问题。

#### 🔴🔴 两次删除式落地**都失败并全量回滚**：消费通道无法穷举（3 → 6 → 8）

**这段必须留着 —— 它证明「AST 静态分析可以穷举消费面」这个假设是错的。**

**第一次尝试：判定器只有 3 种通道 ⇒ `33 failed / 64 errors`**

第一版判定器只统计**三种通道**（`from M import name` / `from M import *` / `import M` + `M.name`），
判出 KEEP=70 / DEAD=386，删除后跑全量 pytest：

```
33 failed, 4612 passed, 6 skipped, 64 errors
```

**漏掉的三个通道**，每个都对应一批真实失败：

| # | 漏掉的通道 | 失败实例 |
|---|---|---|
| 4 | `from pkg import module as alias` + `alias.attr` | `monkeypatch.setattr(pulse_module.time, "sleep", ...)` ⇒ `AttributeError: module 'sms_tool.registration_pulse' has no attribute 'time'`（26 errors） |
| 5 | **字符串字面量里的 dotted token** | `patch("sms_tool.pay_link.core.current_config_data")` ⇒ `AttributeError: ... does not have the attribute 'current_config_data'`（11 failed） |
| 6 | `patch.object(mod, "attr")` / `setattr(mod, "attr", ...)` 的第二参数 | 同类 patch 面 |

通道 5 还**强制判定器必须扫非 `.py` 资产** —— ratchet 基线是 `scripts/*_baseline.json`，
里面的 `"tests/x.py:mailbox._poll_email_otp"` 就是字符串形式的契约清单。

**可复算性锚点**：补上通道 4/5/6 后 KEEP 从 70 涨到 **87**，新增的 17 项**逐个对应**上面那些失败测试
（`registration_pulse.time`、`pay_link/core.current_config_data` …）。
这不是"多留一点保险"，而是**用失败测试反推判定器的漏检面**。

**第二次尝试：判定器扩到 6 通道 ⇒ `17 failed`**

```
17 failed, 4694 passed
```

仍漏两个通道：

| # | 漏掉的通道 | 失败实例 |
|---|---|---|
| 7 | **dotted 字符串 token 的中间段** —— 字符串含 `.` 时其**所有前缀**都是潜在消费面 | `patch("sms_tool.payment_link_manager.subprocess.run")` 消费的是 `payment_link_manager.subprocess` ⇒ `AttributeError: module 'sms_tool.payment_link_manager' has no attribute 'subprocess'` |
| 8 | `from x import name as alias` 的 **alias 侧** | `from ..commands.helpers import mailbox_from_explicit_args as _mailbox_from_explicit_args` ⇒ 测试调 `cli._mailbox_from_explicit_args(args)`，报 `AttributeError: module 'sms_tool.cli' has no attribute '_mailbox_from_explicit_args'` |

同轮还暴露两处**连带损伤**：

- **`ruff --fix` 破坏 `try/except ImportError` 双路径对称性**（§8.1 N9 / §9.14）；
- **删除改变 ratchet 基线漂移**：删 `_message_recipients` 等使
  `scripts/mailbox_private_import_baseline.json` 的 `per_file` 从 14 → 12 ——
  **即"删死代码"本身会污染其它 ratchet 的基线**，这是原报告完全没预见的耦合。

**⚠️ 由此确立的硬规矩**：

1. **删除类改动不能只靠静态判定，必须由全量 pytest 兜底**；
2. 判定器的「通道数」应当由**失败测试**来发现，而不是由作者想象（已发现 **8 种**，且**尚未证明完备**）；
3. **回滚必须先于"打补丁"**：第一次失败后我先做了备份（`runtime/tmp/backup_a7/`，
   84 个文件，A7 开始前的完整工作树状态），才能干净重来；
4. **代价函数**：每补一轮通道 = 一次全量回滚（456 处改动）+ 一轮全量 pytest（约 12 分钟）。
   当"补通道"的边际成本恒大于收益时，**应当改变目标而不是继续补判定器** —— 这就是转向 ratchet 的决策依据。

#### ⬜ 存量 456 处：ratchet 已冻结，**删除仍需拍板**（三处待回答）

ratchet 冻结了存量，所以下列三项**一处未动**，且各有明确的不删理由：

**① 20 项「同行豁免」**：有 8 行同时含 1 个契约名与若干真死名（典型：`pay_link/*.py:17` 的
`from ..config import ConfigError, current_config_data, resolve_runtime_config, validate_config`，
其中只有 `current_config_data` 被判契约）。行级 `noqa` 覆盖整行 ⇒ 那些真死名会被顺带豁免、**未删**。
**不删的理由**：收益 20 项 vs 需要逐行拆行编辑 8 处，且"自信判定"已经翻车两次（§8.1 N12）。
定位方式是把「预期 DEAD 清单」与「实测残留」做**集合差** —— 只看总数会对不上（349 vs 369）。

**② `registration_drivers/external_sessions/__init__.py` 的再导出块**（未删）。

该包入口共 **11 个 F401**。⚠️ **本节原写「10 个零消费 + 1 个契约」是错的** ——
那是**通道 6** 判定器的产物，从未按通道 7/8 重算。按 8 通道口径实测重算后是
**「8 个零消费 + 3 个契约」**：

| 类别 | 数量 | 名字与消费点 |
|---|---|---|
| **真契约**（包级绑定被消费） | **3** | `MOZ_DISABLE_CONTENT_SANDBOX`（`es.MOZ_DISABLE_CONTENT_SANDBOX`，`tests/test_camoufox_sandbox.py:25`）· `curl_requests`（`patch("…external_sessions.curl_requests.request")` ×4）· `time`（`patch("…external_sessions.time.sleep")`） |
| **零消费**（可删） | **8** | `_playwright_proxy`、`ConnectedPlaywrightSession`、`_first`、`_require`、`_normalize_debugger_address`、`_roxy_retryable`、`apply_playwright_stealth`、`_browser_profile_dir` |

**🔴 漏判的根因（判定器 v3 的实现缺陷，值得单独记）**：字符串索引是按
**token 的最后一段**建桶的 —— `by_suffix[tok.rsplit(".", 1)[-1]]`。
于是 `…external_sessions.curl_requests.request` 落进 **`request`** 桶，
查 `curl_requests` 时**永远查不到**；`time` 同理（落进 `sleep` 桶）。
**这正是通道 7「dotted token 的所有前缀」，但本节散文是用通道 6 的产物写的、没有重算**，
于是留下 2 个假阴性。
⚠️ **这 2 个假阴性是被全量 pytest 抓住的**（`patch` 目标不存在 ⇒ `AttributeError`），
**不是**被判定器抓住的 —— 恰好反向印证本节第 1 条硬规矩。
另外两个易误判的：`apply_playwright_stealth` 与 `_browser_profile_dir` 的**包级**绑定确实零消费，
但它们的消费点在**子模块**上（`…external_sessions.managed.apply_playwright_stealth`、
`from …external_sessions.profiles import _browser_profile_dir`）—— **删包级绑定不会动到它们**。

**`__all__` 只声明 5 个名字**（`CamoufoxBrowserSession` / `CloakBrowserSession` /
`RoxyBrowserSession` / `create_browser_session` / `verify_browser_proxy_country`）。
⇒ **公开面既不是「`__all__`」，也不是「全体绑定名」，而是「`__all__` ∪ 事实消费集」= 8 个名字。**

**✅ ADR-0009 的原意已核实**（这原本是本项唯一的实质阻塞）：

> Preserve `external_sessions` imports **through a package** separating lifecycle,
> profile configuration and egress audit.

`external_sessions` **当时是单文件模块** —— `sms_tool/registration_drivers/external_sessions.py`
在 `5168671`（发布 v2026.09.08）被**删除**、改为包；而 ADR-0009 写于 **09-06**。
且该 ADR 的兄弟条目写的是「**Preserve facade imports** and pre-invocation patch scopes」——
两句合读，约束是「**拆分时不要破坏既有导入路径**」，**不是**「保留 `__init__.py` 的每一个内部绑定」。
⇒ **那 8 个零消费名不受 ADR-0009 保护。**

**为什么本轮仍然不删**：ratchet 的既定语义是「冻结存量、只禁增长」，本轮**不做删除**。
**但有一条干净路径可以顺带解掉 2 个契约**：`curl_requests` / `time` 只被**测试 patch 锚点**消费，
把这 5 处 patch 目标从 `…external_sessions.curl_requests` 改指向
`…external_sessions.managed.curl_requests`（**同一个模块对象，patch 效果完全相同**），
它们就也变成零消费 ⇒ **「3 契约 + 8 死」变成「1 契约 + 10 死」**。
属独立改动（改测试、跑全量），留待拍板。

**③ feature-detection 导入**：`paypal/orchestrator.py:339-340` 的 `camoufox`/`browserforge`
导入是**可用性探测**（`except ImportError: use_camoufox = False`），删了会移除
「Camoufox 未装则回退 CloakBrowser」的降级分支。ruff 用 `unsafe` 标记 + 提示
`consider using importlib.util.find_spec` 暗示了这一点。**永久保留**（§8.1 N10 / §9.15）。

### 5.4 反例：`docs/directory-map.md` 的逐文件覆盖率**无法良定义**（不报告）

我尝试用「glob 感知」法测 directory-map 对 266 个库文件的覆盖率，得到
「131/266 未被任何 token 覆盖」—— **这个数字已弃用**。
原因：该文档的模块表用**模式而非字面文件名**（如 `` `providers/mailbox_*.py` ``、
`` `providers/*_client.py` ``、`` `mailbox_<provider>` ``），而 `` `mailbox_<provider>` ``
不含 `.py`，被我的 token 过滤器丢掉 ⇒ 大量假阳性。

按本仓既有的教训（`scan-2026-09-09` §6.1：「测量约定不一致会把正确的文档报成漂移」），
**没有和文档一致的测量约定，就不该给出覆盖率数字**。
`docs/architecture.md` 的做法是对的：它的 `Boundary Rule Checks` 表**每一行都自带 grep 命令**，
所以可复算。建议 `directory-map.md` 的模块表也照此办理（见 §8）。

---

## 6. 死代码（横切四轴）

**方法**：`git ls-files` 跟踪面；库面 = `sms_tool/` + `services/`；
AST 抽 **3765** 个模块级定义；引用计数**先剥 docstring**，且把
`ast.Name` / `ast.Attribute` / `ast.alias`（含 `as` 别名）/ `ast.keyword` / 字符串字面量全部计入。
判定：函数/类 0 引用、常量 ≤1 引用。

**结果：23 个零引用模块级定义。**

| 分组 | 数量 | 明细 |
|---|---|---|
| blik/ideal/twint 提取器三胞胎 | **9** | `fetch_redirect_page` ×3、`run_attempt` ×3、`build_attempt_batches` ×3 |
| blik 独有代理选址 | 4 | `proxy_country_cache_ttl:663`、`proxy_target_cache_ttl:688`、`expected_proxy_country:714`、`load_proxy_groups:1260` |
| 第 6 轮已记录、至今未删 | **2** | `diagnostics.py:28 safe_exception`（round6:141）、`providers/smailr_client.py:234 _mailbox_id`（round6:151） |
| 其余 | **8** | `kakao_extract.py:1340 no_kakao_method_error`、`account_health_queue.py:391 _account_payload`、`browser_fingerprint_pool.py:279 _normalize_geo_response`、`k12_client.py:11 normalize_k12_route`、`k12_client.py:19 _refresh_access_token_from_cookie`、`cfworker_client.py:513 _contains_otp`、`cfworker_client.py:533 _message_matches_email`、`upi_link.py:718 _write_qr_svg` |

**🔴 计数勘误（落地阶段自查）**：本表原写作「9 + 4 + **3** + **7** = 23」，
但第 3 行的 `blik fetch_redirect_page`（round6:153）**已经计入第 1 行的
`fetch_redirect_page` ×3**，属重复计数；而第 4 行标 7 却列了 8 项。
**两个错误恰好互相抵消**（3+7 = 2+8 = 10），所以总数 23 一直是对的 ——
**这正是"总数对不代表分组对"的典型**（同 §9.16 的集合差教训）。
去重后自洽口径：**23 = 9 + 4 + 2 + 8**。
定位方式：**逐行清点明细条数**，而不是只看各行的声明数字加起来对不对。

**✅ A5 已落地 09-22：实删 10 项**（`diagnostics.safe_exception`、`smailr_client._mailbox_id`、
`kakao_extract.no_kakao_method_error`、`account_health_queue._account_payload`、
`browser_fingerprint_pool._normalize_geo_response`、`k12_client.normalize_k12_route`、
`k12_client._refresh_access_token_from_cookie`、`cfworker_client._contains_otp`、
`cfworker_client._message_matches_email`、`upi_link._write_qr_svg`）
⇒ **上表「第 6 轮已记录」与「其余」两行已清零**（10 = 2 + 8）。
**未删的 13 项** = 三胞胎 9 + blik 代理选址 4，理由见 §8 第 6 项
（前者等 §4.2 结论一并处置，后者属 `plan-2026-09-17` §2.5 未决的 Q1）。
⚠️ 因此上表的**行号引用对已删项已失效**，保留原值仅作历史取证。

逐项在**自身文件内** grep 确认「命中数 == 1（仅定义行）」，12 项抽样全部通过。
`_normalize_geo_response` 的旁证：`browser_fingerprint_pool.py` 里留着一句
「Kept byte-compatible with the old ``_normalize_geo_response`` output so」——
即它是重构后遗留的旧实现。

**方法级扫描的 5 个候选全部是误报**（框架按名回调）：
`services/mail-otp-web/app.py Handler.do_GET`（`http.server` 回调）、
`common/protocol_core.py ProtocolResult.__post_init__`（dataclass 钩子）、
`config.py LegacyConfigView.__getitem__` / `__delitem__`（容器协议）、
`providers/mailbox_gmail.py _ProxiedIMAP4_SSL._create_socket`（`imaplib` 钩子）。
**方法级扫描的结论是「0 个真死代码」，只有模块级那 23 项成立。**

---

## 7. 已驳回的误报（含对既往结论的两处纠正）

### 7.1 🔴 纠正一：`store/connection.py` 的反向依赖**不是缺陷**

`sms_tool/store/connection.py:57,89` 在函数体内 `import sms_tool.storage`，
形成「下层 store 反向上依赖上层门面」的观感。**但这是有意设计，且已在原地写了理由**
（`connection.py:40-47`）：

> DELIBERATE REVERSE DEPENDENCY - do not "clean this up".
> The shell is the test suite's patch injection point: 7 test files do
> `patch.object(storage, "database_path", ...)`. Resolving through the shell re-reads
> the attribute on every call, so the patch still redirects internal callers.

**驳回**。若按静态扫描的直觉去「清理」，会让 7 个测试文件的 patch 静默失效
（内部调用者改用真实数据库）—— 这正是本仓反复踩过的「门面是 patch 注入面」陷阱。

### 7.2 🔴 纠正二：`account_deatived`（拼写错误标记）**不是死分支**，第 6 轮结论已过时

`audit-2026-09-02-round6-architecture-hygiene.md:177-179` 写：

> `AccountStatusInterpreter.cs:285` 与 `BackendResultInterpreter.cs:73` 都匹配拼写错误的
> `"account_deatived"`，而 Python 侧只发正确拼写 `account_deactivated` → **容错分支永不命中**。

**实测不成立**：`account_deatived` 在 Python 侧出现 **22 次**，且是**双向的、被测试钉住的**历史拼写容忍：

- `accounts/account_terminal.py:38` — `"account_deatived",  # historic typo variant that really occurred upstream`
- `store/normalize.py:316-327` — 专段注释 + 标记表
- `store/connection.py:316-320` — SQL 的 `WHEN lower(status) IN (...)` 与 `LIKE '%account_deatived%'`
- `tests/test_backend_text_markers.py:158-171` — **跨语言契约测试**，断言 C# 与 Python 两侧都带这个字面量

C# 侧也已**集中化**（`SmsWorkbench.Contracts/BackendTextMarkers.cs:37` +
`BackendResultInterpreter.cs:143`），不再是两处散落。

**驳回，并把纠正写回此处**：第 6 轮该条已被后续工作推翻（Python 侧补了拼写变体容忍，
并加了跨语言测试）。**不要**按第 6 轮去删这两个字面量 —— 删掉会同时弄红
`tests/test_backend_text_markers.py`。

### 7.3 🔴 纠正三（自查）：A6「C# 死成员仍在」是我自己的误报

本条不是纠正前人的结论，而是**纠正本报告初版**。

初版判 A6 的依据是「在 `*.cs` 里数符号命中次数」：10 个符号 `hits=1`，
我便读作「仅有定义行」。落地阶段逐个打开命中行后，`hits=1` 的那一行是
**`// X removed (2026-09-02, round 6)` 墓碑注释**，定义早已删除。

**教训**：零引用/死成员类扫描，**「命中数」只能用来筛候选，不能用来下判定**。
下判定必须读命中行、区分「活定义 / 注释 / 字符串 / 文档」。
同一份报告里我对 `GetQuotaStatus` 恰好读了行（因而判对「已删」），
对其余 10 个只数了次数（因而判错）—— **同一批候选、两种验证深度，就必然出错**。

### 7.4 其余驳回项

| 项 | 驳回理由 |
|---|---|
| `fingerprint_pool.py:72 apply_to`（round6 列为死） | `fingerprint_pool.py:128` 有 `headers = profile.apply_to(base_headers)`，**在用** |
| `one_click_sms_max_reuse`（round6 列为死/门面冗余） | `cli.py` 以 `as _one_click_sms_max_reuse` 导入并在生产路径与测试中使用（`max_reuse=_one_click_sms_max_reuse`） |
| 三个 ratchet「未接入 CI」 | 已由 `tests/test_*.py` 接线，随 pytest 步骤执行 |
| `docs/directory-map.md` 覆盖率 131/266 | 测量约定不匹配（§5.4） |
| 8 个「循环依赖」 | 全部由懒加载/门面断开，import 期无环（§3.1） |
| C# `OpenConfig` / `OpenReport` | 被 `.xaml` 事件绑定引用（§4.3） |
| 方法级 5 个零引用候选 | 框架按名回调（§6） |

---

## 8. 建议落地顺序

**第一批（零决策、可机械验证）**

1. ✅ **A2 已落地 09-22**：`docs/audits/README.md:42` 的「31 files」已改为 **40**
   （39 个已跟踪 + 本报告入库后 1 个）。
2. ✅ **A8 已落地 09-22**：`docs/directory-map.md` 顶层表已补 `local/`、`.githooks/`、`.github/` 三行。
3. ✅ **A3 已落地 09-22**：`cli_parsers/`、`geo/`、`pay_link/` 三个包已在 `directory-map.md`
   补模块组行（并顺带补了 `store/` 整包 —— 原报告漏记，见下）。
   **验收实测**：未文档化模块探针 **13 → 0**；`scripts/docs_consistency_scan.py` 绿。

   ⚠ **落地时发现原报告漏项**：`sms_tool/store/`（8 个模块）在 `directory-map.md` 里
   **整包缺失**，比报告 §5.2 的「13 个模块」范围更大。已一并补上。

**第二批（小成本、有明确判据）**

4. ✅ **A1 已落地 09-22**：`commands/accounts.py:247` 与 `accounts/account_scan.py:23` 的导入已改到
   `recovery_batch`，4 个测试模块已重定向，`account_recovery.py:1298-1316` 的
   `_BATCH_EXPORTS` + `__getattr__` 已删。
   **验收实测**：SCC **8 → 7**；`git grep 'account_recovery\.\(refresh_local_quota_statuses\|_prune_liveness_snapshots\|_heavy_lane_slots\)'`
   只剩注释；全量 pytest 计数不降。
   🔴 **原计划的最后一步「把 31 处函数体内导入提到模块级」已撤销**：那些导入是 **patch 面**
   （`tests/test_account_recovery.py:1308` patch `account_recovery.CFG`，只有函数体内导入才读得到），
   提到模块级会让该 patch **静默失效**。**函数体内导入保留原样。**
5. **~~A6~~ 删 10 个 C# 死成员** —— 🔴 **落地阶段撤销：它们第 6 轮就已删除**，
   文件里只剩墓碑注释。改为：**不动 C#**（详见 §4.3）。
6. ✅ **A5 已落地 09-22**：实删 **10** 项。
   **未删的 13 项各有明确理由**：
   - **blik 4 项**（`proxy_country_cache_ttl` / `proxy_target_cache_ttl` / `expected_proxy_country` /
     `load_proxy_groups`）—— 确实零引用，但它们属 `plan-2026-09-17` §2.5 的「blik 独有 32 函数」，
     即该计划**尚未回答的 Q1**（「blik 特有需求」还是「blik 是新一代、ideal/twint 是旧版」）。
     **删掉等于用一次清理动作替一次架构决策投票，且决策者看不到自己投过票。** 留给 A4。
   - **其余 9 项**属 blik/ideal/twint 提取器三胞胎，同理由 §4.2 的度量结论一并处置，
     避免同族重复删改。
   🔴 **通用铁律（§9.9）：零引用 ≠ 可删 —— 先问「它是不是某个未决设计问题的一部分」。**

**第三批（需要立项或老板拍板）**

7. ✅ **A4 测量已落地 09-22**：`upi_link.py` 的同构度已用零改动探针测出（详见 §4.2 的落地结论）。
   结论是**不建议现在拆分** —— 见 §4.2。
8. ✅ **A7 已落地 09-22（以 ratchet 形式，零删除）**：新增 `scripts/unused_import_ratchet.py`
   + `scripts/unused_import_baseline.json`（冻结 **456 处 / 84 文件**，**只禁增长**）
   + `tests/test_unused_import_ratchet.py`（7 项，含 mutation / 缺基线硬错 / LF 字节 / 反掩盖）。
   **原案的两个要素各被否决一次**：
   - `per-file-ignores` —— 会把同一门面文件里的真死导入一起放过；
   - `select` 加 `F401` 要求清零 —— **两次删除式落地都失败并全量回滚**：
     判定器 3 通道 ⇒ `33 failed / 64 errors`；扩到 6 通道 ⇒ `17 failed`；
     最终确认消费通道 **≥ 8 种且未证明完备**（详见 §5.3）。
   **代价函数**：每补一轮通道 = 一次全量回滚（456 处改动）+ 一轮全量 pytest
   ⇒ 当边际成本恒大于收益时**应改变目标，而不是继续补判定器**。
   存量 456 处**一处未删**；三项不删理由见 §5.3 文末（**需拍板**）。
9. ✅ **§5.4 已落地 09-22**：`directory-map.md` 模块表已加「Check（复算命令）」列，
   并补了覆盖率复算配方与实测数字（180/240 = 75%）。

### 8.1 落地阶段新增发现（原扫描未覆盖）

| # | 类型 | 发现 | 处置 |
|---|---|---|---|
| N1 | 缺陷 | `scripts/refresh_doc_symbol_lines.py:146` 的 `write_text` **缺 `newline="\n"`**，在 Windows 上把被改写的文档整体翻成 CRLF（实测 279/279 行）。`.gitattributes` 要求 `eol=lf`，但行尾守卫只抓「混用」，**四道门禁全绿** | ✅ 已修脚本 + 文件回正 + 补字节级回归测试（§9.10） |
| N2 | 文档 | `sms_tool/store/`（8 模块）在 `directory-map.md` **整包缺失**，比 §5.2 的「13 个模块」范围更大 | ✅ 已随 A3 补齐 |
| N3 | 文档 | §6 的分组计数「3 + 7」把 `blik fetch_redirect_page` **重复计了一次**；去重后为 9 + 4 + 2 + 8 = 23 | ✅ 表已重排并加勘误说明 |
| N4 | 测试 | `tests/test_heavy_lane_slots.py` 的 docstring 引用了 `account_recovery.py:172/222` —— **拆分之下的陈旧行号**，指向无关代码 | ✅ 已改为 `recovery_batch.py:270/320` |
| N5 | 缺陷 | **3 处「失效 docstring」**：字符串写在函数/类体首位**之后**，因此 `__doc__` 为 `None`，那些说明文字**从来不是 docstring**。`recovery_batch.py` ×2、`registration_handlers.py` ×1（后者一处让 5 个类的文档全废） | ✅ 已移到首位；`.venv` 运行时实测 `__doc__` 已非空 |
| N6 | 门禁 | **`F401` 不在 ruff `select` 里** ⇒ 「门面冗余导入」无任何自动化会检（§5.3 根因）。`select = ["E9","F63","F7","F82"]` | ✅ 已落地 09-22：**不把 `F401` 加进 `select`**（加宽即要求 456 → 0，两次实测否决），改用 `scripts/unused_import_ratchet.py`（逐文件基线，只禁增长）+ `tests/test_unused_import_ratchet.py` 接线 |
| N7 | 文档 | `docs/audits/README.md` 的计数行没有复算命令，任何新增报告都会让它漂移（本轮 A2 修过一次，下次还会漂） | ✅ 已加复算命令 + **新增 `tests/test_audits_readme_index.py` 钉住**（计数不符即红），漂移从"无人可见"变成"测试失败" |
| N8 | 方法学 | §5.3 的「332 门面 + 127 其余」口径**不可复算**：它按**文件角色**粗分，把 `pay_link/`、`paypal_link/` 两个机械拆分包整包当门面。按**名字级消费**判定，真契约只有 **87 处**，真死 **369 处**。原案若照此配 `per-file-ignores`，会豁免 `payment_link_manager.py` 里 34 处真死导入 | ✅ 口径已纠正为名字级判定（87 契约 / 369 死）；但**未据此删除** —— 判定器被证无法穷举消费面，改为 ratchet 冻结（§5.3） |
| N9 | 🔴 缺陷 | **`ruff --fix` 对 `try/except ImportError` 双路径导入会破坏两分支对称性**：它的策略是「删 except 分支（safe）、留 try 分支（unsafe）」。`pp_link_helpers.py` 实测：except 分支的 `CheckoutRequestContract` 等被删，try 分支的还在 ⇒ 包内导入路径与直接脚本执行路径**不再暴露同一组名字**，且 try 分支残留变成新报错（两轮都是 `357 = 349 + 8` 这种级联） | ✅ 已确认（**改动随全量回滚撤销**，知识保留）；新增 `runtime/tmp/a7_check_asymmetry.py` 做改动前后逐 `try/except` 名字集合比对（problems=0）。判据：`--fix` 报的 `Found N (M fixed)` 中 `N > 改动前报数` 即级联信号 |
| N10 | 🔴 缺陷 | **`try: from X import Y / except ImportError` 里的导入是 feature detection，F401 会误报**：`paypal/orchestrator.py:337-343` 用导入本身探测 camoufox 是否安装（`except ImportError: use_camoufox = False`）。**删掉会同时删掉「Camoufox 未装则回退 CloakBrowser」的降级分支** —— ruff 正是因此标 unsafe | ✅ **未删**（注解随回滚撤销）；这类导入**永久保留**，不得按"未使用"删除。ruff 的 `consider using importlib.util.find_spec` 提示即为信号 |
| N11 | 门禁 | 删 import 使 `orchestrator.py` 的 `run_browser_registration` 从 69 行移到 68 行，`docs/registration-and-proxy-architecture.md` 的 **7 个符号指针漂移**，`docs_consistency_scan` 转红 | ✅ `refresh_doc_symbol_lines.py --apply` 刷新；**顺带验证了今天 N1 的修复**（103 个 `.md` 全部 `w/lf`，无一被翻成 CRLF）。回滚后复跑该脚本判 `Doc symbol pointers are current` ⇒ **脚本是幂等且与代码自洽的**（`browser_fingerprint_pool.py` 的 `:540`/`:320` 对应当日已落地的 geo 薄壳删除，非 A7 残留） |
| N12 | 方法学 | **行级 `noqa` 会顺带豁免同一行上的其它 F401**：8 行同时含契约名与真死名（如 `pay_link/*.py:17` 的 `from ..config import ConfigError, current_config_data, resolve_runtime_config, validate_config`，只有 `current_config_data` 是契约），给契约名加 `noqa` 后同行真死名不再上报 | ⬜ **未删**（含在 456 存量内）。机制已在删除态实测确认。定位方式：把「预期 DEAD 清单」与「实测残留」做**集合差** —— 只看总数会对不上（349 vs 369） |
| N13 | 🔴🔴 方法学 | **两次删除式落地都失败**：第一版判定器只统计 3 种消费通道 ⇒ `33 failed / 64 errors`；扩到 6 通道 ⇒ `17 failed`；最终确认消费通道 **≥ 8 种且未证明完备**。漏掉的通道 4/5/6/7/8 各对应一批真实失败（详见 §5.3）。**"AST 静态分析能穷举消费面"这个假设被实测证伪** | ✅ 两次均全量回滚（靠 A7 前的 84 文件备份）；**决策转向 ratchet**。确立三条硬规矩：①**删除类改动必须由全量 pytest 兜底**；②判定器的通道数应由**失败测试**反推；③当"补通道"的边际成本（一次回滚 + 一轮全量 pytest）恒大于收益时，**改变目标而非继续补判定器** |
| N14 | 缺陷 | 今天新加的 BOM 守卫（`tests/test_source_hygiene.py`，N5 落地时加的）**首次全量运行就抓到 3 个文件带 UTF-8 BOM**（`tests/test_mail_web.py`、`tests/test_phone_proxy.py`、`tests/test_workspace_scan.py`）—— 与守卫 docstring 记录的「Round 5 五个中的三个未清」**完全吻合**。该守卫加进来后没跑过全量，所以此前无人看见 | ✅ 字节级移除 BOM（保留原行尾），守卫 4 passed |
| N15 | 缺陷 | **`docs/audits/README.md` 的自述计数会漂移**：本次 A7 期间它又被新报告带偏（`docs_consistency_scan` 不检查自述条目数） | ✅ 见 N7 —— 已加 `tests/test_audits_readme_index.py` 钉住计数与条目覆盖 |
| N16 | 🔴 缺陷 | **回滚清单只覆盖了"被删改的文件"，漏掉了"由改动派生"的文件**：`select` 回滚了，但 `pyproject.toml` 的**注释**没回滚，一度声称「F401 was added on 2026-09-22」而 `select` 里根本没有 F401 —— **解释配置值的注释与配置值脱节**，且没有任何门禁会看注释 | ✅ 已改写注释为如实描述（说明为何**不**加 F401、ratchet 落在哪三个文件）。同类风险面：`scripts/*_baseline.json`（ratchet 基线）、文档符号行号。**判据：回滚后按「配置值 ↔ 解释该值的注释/文档」逐对复核**，而不是只 diff 代码 |
| N17 | 🔴 缺陷 | **ratchet 会把"工具本身跑不起来"读成"零告警"而静默转绿**：`ruff check` 退出码为 `2`（配置错误 / 解释器缺失 / 崩溃）时，若按"无输出即无 findings"处理，门禁会在 ruff 坏掉的那一刻**假装通过** —— 这正是本报告 §9.12 批评的"门禁存在但不生效"的另一种形态 | ✅ `collect()` 对 `returncode not in (0, 1)` 直接 `raise SystemExit`；并用测试钉住（缺基线 ⇒ 退出码 2）。**通用判据：任何"解析外部工具输出"的门禁，都必须显式区分"无告警"与"没跑成"** |
| N18 | 🔴🔴 缺陷 | **判定器的字符串索引按 token 的「最后一段」建桶**（`by_suffix[tok.rsplit(".", 1)[-1]]`），于是 `…external_sessions.curl_requests.request` 落进 `request` 桶，查 `curl_requests` **永远查不到**。这**就是通道 7**，但 §5.3 的散文是用**通道 6** 的产物写的、从未重算 ⇒ 留下 **2 个假阴性**（`curl_requests`、`time` 被写成「零消费」，实际各被 4 处 / 1 处 patch 消费） | ✅ §5.3 ② 已按 8 通道口径重算为「**8 零消费 + 3 契约**」；ADR-0009 原意已核实（约束是导入**路径**，非内部绑定）。⚠️ **这两个假阴性是被全量 pytest 抓住的，不是被判定器抓住的**。**通用判据：判定器的产物一旦被写成散文，通道升级后必须重算散文 —— 否则它会比代码活得更久** |

---

## 9. 坑清单（本轮踩到的，都出自**我自己的探针**）

1. 🔴 **零引用扫描器必须把 `ast.alias` 计入引用**。只数 `ast.Name`/`ast.Attribute` 会把
   `from x import y` 后的 `y` 当零引用：首版报 **43** 项，计入 alias 后 **24** 项 ——
   **19 项纯虚报**。
2. 🔴 **`import x as y` 要同时记 `y` 和原名**。只记别名会让 `one_click_sms_max_reuse`
   （以 `as _one_click_sms_max_reuse` 导入）再虚报一次：24 → **23**。
3. 🔴 **逐文件覆盖率检查必须先和文档的书写约定对齐**。`directory-map.md` 用
   `` `providers/mailbox_*.py` `` / `` `mailbox_<provider>` `` 这类**模式**，
   按字面文件名匹配会报 131 项假阳性（§5.4）。**没有一致的测量约定就不给数字。**
4. 🔴 **C# 死成员必须双面检索 `.cs` + `.xaml`**。`OpenConfig` / `OpenReport` 只看 `.cs` 是死的，
   实际被 XAML 事件属性绑定（§4.3）。
5. 🔴 **方法级零引用扫描必须按名排除框架回调**。`do_GET` / `__post_init__` /
   `__getitem__` / `_create_socket` 都会被「全仓只出现一次」误报（§6）。
6. 🔴 **读既往结论要看它当时的前提**。第 6 轮的 `account_deatived` 结论已被后续工作推翻；
   若照抄会把正确的跨语言契约删掉并弄红 `tests/test_backend_text_markers.py`（§7.2）。
7. 🔴 **读代码里的「不要清理」注释**。`store/connection.py:40-47` 若被静态扫描驱动去「修」，
   会让 7 个测试文件的 patch 静默失效（§7.1）。
8. 🔴🔴 **「命中数」只能筛候选，不能下判定 —— 必须读命中行**。本报告 A6 的 10 项误报全部源于此：
   `hits=1` 被我读作「仅有定义」，实际是**墓碑注释**（`// X removed (2026-09-02, round 6)`）。
   同一批候选里我只对 `GetQuotaStatus` 读了行（判对），其余只数次数（判错）——
   **一批候选里混用两种验证深度，就必然出错**。正确口径见 §4.3 的两条 grep。
9. 🔴 **零引用 ≠ 可删 —— 先问「它是不是某个未决设计问题的一部分」**。A5 的 blik 4 个代理选址
   函数确实零引用，但它们属 `plan-2026-09-17` §2.5 的「blik 独有 32 函数」，正是该计划
   **尚未回答的 Q1**（「blik 特有需求」还是「blik 是新一代、ideal/twint 是旧版」）。
   删掉等于**用一次清理动作替一次架构决策投票**，且决策者看不到自己投过票。**这 4 项留 A4。**
10. 🔴🔴 **`Path.write_text()` 缺 `newline="\n"` 会静默把整文件翻成 CRLF，且四道门禁全抓不到**。
    落地 A5 后跑 `refresh_doc_symbol_lines.py --apply` 修行号，它把
    `docs/registration-and-proxy-architecture.md` 整个写成 CRLF（279 行全是 `\r\n`，0 行孤立 LF）。
    - `.gitattributes` 明写 `text eol=lf`，但 **pre-commit 的行尾守卫只拒绝「混用」**，
      整体 CRLF 是「统一」的，一路绿灯；
    - `git diff` 也看不见（`core.autocrlf` 把它归一化了）；
    - **唯一能看见的是 `git ls-files --eol`** —— 本次它显示 94 个 `.md` 里
      `w/lf` 93 个、`w/crlf` 1 个，那个 1 就是罪证。
    已修：脚本补 `newline="\n"` + 文件回正 + `tests/test_doc_symbol_refresh.py` 加
    `test_apply_mode_writes_lf_bytes_not_crlf`（断言**原始字节**，因为 `read_text` 会把差异抹掉）。
    **通用判据**：任何「脚本改写仓库文件」的路径都要问一句「它写回时钉行尾了吗」。
11. 🔴 **「函数体里有个长字符串」不等于「有 docstring」**。字符串必须**在函数/类体首位**才是
    `__doc__`；写在首位之后（典型成因：拆分时把 `from x import y` 插到了原来的 docstring 之前）
    就是一条**被丢弃的表达式**。本轮实测 3 处，其中 `registration_handlers.py` 那处
    让 **5 个类**的文档全废。判据要读**原始字节/AST 位置**，`inspect.getdoc` 也只在运行时才知道
    （实测 `__doc__` 为 `None`）。见 §8.1 N5。
12. 🔴🔴 **`ruff` 的 `select` 是白名单 —— 「ruff 全过」只证明被选中的那几条规则过了**。
    本仓 `select = ["E9","F63","F7","F82"]` 不含 `F401`，所以「门面冗余导入」**无任何自动化会检**，
    20 天零告警不是因为没问题。**读 lint 结论前先读 `select`。** 见 §5.3。
13. 🔴 **用 `ruff --output-format json` 做「改动前后增量」比对时，路径键必须先归一化**。
    Windows 上 `filename` 是**反斜杠绝对路径**，我第一版用正斜杠相对路径查表 ⇒ 恒命中 0，
    差点得出「我的改动新增 0 处 F401」这个**恰好正确但当时无证据**的结论。
    归一化口径：`filename.replace("\\", "/").replace(ROOT + "/", "")`。
14. 🔴🔴 **`ruff --fix` 在 `try/except ImportError` 双路径导入上会破坏两分支对称性**。
    它对「两分支都绑定了的同一个名字」的策略是**删 except 分支（safe）、留 try 分支（unsafe）**，
    于是「包内导入路径」与「直接脚本执行路径」不再暴露同一组名字。实测 `pp_link_helpers.py`：
    except 分支被删掉 6 个名字，try 分支一个没动 ⇒ 结构不对称 + try 分支残留变成新报错。
    **判据**：`--fix` 报告里出现 `Found N errors (M fixed, K remaining)` 且 `N > 改动前的报数`
    就是级联信号。**必须**逐 `try/except` 比对改动前后的两分支名字集合，不能只看残留数。
15. 🔴🔴 **`try: from X import Y / except ImportError:` 里的导入是 feature detection，
    F401 会误报，删了会改变行为**。`paypal/orchestrator.py:337-343` 用导入本身探测
    camoufox 是否安装，`except ImportError: use_camoufox = False` 是「回退 CloakBrowser」的分支——
    删掉导入等于删掉降级逻辑。ruff 用 `consider using importlib.util.find_spec` 的提示
    和 `unsafe` 标记暗示了这一点，**看到这个提示就不要自动删**。
16. 🔴 **行级 `noqa` 覆盖整行 —— 同一行上的其它 F401 会被顺带豁免**。
    `pay_link/registry.py:24` 同时有一个契约名和一个真死名，给契约名加 `noqa` 后
    真死名不再上报（所以残留是 385 而不是 386）。**发现方式**：把「预期清单」与「实测残留」
    做集合差，别只看总数对不对 —— 总数少 1 时我一开始以为是自己算错了。
17. 🔴 **按「文件角色」判定门面会严重高估契约面**。本轮的 332 vs 87 就是这么来的：
    机械拆分的子模块会保留**原文件的完整头部 import**，它们看起来像门面（同族、成组、来自同一原文件），
    实际零消费。**唯一可复算的判据是名字级消费**（八种通道全查 —— 而这个"八"本身是失败测试逼出来的，
    见 §9.19），不是文件在哪个包里。
18. 🔴 **删 import 会移动行号，进而弄红文档一致性门禁**。删 373 行后
    `orchestrator.py` 的 `run_browser_registration` 69 → 68，`docs/registration-and-proxy-architecture.md`
    的 7 个符号指针全部漂移。**批量删行之后必须跑 `refresh_doc_symbol_lines.py --apply`**，
    并把"刷新文档行号"当成删除动作的**收尾步骤**而不是事后补救。
    ⚠️ **反过来也成立**：回滚代码之后**必须再跑一次**，否则文档会停在"删除态"的行号上。
    （本次复跑判 `Doc symbol pointers are current`，说明回滚后的 `browser_fingerprint_pool.py:540/320`
    是当日已落地的 geo 薄壳删除所致，**不是** A7 残留 —— 这正是"先复跑再下结论"的价值。）
19. 🔴🔴 **判定器的"消费通道数"不能靠想象，只能由失败测试反推**。第一版我列了 3 种通道
    （`from M import name` / `from M import *` / `import M` + `M.name`），自认为"覆盖了全部写法"，
    实测漏掉 3 种，代价是 `33 failed + 64 errors` 和一次全量回滚。补到 6 种后**又**漏 2 种
    （dotted 字符串的中间段、`as` 别名侧），代价是 `17 failed` 和第二次全量回滚。
    **累计 8 种，且仍未证明完备**。**做法**：先按最保守的方式小批量试，
    让全量测试告诉你漏了什么，再回填判定器 —— 而不是先把判定器"想全"再动手。
20. 🔴 **回滚备份必须在动手之前做，且要覆盖"本次任务之前的完整工作树"**。
    `runtime/tmp/backup_a7/` 存的是 A7 开始时的 84 个文件（**含当天早先的 A1/A5/N1-N5 落地**），
    所以恢复它得到的是"除 A7 之外的一切"，而不是 HEAD —— 这正是重做所需要的起点。
    **另一个坑**：备份文件名用 `rel.replace("/", "__")` 扁平化后**不可逆**
    （`__init__.py` 自身含 `__`），必须用 JSON 里的原始相对路径还原，不能从文件名反推。
    ⚠️ 但备份只覆盖了"被删改的 84 个文件"，**没有覆盖由改动派生的文件**（`pyproject.toml` 注释、
    ratchet 基线、文档行号）—— 见 §9.22。
21. 🔴 **新加的守卫/测试本身也要跑一次全量**。`tests/test_source_hygiene.py` 是当天 N5 落地时
    加的，加完没跑全量；等 A7 验证时才首次运行，立刻抓到 3 个带 BOM 的文件。
    **守卫没跑过 = 守卫还没生效**，"我加了守卫"和"守卫在拦人"是两回事。
22. 🔴 **回滚清单必须包含"由改动派生"的文件，而不只是"被改动的文件"**。本次回滚把 `select` 还原了，
    却漏掉 `pyproject.toml` 里**解释这个 `select` 的注释** —— 它一度声称「F401 was added on 2026-09-22」，
    而 `select` 里根本没有 F401。**没有任何门禁会看注释**，所以它只会在下一次有人读配置时误导人。
    同类派生面：`scripts/*_baseline.json`（ratchet 基线）、文档符号行号、`docs/audits/README.md` 计数。
    **判据：回滚后按「配置值 ↔ 解释该值的注释/文档」逐对复核**，而不是只 `git diff` 代码。
23. 🔴 **ratchet 必须区分"零告警"与"工具没跑成"**。`ruff check` 的退出码是 `2` 时（配置错误、
    解释器缺失、崩溃），若按"没有输出就没有 findings"处理，门禁会在 ruff 坏掉的那一刻**假装通过** ——
    这跟 §9.12 批评的"门禁存在但不生效"是同一种病，只是更隐蔽（它平时是绿的）。
    `unused_import_ratchet.collect()` 因此对 `returncode not in (0, 1)` 直接 `raise SystemExit`。
    **通用判据：任何解析外部工具输出的门禁，都要显式写出"失败模式"这一支**。
24. 🔴🔴 **当"把判定器补全"的边际成本恒大于收益时，正确动作是改目标，不是继续补**。
    本轮两次删除式落地各付了一次全量回滚（456 处改动）+ 一轮全量 pytest（约 7.5 分钟），
    而每轮只把通道数从 3 推到 6、再推到 8，**没有收敛迹象**。
    此时把目标从「清零」换成「冻结存量、只禁增长」，**用 1 个脚本 + 7 个测试拿到同一个价值**
    （"F401 的增长被自动化拦住"），且**零删除风险**。
    **判据：如果一个方案的每轮迭代都在"暴露新未知"而不是"逼近完成"，那是目标选错了。**
25. 🔴🔴 **「我用某个模式 grep 过、结果为空」只证明那个模式为空，不证明风险不存在**。
    §3.3 的扫描阶段我用
    `git grep 'patch("sms_tool.accounts.recovery_batch\.' tests/` 排除过「patch 到函数体内
    导入上、静默失效」的风险，结果为空 ⇒ 写下了「**不存在**假绿风险」。落地时才发现
    **真实 patch 面在它的上游**：`tests/test_account_recovery.py:1308` patch 的是
    `account_recovery.CFG`。那 31 处函数体内导入正是为了让调用发生在该 patch **之后**。
    **排除一条风险时必须同时写明「在哪个命名空间、用什么模式」排除的**，
    并主动反问三个方向：**上游？别名侧？字符串字面量里？**
    （这三个方向恰好就是 §5.3 那 8 种消费通道里最难想到的几种。）
    **更一般的形态**：这是同一个病 —— **把"我没找到"读成"它不存在"**，与 §9.8 的
    「把命中数当判定」是镜像关系：一个是**假阳性**，一个是**假阴性**。
26. 🔴🔴 **「为了加速查找而做的键归一化」会静默丢掉信息 —— 索引设计本身可以是漏检源**。
    判定器 v3 把 dotted token 按 **最后一段** 分桶：`by_suffix[tok.rsplit(".", 1)[-1]]`。
    于是 `…external_sessions.curl_requests.request` **只存在于 `request` 桶里**，
    查 `curl_requests` 恒为空 —— 而 `curl_requests` 恰恰是被那个 token 消费的那个名字。
    这正是通道 7（「dotted token 的**每一个前缀**都是潜在消费面」）在**实现层**的表现：
    通道想清楚了，索引却按末段建桶。**修法是登记所有前缀（或改前缀匹配），不是加通道。**
    后果很重：`curl_requests` / `time` 被判「零消费」并写进了报告散文，差点成为删除依据。
    ⚠️ **散文比代码活得久**：通道从 6 升到 8 之后，代码里的判定器改了，**报告里那段散文没重算**。
    **通用判据两条**：① 任何「键归一化 / 取末段 / 取 basename」的加速结构，都要问
    「这个归一化丢掉了什么信息」；② **判定器的输出一旦被写成散文，就必须和判定器一起版本化** ——
    否则升级判定器时，散文会带着旧结论继续误导人。
27. 🔴 **「公开面是什么」要用实测回答，不能用 `__all__` 或直觉回答**。
    `external_sessions/__init__.py` 的 `__all__` 声明 5 个名字，但
    `tests/test_camoufox_sandbox.py` 用 `es.MOZ_DISABLE_CONTENT_SANDBOX`（**不在 `__all__` 里**），
    `tests/test_external_registration_drivers.py` 用 `patch("…external_sessions.curl_requests.request")`。
    ⇒ **`__all__` 只描述意图，不描述事实**；事实是「`__all__` ∪ 事实消费集」。
    判据：枚举**全部**属性访问点（含字符串 patch 目标、别名间接、子模块路径），
    再与 `__all__` 取并集 —— 两者之差才是真正要判断的部分。
28. 🔴 **读 ADR 要连它的「写作时刻」一起读**。ADR-0009 写于 09-06 说
    「Preserve `external_sessions` imports **through a package**」；而
    `external_sessions` **当时是单文件模块**（`external_sessions.py` 在 `5168671` 被删、改为包）。
    ⇒ 那句话是**对拆分的约束**（别破坏导入路径），不是「保留包入口的每个内部绑定」。
    若只读字面「preserve imports」就照做，会把 8 个零消费名当成 ADR 保护的契约永久留存。
    **判据：确认被点名的对象在 ADR 写作时是什么形态**（用 `git log --diff-filter=D --follow` 查）。
