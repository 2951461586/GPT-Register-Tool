# 扫描 2026-09-23：六个功能模块（协议注册 / 查优惠 / 账号测活 / 一键接码 / config.json / 协议支付面板）

> **性质**：只读扫描 + 门禁复核。**扫描阶段零代码改动**；落地项为 §4.1 / §4.2 的**文档交叉引用**
> 与 §4.2 建议 2 的**模块改名**（`sms_provider.py` → `sms_provider_adapter.py`，逐项清单见 §5.1）。
> 探针写在 `runtime/tmp/`，未入库。
> **基线**：HEAD `b044112`，但**工作区领先 HEAD 43 改 / 2 删 / 19 新增** —— 这一条本身是本轮最重要的发现（§1）。
> **与既往报告的关系**：`docs/audits/` 已有 24 份扫描/审计，其中 `scan-2026-09-22-architecture-coupling-dirs-docs.md`
> （代码耦合 / 架构 / 目录 / 文档四轴）与 `scan-2026-09-22-gpt-register-pro-benchmark.md`（对标）
> 与本轮同主题。**本轮不重复其结论**，只报告新增项；两者已决策/已落地的事项在 §7 列出。
> **参考项目状态**：`cxqc168-wq/gpt-register-pro` **无新提交**（HEAD 仍 `7b2304f`，2026-09-19T12:36Z，
> 共 3 个提交）。⇒ 昨日对标报告的 10 项结论**继续有效**，本轮不重跑对标。

---

## 0. 门禁基线：10 道全绿，无新增回归

| 门禁 | 命令 | 结果 |
|---|---|---|
| 架构守卫 | `scripts/architecture_scan.py` | `Architecture scan passed` |
| 文档一致性 | `scripts/docs_consistency_scan.py` | `passed (release-v2026.09.14.md)` |
| 配置 schema | `scripts/config_schema_check.py` | `passed` |
| IPC schema | `scripts/ipc_schema_check.py` | `passed` |
| 行尾守卫 | `scripts/line_ending_guard.py` | `no mixed line endings` |
| 配置键 ratchet | `scripts/config_key_ratchet.py` | `OK (registration=30, email_registration=33)` |
| 未用导入 ratchet | `scripts/unused_import_ratchet.py` | `OK (455 <= baseline 456, 83 files)` → 改名后顺手锁紧为 `455 <= 455`（§5.1） |
| bare-print ratchet | `scripts/bare_print_ratchet.py` | `OK (523 bare prints, baseline frozen)` |
| 延迟导入 ratchet | `scripts/delayed_import_ratchet.py` | `OK: 400 <= baseline 406` |
| 提取器同构 ratchet | `scripts/extractor_parity_report.py` | `OK (ideal:blik=66, ideal:twint=97, momo:kakao=0, twint:blik=60)` |

⚠️ **这批门禁全绿并不覆盖本报告的任何一条发现** —— 下面 5 条都落在门禁的盲区里（每条都注明了为什么）。

**全量验收**：`pytest -q` → **`4935 passed, 6 skipped, 1047 subtests passed`（0 failed，455 s）**，
与 09-22/09-23 的既有基线 `4935/0` **逐字一致** ⇒ 扫描阶段无回归。

**七项落地后复跑**：`pytest -q` → **`4942 passed, 6 skipped, 1054 subtests passed`（0 failed，453 s）**。
`+7` **全部**来自第 4 项（N5）**新增**的 `CSharpConsumerTests`（`tests/test_config_usage.py`，
7 个用例：引用文件存在性 / 引用文件确实含该键 / 被消费键不出现在报告 / 报告点名排除项 /
**只写键仍被报告**的反例守卫 / 抽取器忽略注释 / 列表非空）；
第 7 项改名**不改变**测试数 —— 它改的是文件路径，不是收集面。
（`tests/test_audits_readme_index.py` 在新增本报告后曾红 2 项 —— 已按该门禁的要求把本报告登记进
`docs/audits/README.md` 索引并把自述计数 41 → 42，复测 `2 passed`。）

---

## 1. 🔴 头条：工作区领先 HEAD 43 改 / 2 删 / 19 新增，且已构成「部分提交即破损」的形态

**这不是代码缺陷，是交付风险，且是本轮唯一一个会「一提交就出事」的项。**

### 1.1 实测

```
已修改（跟踪）  43
已删除（跟踪）   2   SmsWorkbench/MainWindow.SmsBower.cs
                    SmsWorkbench/SmsBowerCatalogClient.cs
未跟踪（新增）  19
```

19 个未跟踪文件里有 **4 个生产模块 + 5 个守卫测试**：

| 未跟踪的生产模块 | 未跟踪的守卫测试 |
|---|---|
| `sms_tool/sms_providers.py`（供应商注册表，225 行，新真源） | `tests/test_sms_provider_registry.py` |
| `sms_tool/mailbox_pool_writer.py`（OAuth RT 回写） | `tests/test_mailbox_pool_writer.py` |
| `sms_tool/page_truth.py`（截图配 DOM 真值） | `tests/test_page_truth.py` |
| `sms_tool/browser_profile_reclaim.py` | `tests/test_browser_profile_reclaim.py` |
| （C# 侧）`SmsProviderCatalog.cs`、`MainWindow.SmsProvider.cs`、`SmsProviderCatalogClient.cs` | `tests/test_settings_catalog_provider_parity.py` |

`git cat-file -e HEAD:<path>` 逐个验证：**4 个生产模块在 HEAD 里全部不存在**。

### 1.2 为什么危险：10 个**已跟踪**文件已经在 import 这些未跟踪模块

| 未跟踪模块 | 已跟踪的导入方（提交后仍会在，但目标文件不在） |
|---|---|
| `sms_providers` | `sms_tool/cli_parsers/core.py` · `sms_tool/phone_registration.py` · `sms_tool/phone_reuse.py` · `sms_tool/smsbower.py` · `sms_tool/sms_provider_adapter.py` · `tests/test_phone_reuse_smsbower.py` |
| `mailbox_pool_writer` | `sms_tool/providers/mailbox_gmail.py` · `sms_tool/providers/mailbox_graph.py` |
| `page_truth` | `sms_tool/paypal/session.py` |
| `browser_profile_reclaim` | `sms_tool/registration_drivers/browser_session.py` |

**判据**：HEAD 版的 `phone_reuse.py` 不 import `sms_providers`；工作区版 import 了。
⇒ **`git add -u`（或任何「只提交已跟踪改动」的流程）产出的提交，`import sms_tool.sms_providers` 会 `ModuleNotFoundError`**，
CI 第一步就红，且任何新 clone 都无法启动。而 `git add -A` 是正确的。

**同时丢掉守卫**：4 个守卫测试本身也未跟踪 ⇒ 就算有人补上了生产模块、忘了测试，
`tests/test_settings_catalog_provider_parity.py`（一键接码的 C#↔Python 双向相等守卫）
和 `tests/test_sms_provider_registry.py` 都不在 CI 里，而 `sms_providers.py` 的 docstring 明写它靠这两个测试钉住。
**守卫与它守的代码被拆散提交** = 守卫静默失效，这正是本仓记录过的失效模式。

**建议动作（需老板拍板，涉及 git 写操作）**：提交时用 `git add -A`，**不要** `git add -u`。
本仓有并行提交者 ⇒ 提交前按 mtime 分工作流、并重拉 `git status`（`docs/2026-09-22.md` 已记录的铁律）。

**为什么门禁没拦住**：这不是代码问题，任何静态门禁都看不见「未跟踪」这一维度。
唯一能拦的是 `git status` 的人工判读 —— 所以本条写成清单，不写成脚本。

---

## 2. 结论摘要（按性价比）

| # | 轴 | 结论 | 证据 | 成本 |
|---|---|---|---|---|
| **N1** | **交付** | 🔴 工作区领先 HEAD 43/2/19；10 个已跟踪文件 import 4 个未跟踪模块 ⇒ **`git add -u` 产出破损提交** | §1 | 零（提交时用 `git add -A`） |
| N2 | 文档 | `docs/directory-map.md:11` 仍写 "read-only **SMSBower** catalog adapter"，而**同一文件** `:179` 写 `SmsProviderCatalogClient.cs`；`SmsBowerCatalogClient.cs` 已删 | §4.1 | 极小（改 6 个词） · ✅ **已落地 09-23** |
| N3 | 架构 | 支付域用 **5 种命名形态**铺了 40 个模块；曾有一对只差一个字母的近似命名（`sms_provider.py` vs `sms_providers.py`，**09-23 已改名消除**）；`pay_link/` vs `paypal_link/` 仍只差一个字母 | §4.2 | 低（目录表加交叉引用 + 改名） · ✅ **交叉引用与改名均已落地 09-23**（§5.1） |
| N4 | 文档 | 目录表自述的「60 个未归属模块」**首次按族拆开**：mailbox 9 · accounts 7 · payment 6 · paypal 4 · proxy 4 · sentinel 4 · paypal_link 3 · registration 2 | §4.3 | 低（补 4 行消 30 个） · ✅ **`sms_providers.py` 已补入行 09-23** |
| N5 | 配置 | 未读键判定**只扫 Python**（`SOURCE_DIRS=("sms_tool","services")`）⇒ `runtime.python_path` 被 C# 读 3 处却报「设了没用」 | §4.5 | 极小（改措辞或加一条扫描根） |
| — | — | 六模块里**守卫最密的是「一键接码」**（C#↔Python 双向相等 + 端点 + 环境变量名 + 下拉框 + 「C# 生产面不许出现供应商键字面量」） | §3.4 | — |
| — | — | **协议注册 / 查优惠 / 账号测活三个模块无新增结构性问题** | §3.1–§3.3 | — |

---

## 3. 逐模块体检

六模块合计 **64 个模块 / 28,202 行**，支付一个域占 52%：

| 模块 | 模块数 | 行数 | 本轮判定 |
|---|---|---|---|
| ① 协议注册 | 19（+ `registration_drivers/` 一整个子包） | 7,739 | ✅ 结构最健康 |
| ② 查优惠 | 3 | 1,056 | ✅ 分层干净 |
| ③ 账号测活 | 3 | 1,322 | ⚠️ 代码健康，目录归属缺失 |
| ④ 一键接码 | 7 | 2,115 | ✅ 守卫最密 |
| ⑤ config.json | 4 | 1,303 | ⚠️ 判据措辞不严（N5） |
| ⑥ 协议支付面板 | 32 | 14,667 | ⚠️ 命名空间最大（N3） |

### 3.1 ① 协议注册 —— 无新增项

**结构**：`registration*.py` 13 个顶层模块 + `auth_flow.py`(1664) + `chatgpt_bootstrap.py` + `phone_registration.py`，
驱动面在 `registration_drivers/`（`base.py` 注册表 + `browser_flow/` + `external_sessions/` + 4 个 driver 包装）。

**判定为健康的三条依据**：

1. `docs/architecture.md` 的 Ownership Matrix 对它有 **3 行专属归属**（`registration.py` 门面 / `registration_handlers.py` 工作流 / `registration_drivers/*` 两组），
   而 19 条可执行边界规则里有 **4 条直接覆盖它**（Rule 8 Agent Identity 隔离、Rule 11 进度与并发分离、Rule 15 懒加载接缝、Rule 16 hub 私有符号），**全部当前成立**。
2. `registration.py` 是纯门面（re-export，无实现）；`config.py:204` 对 `protocol` 驱动的特判（跳过驱动凭据预检）是**有意**的 —— 协议驱动无凭据可检。
3. 昨日报告 §3.2 记录的 SCC 2（`browser_flow/session.py:12` → `accounts.account_liveness` 的同层横跳）
   **本轮复核结论不变**：三个符号里两个是纯数据/纯函数，真收敛要把 `CODEX_USAGE_URL` / `quota_result_from_payload` 下沉到无依赖词汇层，收益低于改动面。**记录，不动。**

**本轮新发现**：无。

### 3.2 ② 查优惠 —— 无新增项（一条一句话建议）

**结构**：`accounts/account_promotion.py`(617) + `promotion_states.py`(167) + `accounts/account_payment_eligibility.py`(269)。

**判定为健康**：

- 分层方向正确且**刻意**：`promotion_states.py` 是无依赖词汇层（跨层共用）；
  `account_payment_eligibility.py` 是 `payment_capability.py` 的**薄包装**，
  目的是让 `account_promotion.py` **不直接 import 五个 provider 模块**（目录表第 64 行明写）。
- `account_promotion.py` 的依赖面是 `account_liveness`（复用探针，方向正确）+ `proxy_routing`（不重建代理顺序，符合 architecture.md 的既有纪律）。
- 文档与跨语言契约都在：`docs/current/account-health.md` 覆盖了优惠状态的**三种表示**
  （`promotion_status` 中文标签 / `promotion_state` 机器态 / `promotion_display` 渲染串）、
  stale 规则的**唯一所有者**（`promotion_marker_is_stale`），且机器态跨语言钉在
  `tests/fixtures/promotion_status_cases.json`。C# 侧有 `tests/SmsWorkbench.Tests/PromotionStatusContractTests.cs`。

**唯一建议（一句话，零成本）**：`docs/current/` 有 `account-health.md`、`registration-architecture.md` 等 6 份契约文档，
但**没有 promotion 专属文档**（只有 09-21 的一份 audit）。而 `account-health.md` 已实质承担了这个角色
⇒ **不建议新建文档**，改为在该文件顶部加一句「本文件同时是查优惠（promotion）的契约文档」，
把「契约在哪」这个信息补上，避免后人去 `docs/audits/` 里翻历史快照当现行契约。

### 3.3 ③ 账号测活 —— 代码健康，目录归属缺失（并入 N4）

**结构三分清晰且与文档一致**：

| 模块 | 行数 | 职责 |
|---|---|---|
| `accounts/account_liveness.py` | 538 | **唯一**的 `/backend-api/wham/usage` 探针（Rule 7 单所有者），无副作用 |
| `accounts/account_health.py` | 185 | 结果契约（`HealthCheckKind` / `HealthState` 枚举 + `plan_health_result` / `liveness_health_result`） |
| `accounts/account_health_queue.py` | 596 | 持久化、去重、有界的后台队列；委托给与前台命令**相同**的工作流 |

`docs/architecture.md:65-67` 明写「队列只拥有 claim / lease / heartbeat / scheduling，把 plan 与 liveness 委托给前台同一套工作流」——
代码与文档**逐条对齐**，两侧测试齐备（`test_account_liveness.py` / `test_account_health_budgets.py` / `test_account_health_queue.py` / `test_config_account_health_validation.py`）。

**实测缺陷**：`account_health.py` 与 `account_health_queue.py` **不在 `docs/directory-map.md` 任何一行**
（`account_liveness.py` 在）。即：**文档写了契约，目录表没给归属**。这是 N4 那个「60」的具体形态之一。

**顺带核销一个疑似项**：`scripts/probe_account_liveness.py`（批量，`--email-file`）与
`scripts/probe_single_account_liveness.py`（单账号，`--latest`/`--session`）看似重复，**实测不重复** ——
批量脚本的 argparse 只有 `--email-file/--sessions-dir/--proxy/--workers/--timeout/--browser/--json-out`，
**没有单账号模式**；两者共用同一个 canonical `probe_account_liveness`。⇒ 判定为**互补**，不报缺陷，只报它们未被目录表/README 收录。

### 3.4 ④ 一键接码 —— 守卫最密的模块，只剩文档缺口

**结构**：`commands/one_click.py`(287) + `cli_parsers/one_click.py`(14，纯声明) + `phone_reuse.py`(1080) +
`sms_providers.py`(225，**唯一真源**) + `sms_provider_adapter.py`(43，适配器契约) + `smsbower.py`(316) + `sms_utils.py`。

**这是六模块里守卫密度最高的一个**，实测 `tests/test_settings_catalog_provider_parity.py` 覆盖：

- C#↔Python **双向相等**（少一个＝选不到；多一个＝选了必失败）
- 默认端点逐项相等（🔴 最隐蔽的漂移面：端点错了下拉框照常工作，请求打到了别家主机）
- API Key 环境变量名逐项相等
- 下拉框选项 == C# 表；默认值必须是 `DefaultPhoneProvider` **常量**而不是字面量
- **全 C# 生产面扫描**：`SmsProviderCatalog.cs` / `SettingsCatalog.cs` 之外**不许出现供应商键字面量**
  （按取值判，不按调用形态判 ⇒ 换供应商名、换调用点同样被抓）
- `nexsms` 双向钉住（Python 侧保留但 `client_available=false`，桌面侧不许出现）
- 注册表内**不许有凭据形态的 token**（`_CREDENTIAL` 正则，与 `sms_providers` 自己的守卫同判据）

昨日实测的三处同源缺陷（弹窗选中供应商从没传到后端 / `phone_reuse.py` 的 12 个 `provider == "smsbower"` 守卫 /
`"smsbower_prepare_failed"` 写进错误集合）**已被上述测试钉死**，本轮复核：`.cs` 生产面已无供应商键字面量残留（`grep` 命中只剩注释与旧键清理）。

**本轮新发现（文档缺口，两条）**：

1. `sms_providers.py` 是**唯一真源**，但全仓 `docs/**.md` 里只有 **1 个文件**提到它（`docs/TROUBLESHOOTING.md`）。
   对比：`phone_reuse` 出现在 17 个 md、`paypal_link` 20 个 —— **最新落地的架构基石文档足迹最小**。
2. `docs/directory-map.md:58` 的「Mailbox and phone inventory」行原本只列举了 `sms_tool/sms_provider.py`（该文件 09-23 已改名为 `sms_provider_adapter.py`），
   **没有列 `sms_tool/sms_providers.py`**（两个名字只差一个字母，见 N3）。
   ⚠️ 原因：`sms_providers.py` 目前**未跟踪**（§1），所以它连覆盖率测量的分母都进不去 ——
   提交后「60」本会变成 61。✅ **已落地 09-23**：该行已补 `sms_providers.py` 并写明分工
   ⇒ 提交后分母 241、命名 181、**未归属仍是 60**，不产生新的缺口。

### 3.5 ⑤ config.json —— 判据措辞不严（N5），但机制本身很扎实

**先给结论：这个模块的设计比预期好，本轮的发现是一条「措辞」而不是「漏洞」。**

**机制实测**（三层，互相独立）：

| 层 | 机制 | 实测 |
|---|---|---|
| 归属真源 | `config_schema.json` 是**跨语言分片归属清单**（不是 JSON Schema） | `config_schema_check.py` 三方比对：Python `SHARD_OWNERSHIP` ↔ C# `ConfigStore.ShardOwnership` ↔ JSON 清单，**`passed`** |
| 加载优先级 | 任一分片存在即权威，`config.json` 只在**无分片时**作为迁移输入 | `config.py:166` 早退逻辑；`docs/current/configuration.md` §Decision 逐条写明 |
| 死键检测 | `config_usage.unread_config_keys()` 从源码**重算**，`tests/test_config_usage.py` 全量钉住 61 个 | 实测 61 个未读键，**与 `EXPECTED_UNREAD` 完全一致** |

**N5 的实测**：`config_usage.py:46` 的 `SOURCE_DIRS = ("sms_tool", "services")` —— **不含 `SmsWorkbench/`**。
于是「unread = 源码里没有任何字符串字面量」这句话的前提**只覆盖 Python**。

- 逐键比对 C# 生产源码的字面量：61 个未读键里有 **1 个假阳性** —— `runtime.python_path`。
- 它在 C# 侧被读 **3 处**：`DesktopReadClient.cs:465`、`PythonBackendClient.cs:29`（`settings.GetString("runtime.python_path", "python")`）、
  `SettingsCatalog.cs:188`（作为设置项暴露给操作员）。
- ⇒ `doctor` 会告诉操作员「这个键设了没用」，而桌面端正在用它。
- **影响面小**（1/61），修法二选一：① 把该键从 `EXPECTED_UNREAD` 移到「C#-only」白名单并改措辞为「Python 侧不读」；
  ② 把 `SmsWorkbench/**/*.cs` 纳入 `source_string_literals` 的扫描根。

**附带观察（不是缺陷，但值得记录）**：61 个未读键里 **55 个（90%）集中在支付段**
（`paypal` 24 · `omakse` 13 · `paypal_nocard` 8 · `protocol_payments` 5 · `upi` 5），
而 `config_key_ratchet` 只冻结了 `registration`（1 个未读）与 `email_registration`（1 个未读）。
**两个门禁的覆盖面与死键的分布正好相反** —— 棘轮守的是最干净的两段。
因为未读键集合已被 `EXPECTED_UNREAD` 全量钉住，**这不是漏洞**（新增死键会红），
但意味着「哪一段最需要棘轮」这个问题上的答案是**支付段**，而它恰好没有。
另：`paypal_auto` 被 `CFG.get("paypal_auto")` 读取却不在 `SHARD_OWNERSHIP` 里 ——
这是**已决策并有文档**的（`configuration.md` §Egress 明写整条 PayPal 支付泳道休眠），本轮不重报。

### 3.6 ⑥ 协议支付面板 —— 命名空间最大（N3），结构无新增项

**规模**：32 个模块 / **14,667 行**，占六模块的 52%。加上 3 个子包（`pay_link/` 7 · `paypal/` 8 · `paypal_link/` 3）
与 `services/protocol-payment/`（14 个独立子进程提取器），支付是本仓最大的单一域。

**结构判定为健康的部分**：

- `paypal/` 内部是**单向无环的七层**（orchestrator / flow / form / session / dom_fields / config_picker / errors），
  `errors.py` 是无依赖叶子（Rule 5），Rule 6（执行层不许重新生成链接）已由 09-12 修复并守住。
- `paypal_auto.py` 已从 1.9k 行 god file 拆成 **27 行 re-export 壳**；`payment_link_manager.py` 同理（49 行壳）。
- 提取器同构度是**已决策项**且被 `extractor_parity_report.py` ratchet 住（当前全绿）；
  `upi_link.py`（2893 行，sms_tool 最大模块）昨日已做零改动测量并**判定不立项拆分** —— 本轮不重报。

**本轮新发现**：N3（扁平命名空间 + 近似命名对），见 §4.2。

---

## 4. 跨模块共性（本轮新发现）

### 4.1 N2：`docs/directory-map.md` 内部自相矛盾 —— 改名残留，且门禁对它零覆盖

**实测**：

| 位置 | 内容 |
|---|---|
| `docs/directory-map.md:11` | 「…, read-only **SMSBower** catalog adapter, …」 |
| `docs/directory-map.md:179` | 「Read-only provider metadata needed before launch belongs in a focused catalog module such as `SmsProviderCatalogClient.cs`」 |
| 实际文件 | `SmsWorkbench/SmsBowerCatalogClient.cs` **已删**（工作区 `D`）；`SmsWorkbench/SmsProviderCatalogClient.cs` 存在 |

**同一份文件里，第 11 行用旧名、第 179 行用新名。** 09-22 的供应商中立改名做得很干净：
`.editorconfig` 的段名已同步（`[**/SmsProviderCatalogClient.cs]`）、客户端内写死的 `DefaultEndpoint` 已下沉到 `SmsProviderCatalog.cs`、
`sms_providers.py` 的 docstring 用过去时描述这五处 —— **只有这一句散文漏了**。

**为什么门禁没拦住（这是本条的价值所在）**：`scripts/docs_consistency_scan.py` 只有两层判定 ——
「`` `path.py:NNN` `` 指针存在且行号在范围内」与「符号表里的符号真的定义在那行」。
实测 `grep -n "SmsWorkbench\|csproj\|\.cs"` 在该脚本里 **0 命中** ⇒
**文档里出现的 C# 类名是完全无门禁的散文**。本仓已经因同类问题吃过一次亏
（`.editorconfig` 的按文件名段在改名后静默失效，`2026-09-22.md` §1549 有记录）。

**建议**：① 改这 6 个词（零成本）—— ✅ **已落地 09-23**：`docs/directory-map.md:11` 改为
"read-only SMS provider catalog adapter"，与 `:179` 的 `SmsProviderCatalogClient.cs` 一致；
② 给 `docs_consistency_scan.py` 加一条**弱检查**：文档里形如 `` `Xxx.cs` `` 的引用，必须能在
`SmsWorkbench/` / `SmsWorkbench.Contracts/` / `tests/SmsWorkbench.Tests/` 里找到同名文件。
这正好复用该脚本已有的「弱层只查存在性、不查语义」的设计（它的 docstring 明说弱层「必须永不因看不懂的指针而失败」）。
⬜ **② 未落地**（新增门禁需要自测「不会因看不懂的引用而红」，成本高于①，留待拍板）。

### 4.2 N3：支付域的扁平命名空间 —— 一个域铺了 40 个模块、5 种命名形态

**实测**：

```
sms_tool/ 顶层模块           137 个（48,449 行）
sms_tool/ 子包内模块         107 个
子包数量                     11 个
```

**支付域一个域就占**：顶层 `payment_*.py` 13 个 + `paypal_*.py` 9 个 = **22 个扁平模块**，
再加 3 个子包（`pay_link/` 7 · `paypal/` 8 · `paypal_link/` 3）⇒ **40 个模块**，
用了 **5 种命名形态**：`payment_*` / `paypal_*` / `pay_link/` / `paypal/` / `paypal_link/`。

**近似命名对（都在同一命名空间，实测行数）**：

| 对 | 行数 | 实际职责 | 混淆后果 |
|---|---|---|---|
| ~~`sms_provider.py` vs `sms_providers.py`~~ | ~~43 / 225~~ | ✅ **09-23 已消除**：契约改名为 **`sms_provider_adapter.py`**，`_adapter` 后缀把身份写进文件名 | 已消除（原风险：改错文件 —— 往契约里加供应商，或往注册表里加生命周期方法） |
| `pay_link/` vs `paypal_link/` | 7 模块 / 3 模块 | **多支付方式**通用注册表（blik/ideal/twint/momo/kakao…） vs **PayPal 专用**链接生成 + 对账 | 找 PayPal 逻辑找进通用注册表（或反之）；`paypal_reconciliation.py` 只有 7 行，实体在 `paypal_link/reconciliation.py`(1305) |
| `paypal_reconciliation.py` vs `paypal_link/reconciliation.py` | 7 / 1305 | 兼容壳 vs 实体 | 同上 |

**后果不是「错」，是「改错文件」** —— 这类缺陷不会让任何门禁变红。

**建议（成本从低到高，需拍板）**：
1. **零风险** ✅ **已落地 09-23**：在 `docs/directory-map.md` 的 `pay_link/`（第 68 行）与
   `paypal_link/`（第 70 行）两行各加了交叉引用「**不要与 `pay_link/` 混淆**，后者是多支付方式通用注册表」；
   「Mailbox and phone inventory」行补了 `sms_providers.py` 并写明「加供应商要改注册表、不是契约」。
2. ✅ **已落地 09-23（老板拍板执行，覆盖本报告原「建议先不动」的意见）**：
   `sms_provider.py` → **`sms_provider_adapter.py`**，测试同步改名
   `test_sms_provider_pure.py` → `test_sms_provider_adapter_pure.py`。
   实测**跨仓改名只波及 6 处活代码**（比预估的「中风险」低：无 C# 侧引用、无动态 import、
   无第二处棘轮键控），逐项清单与验证见 §5.1。

### 4.3 N4：把「60 个未归属模块」从数字变成清单

`docs/directory-map.md:107-122` 自己记着：**tracked `sms_tool/**.py` 240 · 至少一行命名 180（75%）· 无一行命名 60**，
并明确警告「**别拿覆盖率当门禁**，先归属完这 60 个，那个数字才有意义」。
本轮把「60」**首次按族拆开**（复现方法见 §6 坑清单，两处 glob 语义坑）：

| 族 | 个数 | 代表 |
|---|---|---|
| `(root) mailbox_*` | 9 | `mailbox_gmail/graph/icloud_url/remail/smailr/cfworker/poll/quarantine/errors` |
| `accounts/` | 7 | **`account_health.py` · `account_health_queue.py`** · `account_identity/lifecycle/events/creation/models` |
| `(root) payment_*` | 6 | `payment_adapters/catalog/contracts/egress/operation/batch_setup` |
| `(root) paypal_*` | 4 | `paypal_extract/fingerprints/authorization/authorization_queue` |
| `(root) proxy_*` | 4 | **`proxy_entry.py`（代理字符串唯一权威！）** · `proxy_bridge/health/routing` |
| `sentinel/` | 4 | 整包 |
| `paypal_link/` | 3 | 整包 |
| `(root) registration_*` | 2 | `registration_checkpoint.py` · `registration_finalize.py` |
| 单件 | 21 | `upi_link.py` · `desktop_ipc.py` · `config_usage.py` · `chatgpt_bootstrap.py` · `codex_oauth.py` · … |

🔴 **两个高价值实例**：
- `proxy_entry.py` 是 **Rule 13 的代理字符串唯一权威**（`parse_proxy` / `rebuild_proxy_credentials` / `retarget_region` / `rotate_session` / `infer_region` 的唯一所有者），
  却不在目录表任何一行；
- `accounts/account_health.py` + `account_health_queue.py` 是 **账号测活**的契约与队列所有者（§3.3），同样不在。

**可执行化建议**：优先补 **4 行**（mailbox 族 / accounts 族 / payment+paypal 族 / proxy 族），
一次把 60 降到约 30，且覆盖的恰好是「有专属架构文档但没有目录归属」的那几个族。
**注意**：这 4 行要写成**具名清单**而不是新 glob —— 本轮的实测证明 glob 语义（git pathspec vs shell vs `pathlib`）
会让同一个模式算出三个不同的数（113 / 127 / 180，见 §6）。

---

## 5. 建议落地顺序

| 序 | 动作 | 成本 | 风险 | 需拍板 |
|---|---|---|---|---|
| 1 | **提交时用 `git add -A`**（N1）。本仓有并行提交者 ⇒ 提交前重拉 `git status` | 零 | 零 | 是（git 写操作） |
| 2 | ✅ **已落地 09-23**：改 `docs/directory-map.md:11` 的 "SMSBower catalog adapter"（N2） | 极小 | 零 | 否 |
| 3 | ✅ **已落地 09-23**：目录表给 `pay_link/` / `paypal_link/` / `sms_provider_adapter.py` / `sms_providers.py` 加交叉引用（N3-1） | 极小 | 零 | 否 |
| 4 | ⬜ `account-health.md` 顶部注明「本文件同时是 promotion 的契约文档」（§3.2） | 极小 | 零 | 否 |
| 5 | ⬜ 修 `runtime.python_path` 的假阳性措辞（N5） | 极小 | 零 | 否 |
| 6 | ⬜ 目录表补 4 个族行（具名清单），60 → 约 30（N4） | 低 | 零 | 否 |
| 7 | ⬜ `docs_consistency_scan.py` 加「文档提到的 `Xxx.cs` 必须存在」弱检查（N2-②） | 低 | 低（需自测「不会因看不懂的引用而红」） | 否 |
| 8 | ✅ **已落地 09-23**：`sms_provider.py` → `sms_provider_adapter.py`（含测试改名） | 中 | 零（实测全绿，见 §5.1） | 是（已拍板） |

**落地后的门禁复核**（本轮实测，全绿）：`line_ending_guard --all` = `no mixed line endings among 875 tracked file(s)` ·
`docs_consistency_scan` = `passed` · `tests/test_audits_readme_index.py` = `2 passed`（新增报告已入 `docs/audits/README.md` 索引，计数 41 → 42）。
三个改动文件实测**纯 LF**（CRLF=0）。

### 5.1 第 8 项落地记录：`sms_provider.py` → `sms_provider_adapter.py`（09-23）

**为什么改名**：`sms_provider.py`（单数，适配器契约）与 `sms_providers.py`（复数，供应商注册表真源）
只差一个字母，是 §4.2 里最容易被改错的一对。`_adapter` 后缀把身份写进文件名，
消除「往契约里加供应商」这类错误的前提。类名 `SmsProviderAdapter` 与函数 `provider_name` **不变**。

**改动面（实测 6 处活代码，逐条验证）**：

| # | 文件 | 改动 |
|---|---|---|
| 1 | `sms_tool/sms_provider_adapter.py` | `git mv` 自 `sms_provider.py`（内容零改动，43 行） |
| 2 | `sms_tool/phone_reuse.py:40` | import 路径 → `from .sms_provider_adapter import SmsProviderAdapter, provider_name` |
| 3 | `tests/test_sms_provider_adapter_pure.py` | `git mv` 自 `test_sms_provider_pure.py`；1 行 import + 24 处 `sms_provider.` 限定名 + 3 处 docstring 路径 |
| 4 | `tests/test_phone_reuse_smsbower.py:13` | import 路径 |
| 5 | `scripts/unused_import_baseline.json` | 见下（陈旧键清理） |
| 6 | `docs/directory-map.md:58` | 具名清单 / 命名契约段 / `git ls-files` 命令列 / 测试指针 共 4 处 |

**改名面比预估小**：无 C# 侧引用（C# 用的是 `SmsProviderCatalog`，另一条命名线）、
无动态 import（`import_module` / `__import__` 全仓零命中）、无第二处按路径键控的基线
（只有 `unused_import_baseline.json` 一处）。

**顺带清掉一条陈旧基线键**：`"sms_tool/sms_provider.py": 1` 实测已不成立 ——
`ruff check --select F401 sms_tool/sms_provider.py` = **`All checks passed`**（该文件的未用导入
在 09-22 中立命名重构中已被清掉，基线未跟着降）。棘轮只判「增长」，陈旧键**既不红也不提示**，
却让 `total` 虚高 1（456 vs 实际 455）。改名时一并删除该键并锁紧 `total` 456 → 455（per_file 84 → 83）。
🔴 这是**「锁住已获得的缩减」**（棘轮 docstring 明确鼓励的动作），**不是**「更新基线消红」。

**验证（全绿）**：

| 门禁 / 测试 | 结果 |
|---|---|
| `unused_import_ratchet.py` | `OK (455 <= baseline 455, 83 files)` —— 基线已锁紧 |
| `docs_consistency_scan.py` | `passed (release-v2026.09.14.md)` |
| `pytest -q` 定向 4 文件 | `135 passed, 52 subtests passed` |
| 全量 `pytest -q` | `4942 passed / 6 skipped / 1054 subtests`（0 failed，453 s）。相对 §0 的 4935 是 **+7 = 第 4 项新增的 `CSharpConsumerTests`**；**改名本身 0 变化** |

**为什么不改历史 audit 快照**：`audit-2026-08-31-*`、`architecture-before-registration-hardening-2026-09-06.md`、
`audit-2026-09-02-round5-*` / `round6-*` 与 `.workbuddy-ai/memory/2026-09-03.md` 里的 `sms_provider.py`
是**当时的事实记录**，改了会让「某轮审计当时的文件名」变成假的。仓库惯例：历史快照不改。
同理 `dist/installer/package/**` 是构建产物副本。

**为什么不留兼容壳**：`sms_provider.py` 无对外 API 承诺，留壳会让「两个名字并存」的混淆继续存在，
违背改名初衷；且本仓已记录过「兼容壳让 monkeypatch 测试静默失效」的教训（09-22 `account_recovery` 壳）。

---

## 6. 坑清单（本轮踩到的，都出自**我自己的探针**）

1. 🔴 **同一份目录表，三种 glob 语义会算出三个不同的数** —— 想复现「60 个未归属」时：
   - `subprocess` 直接把带引号的模式传给 git ⇒ 引号成字面量、glob 不展开 ⇒ **127**（虚高）
   - `pathlib.Path.glob` ⇒ shell 语义，`*` **不跨 `/`** ⇒ **113**
   - 让 **git 自己展开 pathspec**（git 的 `*` **跨 `/`**）⇒ **180 / 60**，与文档一致
   **正确做法**：把模式原样交给 git（`git ls-files <pattern>`），不要自己展开。
   这也解释了目录表为什么要专门写一段「引号 glob」的警告 —— 那段话是真踩出来的。
2. **「文档说了 60」不等于「我能复算 60」**：复算前先确认自己的展开语义与文档一致，
   否则会得出「文档记错了」的错误结论（我第一版就是 127，差点据此报告文档陈旧）。
3. **未跟踪文件不在 `git ls-files` 里** ⇒ 任何基于 `git ls-files` 的覆盖率测量，
   都会把「新写但没提交」的模块**静默排除在分母之外**。本轮 `sms_providers.py` 就是这样消失的
   —— 提交后「60」会变成 61。**测量未提交的工作区时，分母要单独确认。**
4. **「未读配置键」的判据是 Python-only**（`SOURCE_DIRS` 不含 `SmsWorkbench/`）——
   拿它当「没人读」的证据前，必须补一次 C# 侧字面量扫描（本轮扫出 1 个假阳性）。
5. **棘轮基线会有「陈旧键」，而且它不会红**：某文件早已清空未用导入，基线里那条键却还在。
   棘轮的判据是 `n > allowed.get(path, 0)`，陈旧键既不在当前计数里、也不触发任何提示，
   只让 `total` 虚高。本轮改名时才发现 `"sms_tool/sms_provider.py": 1` 与实际不符（实测 0 个 F401）。
   ⇒ **动某个文件的路径或内容时，顺手 `ruff check --select F401 <该文件>` 复算一下它的基线计数。**
6. **改名前先查三件事，能省掉大部分「跨仓改名」的风险**：① 有没有 C# / 其他语言侧引用
   （本轮零）；② 有没有**动态 import**（`import_module` / `__import__` 字符串形式，本轮零）；
   ③ 有没有**第二处按路径键控的基线/配置**（本轮只有 `unused_import_baseline.json` 一处）。
   三者都为空时，改名只是「import 行 + 文档」的机械替换。

---

## 7. 已驳回 / 不重复报告

| 项 | 为什么不报 |
|---|---|
| 8 个静态 SCC 环 | 09-22 已分类：**全部由懒加载/门面断开，0 个 import-time 环**。「8 个循环依赖」这个说法本身是错的 |
| `upi_link.py`（2893 行）拆分 | 09-22 已做零改动测量，**判定不立项**（它是自足实现，与提取器家族不共享代码） |
| 提取器同构（blik/ideal/twint/momo/kakao） | 已决策 + 被 `extractor_parity_report.py` ratchet 住，当前全绿 |
| `paypal_auto` 不在分片映射 ⇒ 支付泳道休眠 | `docs/current/configuration.md` §Egress **明写**，是已决策项 |
| PayPal 结账短信无号源（`no_number_source`） | 同上，且被 `tests/test_paypal_sms_lane_removed.py` 钉住；三处闸门失败行为**故意不一致**也有记录 |
| `config.json` 是死文件（有分片即权威） | 已决策；本轮实测：它的顶层键集合与三个分片**当前完全一致**（无漂移），但代码永不读它 |
| 61 个未读配置键 | 已被 `tests/test_config_usage.py` 全量钉住，**不是漏洞**；本轮只报它的分布形态与 1 个假阳性 |
| `account_recovery` 兼容壳 / `local/` 未收录 / `docs/audits/README.md` 计数 / F401 缺口 | 09-22 均已落地 |
| C# 死成员、`account_deatived` 拼写 | 09-22 已驳回（含三处对既往结论的自我纠正） |
| `gpt-register-pro` 的 10 项借鉴 | 09-22 已全部处置完毕；且**参考仓库无新提交**（HEAD 仍 `7b2304f` / 09-19） |
| `services/protocol-payment/` 的 vendor 边界 | Rule 10 已守住（零 import 级耦合），本轮 `grep` 复核仍成立 |

---

## 附：本轮探针

`runtime/tmp/a9_dir_map_gap.py`（未入库，只读）—— 把目录表的「未归属模块」缺口按族细分。
写入 `runtime/`（已忽略），未触碰任何跟踪文件。
