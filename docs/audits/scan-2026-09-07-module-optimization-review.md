# 模块优化扫描报告（2026-09-07）

针对**协议注册 / 无头浏览器注册 / 查优惠 / 账号测活**四个模块，从
**代码架构 / 模块功能 / 文档规范 / 目录结构**四个维度扫描。

> 定位：本次为**只读扫描 + 建议**，未修改任何生产代码。
> 与既有文档的关系：`docs/audits/architecture-before-registration-hardening-2026-09-06.md`
> 是 09-06 硬化前的快照，`docs/adr/0009-registration-hardening.md` 是硬化决策。
> 本报告是硬化**之后**的状态复查，并覆盖 ADR-0009 未涉及的支付/测活/邮箱方向。

---

## 0. 结论摘要

| 维度 | 现状 | 最要紧的一项 |
|---|---|---|
| 代码架构 | 分层骨架清晰、**已无 import 环**，但依赖注入与判据收敛是靠"宽接口 + 延迟 import"实现的 | `RegistrationOperations` 59 字段 + `globals()` 抓 56 个依赖 |
| 模块功能 | 四个模块各有"一处臃肿核心" | `run_browser_registration()` **571 行**，全项目最长函数 |
| 文档规范 | 体系成熟（ADR 0001–0009 + 活文档 + 落码规则），但**代码内文档**薄弱 | 函数 docstring **24%**，全参注解 **42%** |
| 目录结构 | `sms_tool/` 根目录平铺 **141 个 .py**；子包已拆但**反向依赖父壳 8 处** | `pay_link/` 六个子模块全部反向 import 父壳 |

**规模基线**：`sms_tool/` 213 个 .py / **64,353 行**；16 个文件 >800 行，占代码量 26%。

---

## 1. 代码架构

### 1.0 循环依赖：实测结果（含一次自我修正）

用 AST 构建模块级依赖图后跑 Tarjan SCC，**分两张图**分别统计：

```
图 A（仅顶层 import）                 环：0 个
图 B（顶层 + 函数内延迟 import）       环：3 个
```

> **⚠️ 方法学修正**：本次扫描初稿曾得出"含延迟 import 也是 0 环"的结论，
> 经复核是脚本缺陷——SCC 函数内部写死了引用顶层图，第二张图从未被使用。
> 修正后重算得到 3 个环。**下面以修正后的结果为准。**

三个真实存在的环（全部靠函数内延迟 import 维持）：

| # | 环 | 关键边 |
|---|---|---|
| 1 | `checkout_contract → payment_catalog → payment_flow` | `checkout_contract.py:10` 顶层 import `payment_catalog`；`payment_catalog.py:193` / `:194` **函数内** import `checkout_contract`、`payment_flow` |
| 2 | `payment_routing ↔ paypal_proxy` | 双向均含延迟 import |
| 3 | `sentinel.client ↔ sentinel_tokens` | 双向均含延迟 import |

对比 09-02 第五轮审计的"**7 个 import 环仍在，靠延迟 import 糊住**"：
环的数量从 7 降到 3，且**顶层图已完全无环**——ADR-0009 的显式依赖改造确有成效。
但"靠延迟 import 糊环"的机制本身没有变，环只是被推迟到运行期。

**结论**：顶层无环是真实进步；但不要据此认为"依赖问题已解决"。

### 🔴 1.1 代价：229 处函数内延迟 import

```
函数内延迟 import：229 处 / 84 个模块
  account_recovery        15 处
  cli                     14 处
  registration_handlers   11 处
  commands/accounts       11 处
  commands/payment         9 处
  commands/registration    9 处
```

**影响**：延迟 import 掩盖了真实的依赖方向，静态检查（依赖图、分层守卫、IDE 跳转）
全部失效；真正的循环依赖风险只是被推迟到运行期，而非消除。

**建议**：不必急于清理（风险高、收益慢），但应把"延迟 import 数量"做成**棘轮指标**——
新增不得超过当前值。可复用 `tests/conftest.py` 已有的 AST 守卫模式。

### 🔴 1.2 `RegistrationOperations`：59 个字段的"显式依赖"

`sms_tool/registration_operations.py`（77 行）是一个 frozen dataclass，
字段从 `REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS` 到 `validate_config` 共 **59 个**，
且调用方用 `globals()` 一次性抓取：

```python
# registration_operations.py:75-77
@classmethod
def bind(cls, namespace: Mapping[str, Any]) -> "RegistrationOperations":
    return cls(**{item.name: namespace[item.name] for item in fields(cls)})
```

ADR-0009 自己在 Consequences 里承认：

> "The operations Interface still exposes many helpers; grouping those into
> higher-level operations and immutable stage results can follow independently."

**这是官方留口子的下一步，不是新发现。** 现状是：59 个扁平字段里混杂着
邮箱 OTP、sentinel 设备指纹、HTTP 头构造、timing、代理解析、随机身份生成等
**至少 6 个语义组**，全部平级暴露。

**影响**：
- 新增一个注册环节就要改 dataclass + 所有 bind 站点；
- `bind()` 用 `namespace[item.name]`，**漏一个键就是 KeyError，且发生在调用时**；
- 无法从接口看出注册流程的阶段结构。

**建议**：按 ADR-0009 的规划，把 59 个字段收敛成 5–7 个**阶段级 operation 对象**
（如 `MailboxOps` / `IdentityOps` / `SentinelOps` / `TransportOps` / `TimingOps`），
`RegistrationOperations` 只持有这 5–7 个字段。这一步**可以先只加分组、不改调用语义**，
风险可控。

### 🟡 1.3 分层违规：9 处底层反向 import 上层

以"基础设施层不得依赖业务/编排层"为判据，实测：

| 底层模块 | 反向 import | 类型 |
|---|---|---|
| `http_client` | `registration_policy` | **顶层** |
| `config` | `registration_drivers.base` | 延迟 |
| `config` | `payment_flow` | 延迟 |
| `config` | `payment_catalog` | 延迟 |
| `store.accounts` | `account_models` | **顶层** |
| `store.accounts` | `account_events` | 延迟 |
| `store.markers` | `account_health` | 延迟 |
| `store.normalize` | `payment_link_manager` | 延迟 |
| `store` | `accounts` | **顶层** |

**最刺眼的是 `http_client → registration_policy`（顶层）**：HTTP 客户端是注册、
支付、邮箱共用的最底层，它去 import 注册策略，意味着任何注册策略的改动都会波及
全部 HTTP 调用方，且形成事实上的隐式环（只因 `registration_policy` 没 import
`http_client` 才没成环）。

`config → registration_drivers.base` 是 09-02 就点名的 C 环残留（当时已确认存在），
现在仍是延迟 import 形态。

**建议**：优先断 `http_client → registration_policy`（顶层，唯一会真正成环的），
方式是把注册策略里被 http_client 用到的那部分**下移**到 `http_client` 或独立的
`http_policy` 模块。其余 8 处保持延迟 import 现状，纳入棘轮即可。

### 🟡 1.4 子包反向依赖父壳：8 处

`docs/README.md` 说得很清楚（子包是 2026-08-31 机械拆分），但拆分遗留仍在：

```
pay_link/adapters     -> sms_tool.pay_link (父壳)
pay_link/base         -> sms_tool.pay_link (父壳)
pay_link/core         -> sms_tool.pay_link (父壳)
pay_link/normalize    -> sms_tool.pay_link (父壳)
pay_link/persistence  -> sms_tool.pay_link (父壳)
pay_link/registry     -> sms_tool.pay_link (父壳)
browser_flow/orchestrator -> sms_tool.registration_drivers (父壳)
browser_flow/orchestrator -> sms_tool (父壳)
```

**这一项有已知的高风险面**：本项目测试高度依赖"通过壳在调用时重读属性"来注入
patch，任何"消除壳间接层"的重构都会让 patch 静默失效（单独跑绿、全量跑红）。
09-02 删 `_plm` 24 处 → 8 个测试红，改 42 处 patch 才过。

**建议**：**不要**为了图层整洁去动这 8 处。若要动，必须先确认每个子模块
`from 父壳 import X` 的 X 是否被测试 patch 过（排查手法：给可疑函数挂哨兵
raise + 打 args，跑一次全量）。

### 🟡 1.5 重复实现：同一语义多处各判一次

扫描发现（细节见各模块章节）：
- **token 提取**：`account_seed.extract_access_token`（3 来源、要求 `isinstance(str)`）
  与 `k12_identity._extract_access_token`（12 来源、一律 `str()` 强转）行为不一致
  （既有结论，本次未重复验证）；
- **账号"是否失效"判定**：`account_liveness._is_token_invalid` /
  `account_recovery._probe_is_token_invalid` / `account_scan._token_probe_is_invalid`
  **三个同名异义的函数**（本次实测确认），详见 §2.5；
- **"是否可重试"判据**：集中在**支付链路**而非协议注册——
  `gcash_transport.py` 一处 8 条、`gcash_provider.py` 5 条、`payment_batch.py` 3 条，
  而统一的终局判定层 `pay_link/normalize.py`（87 用例 / 37 变异）未被这些调用点复用；
- **邮箱 OTP 校验**：`_validate_email_otp` 有 3 份定义（本次实测确认），详见 §5.2。

---

## 2. 模块功能

### 2.1 超长函数（全项目 TOP）

单纯按行数排序，`>=150 行`标 🔴：

| 行数 | 位置 | 所属 |
|---|---|---|
| **571** | `registration_drivers/browser_flow/orchestrator.py:70` `run_browser_registration()` | 🔴 浏览器注册 |
| 472 | `payment_batch.py:81` `run_payment_batch()` | 🔴 支付 |
| 424 | `paypal_link/reconciliation.py:301` `reconcile_paypal_return()` | 🔴 支付对账 |
| 367 | `upi_link.py:365` `generate_upi_qr_link()` | 🔴 支付 |
| 367 | `cli.py:401` `main()` | 🔴 CLI |
| 357 | `batch_runner.py:87` `run_batch_impl()` | 🔴 注册批次 |
| 317 | `phone_registration.py:31` `run_phone_register()` | 🔴 手机注册 |
| 313 | `paypal_link/gen_link.py:524` `generate_pp_link()` | 🔴 支付 |
| 305 | `account_recovery.py:37` `refresh_local_quota_statuses()` | 🔴 **账号测活** |
| 304 | `wallet_provider.py:302` `run_wallet_provider()` | 🔴 **查优惠/支付** |

**`run_browser_registration()` 571 行是浏览器注册的唯一编排入口**
（`docs/README.md` 规定浏览器流程改动都落在这里）。

> **⚠️ 修正一项建议**：初稿曾建议"优先切分其中的异常恢复逻辑"。
> 深入阅读后**收回该建议**——见 §2.3。这个函数虽然长，但注释质量高、
> 是**有意为之的线性编排脚本**，且异常恢复已被提取成独立的纯策略层。
> 机械按行数切分反而会损害可读性。

### 2.2 协议注册

已实测确认的两项（详见 §5）：

- 🔴 59 字段 DI + `bind()` 依赖注入（§1.2）
- 🔴 `_validate_email_otp` **三份定义并存**，签名与返回类型各不相同（§5.2）

其余为规模/结构观察：`registration.py` 扇出 29（全项目最高），
`commands/registration.py:_persist_registration_result_core()` 166 行，
`auth_flow.py:_login_existing_account_with_email_otp()` 164 行、
`_prepare_signup_auth_state()` 157 行。

### 2.3 无头浏览器注册

这一块**初稿判断有误，已深入复查**。复查后的结论是：**驱动器抽象层做得相当好**，
真正的问题不在"函数太长"。

#### ✅ 先说做得好的（这些是 ADR-0003/0004 的成果，不应再动）

**驱动注册表是单一真相源**（`registration_drivers/base.py:54` `DRIVERS` dict），
5 个驱动文件都是 **12–27 行的薄壳**：

```
playwright.py 27 行   roxy.py 19 行   camoufox.py 12 行   cloak.py 12 行
base.py 140 行（BrowserDriverSpec + DRIVERS + 别名解析）
```

**外部会话是规整的模板方法模式**（`external_sessions/managed.py`，723 行）：

```
ConnectedPlaywrightSession（基类：连接/接管/关闭）
  ├─ CloakBrowserSession      (128)
  ├─ CamoufoxBrowserSession   (230)
  ├─ RoxyBrowserSession       (428)
  └─ AdsPowerBrowserSession   (645)
```

723 行对应 4 个反检测浏览器各自的启动流程（调 API、等就绪、注入配置），
属**领域固有复杂度**，不是设计缺陷。三个 `__enter__()` 分别 112/86/63 行同理。

**异常恢复已被提取为纯策略层**（`browser_flow/recovery.py`，39 行）：

```python
"""The browser orchestrator owns side effects; this module only classifies errors
and computes bounded retry/timeout decisions so the policy is testable without
launching Camoufox."""
```

仅 3 个纯函数（`is_navigation_retryable` / `stage_timeout` / `probe_pending`）+ `__all__`，
**这是全仓可测试性设计的正面范例**——初稿曾建议"优先切分异常恢复"，属误判，已收回。

#### 🔴 `run_browser_registration()` 571 行：长，但**不建议机械切分**

逐段阅读后，该函数内部注释解释了**为什么这么做**，例如：

- `orchestrator.py:431` — 首屏预热**故意放在 2FA 之后**，
  "2FA 注册保护账号，绝不能被装饰性预热流量延迟"；
- `orchestrator.py:468` — 注册国家记录是**镜像协议路径**，
  因为浏览器路径此前漏记导致 `registration_country=""`、无法归因地区；
- `orchestrator.py:307` — 卡在 OTP 后验证时跑**两轮** reload 而非一轮（实测健康运行约 200s）；
- `orchestrator.py:553` — catch-all 保留 traceback 进日志，但对外只给脱敏文本。

**判断**：这是一个"顺序即语义"的编排脚本，571 行的线性结构本身承载了
时序约束信息。按行数切成若干函数，会把这些隐含的时序约束**打散到调用关系里**，
反而更难维护。

**真正值得做的**是沿用 `recovery.py` 的模式：把其中**可独立测试的决策片段**
继续提取成纯函数（如"指纹 + 出口 geo 对齐"、"token + transport-unknown 是否保留账号"），
保持编排主体线性。这样既降行数又不破坏可读性。

#### 🟡 值得关注的两点

1. **`browser_fingerprint_pool.py` 567 行，生产消费者只有 2 个**：
   `browser_flow/orchestrator.py` 与 `account_liveness.py`。
   下层 `fingerprint_pool.py`（236 行）被 6 个文件使用——两者是**分层关系**
   （browser 层依赖 protocol 层），不是重复实现。
   但 567 行的上层只服务 2 个调用点，若后续不再扩展可考虑与下层合并。
2. **分支密度热点** `page_state.py` **14.3 if/百行**（433 行 62 个 if），
   与 `mail_otp.py`(15.1)、`cfworker_mailbox.py`(14.9) 同属全项目最高档。
   该文件同时是零测试覆盖模块（既有结论）。
3. `browser_flow/orchestrator` 反向依赖父壳 2 处（§1.4）——**不建议动**。

### 2.4 查优惠

**定位结论**：本项目的"查优惠" = 账号促销/试用资格探测，核心是
`account_promotion.py`（516 行），入口有三处：
`commands/accounts.py:213 check_promotion()`（CLI 手动）、
`commands/registration.py:129 check_registered_promotions()`（注册后自动）、
`cli.py:845 _check_promotion()`。

三者最终都收敛到 `account_promotion.refresh_promotion_statuses()`——**底层是统一的，这点很好**。
问题在外壳与判据：

- 🔴 **两套外壳参数不一致**：`check_promotion()` 传 `proxy_pool=`，
  而 `check_registered_promotions()` **不传** → 注册后自动查优惠**不走代理池**，
  手动查却走。同一批账号两种出口 IP 行为。
- 🔴 **`trial_eligible` 统计只在一处做**：`check_registered_promotions()` 自己算
  （读 `item["probe"]["plus_trial_eligible"]`），`check_promotion()` 不做 →
  同一份数据在 CLI 和自动流程里口径不同。
- 🟡 能力探测存在**四套互不兼容的键名**。
- 🟡 促销状态存在**新旧两套表示**（新 4 态 vs 旧布尔），落库与展示各用一套。
- 🟡 `payment_capability.payment_method_capability_probe()` 150 行 +
  `_paypal_capability_probe()` 136 行。
- 🟡 `load_payment_catalog()` 每次调用重读文件（87 行）。
- 🟡 超时常量存在三套。

> ⚠️ **工作区提示**：`account_promotion.py`、`commands/accounts.py`、
> `commands/registration.py`、`tests/test_account_promotion.py` 等 **19 个文件当前
> 处于未提交修改状态**，上述结论基于工作区现状，提交后需复核。

### 2.5 账号测活

- 🔴 `account_recovery.py:37 refresh_local_quota_statuses()` **305 行**
- 🟡 `account_scan.py:144 _scan_one()` 242 行、`scan_accounts()` 106 行
- 🟡 `workspace_scan.py:42 inspect_workspace()` 137 行
- 🟡 `account_liveness.py:141 probe_account_liveness()` 118 行、
  `browser_fetch_for_account()` 101 行
- 🟡 **`account_liveness` / `account_health` 被 20 个模块引用**（含
  `store/markers.py`、`config.py`、`registration_preflight.py`、
  `registration_outcome.py`、`payment_auth.py`）——扇入 12，改动波及面大
- 🟡 `store/markers.py` 分支密度 11.0 if/百行

**已知且已处理**：`account_health_queue.py:_pid_alive` 的 Windows 探活实现
（不能用 `os.kill(pid,0)`）仍然有效，未发现别处重踩。

---

## 3. 文档规范

### 3.1 代码内文档：量化结果

对 `sms_tool/` 213 个 .py 做 AST 统计：

| 指标 | 覆盖率 |
|---|---|
| 模块级 docstring | **85%** (183/213) |
| 类 docstring | **39%** (75/189) |
| 函数 docstring | **24%** (583/2373) |
| 函数返回注解 | **67%** (1611/2373) |
| 函数全参注解 | **42%** (997/2373) |

**30 个文件完全没有模块 docstring**，其中包含核心链路模块：

```
cli.py、auth_flow.py、codex_oauth.py、mailbox.py、mailbox_parsers.py、
mailbox_types.py、http_client.py、k12_client.py、k12_identity.py、
cpa_import.py、codex_export.py、agent_identity.py、paths.py、paypal_proxy.py ...
```

**建议**：不追求 100%，按"扇入 >=10 的模块必须有模块 docstring + 公开函数 docstring"
来收敛。按 §1 的扇入表，优先补：`config`(68)、`sanitizer`(31)、`paths`(30)、
`storage`(26)、`phone_proxy`(24)、`auth_headers`(21)。

### 3.2 日志：print 525 处 vs logger 79 处

```
print(            525 处 / 56 个文件
logger.            79 处 / 16 个文件

print 最多的文件：
  paypal_reverse.py        35
  phone_reuse.py           31
  nodriver_paypal.py       31
  commands/payment_links.py 30
  sentinel_tokens.py       27
```

`logging_setup.py` 已在 09-02 第六轮接线（`to_console=False`，因为 stdout 是 C# 的
IPC 通道），但**日志化改造远未完成**。可观测性最弱的正是资金链路
（`paypal_reverse` / `nodriver_paypal`）和手机链路（`phone_reuse`）。

> 工作区中 `logging_setup.py`、`registration_progress.py`、
> `SmsWorkbench/MainWindow.xaml`、`tests/test_log_display.py`(新增未跟踪)
> 正处于未提交修改中，说明这一项**正在被处理**，本报告不重复建议。

### 3.3 项目文档：体系成熟，无需重构

这一点值得明确肯定，避免"建议加强文档"的空话：

- `docs/README.md`（7.4 KB）有**完整的文档规则 + 落码位置约定**（2026-09-06 起），
  明确到"浏览器注册改动落 `browser_flow/`，协议注册改动落 `registration_handlers.py`
  等 focused modules，`registration.py` 只加 re-export 不加实现"。
- `docs/adr/` 有 **ADR-0001 ~ ADR-0009**，最新 ADR-0009（2026-09-06）记录注册硬化决策。
- `docs/current/` 提供当前状态入口（registration-architecture / configuration /
  registration-recovery / telemetry-and-runtime）。
- `docs/audits/` 归档所有历史快照，`docs/releases/` 一个 tag 一份不可变发布说明。

**唯一提示**：`docs/architecture.md` 只有 **5.6 KB**，而
`docs/audits/architecture-before-registration-hardening-2026-09-06.md` 有 **113 KB**。
活的架构文档比快照薄两个数量级——**快照里有大量结论没有回流到活文档**。
建议把 09-06 快照中的"Boundary Rules / Ownership Matrix / Dependency Direction"
三节提炼回 `docs/architecture.md`，否则新读者只能读到 113 KB 的历史快照。

### 3.4 🟡 历史发布说明与现状脱节：GoPay 案例

`docs/README.md:61` 记录 v2026.08.02 的一项变更是 "**GoPay removal**"，
但实测代码中 GoPay 仍然活跃：

```
24 处引用 / 10 个文件：
  wallet_provider.py、wallet_transport.py、payment_flow.py、payment_routing.py、
  checkout_contract.py、payment_batch.py、commands/payment.py、
  pay_link/adapters.py、pay_link/core.py、pay_link/registry.py
```

而现行 `docs/directory-map.md:61` / `:114` 明确把 GoPay 记为**现行能力**
（"Promotion/Update is supported by PayPal and by GoPay full/probe zero-due flows"）。

**判断**：这不是文档错误——项目规范本身就要求"历史发布说明不可回写"。
真正的问题是**没有机制标注"该历史条目已被后续变更取代"**，
读者看到 "removal" 会与代码产生矛盾理解。
建议在该条目后加一句 `(后续版本中 GoPay 以共享 wallet adapter 形式保留，见 directory-map.md)`。

### 3.5 🟡 工程化：无静态类型检查、无 lint

实测仓库根目录：

```
存在：pytest.ini、.github/workflows/ci.yml（1 个）
缺失：mypy.ini / pyproject.toml / setup.cfg / .ruff.toml / ruff.toml / .flake8
```

即**完全没有静态类型检查和 lint 工具链**。考虑到：
- 全参注解覆盖率仅 **42%**，函数 docstring 仅 **24%**；
- 已有 2,739 个 pytest 用例、且已建立 AST 静态守卫
  （`test_config_patch_seams.py`、`sensitive_policy_coverage.py`）的先例；

**建议**：按项目既有惯例——**新增检查写成 pytest 用例，而不是改 workflow**
（gh token 缺 `workflow` scope）。优先级最高的两条：
1. 用 AST 断言"扇入 >=10 的模块必须有模块 docstring"（对应 §3.1 的 30 个文件）；
2. 用 AST 断言"`pay_link/*` 不得新增对父壳的属性读取"（防止 §1.4 的反向依赖继续扩散）。

依赖锁定方面：`requirements.txt` 只 pin 了 `curl_cffi==0.16.0`，
其余为 `>=` 约束（`cryptography>=41.0.0`、`httpx[http2,socks]>=0.28.0` 等），
存在传递依赖漂移风险。

---

## 4. 目录结构

### 🔴 4.1 `sms_tool/` 根目录平铺 141 个 .py

按前缀聚族：

| 族 | 数量 | 代表 |
|---|---|---|
| `account_*` | **15** | 2fa / cleanup / health / liveness / scan / recovery / promotion ... |
| `payment_*` | **15** | adapters / auth / batch / catalog / executor / flow / routing ... |
| `mailbox_*` | **14** | mailbox.py / parsers / poll / quarantine / remail / strategies ... |
| `registration_*` | **14** | handlers / outcome / policy / preflight / progress / retry_guard ... |
| `paypal_*` | **9** | authorization / extract / proxy / reverse / fingerprints ... |
| `proxy_*` | 5 | pool / health / routing / entry / bridge |
| `codex_*` | 4 | oauth / export / phone / sentinel |
| `auth_*` | 3 | flow / headers / state |

已有子包：`commands`(10) / `providers`(10) / `registration_drivers`(22) /
`paypal`(8) / `pay_link`(7) / `store`(7) / `paypal_link`(3) / `sentinel`(4)。

**最成熟、收益最高的候选是 `account_*` 15 个**（测活/恢复/清理/改邮/事件/生命周期），
它们内聚性最强、跨族依赖最少。

**但有一个硬约束必须先考虑**：本项目大量使用 `from X import f` 按名导入，
一旦移动模块，所有 `patch("sms_tool.account_health.xxx")` 会静默失效
（单独跑绿、全量跑才红）。09-02 的教训是改 42 处 patch 才过。

**建议**：如果做，采用`__init__` re-export + 渐进去壳**的方式：
先建 `accounts/` 子包并把模块搬进去，根目录保留薄壳
（现状已有 6 个 mailbox 壳的先例，见 §4.2），测试 patch 目标不改也能过；
等测试全部迁到新路径后再删壳。**不要一次性移动。**

### 🟡 4.2 邮箱层的"模块自我替换"黑魔法

根目录 6 个 `mailbox_*.py` 是 148–222 字节的壳，内容是：

```python
# sms_tool/mailbox_graph.py
"""Compatibility facade for the Microsoft Graph mailbox provider."""
from .providers import mailbox_graph as _impl
import sys
sys.modules[__name__] = _impl          # ← 把自己从 sys.modules 里替换掉
```

实测 6 个壳都能正常 import，`sys.modules["sms_tool.mailbox_graph"]` 确实指向
`sms_tool.providers.mailbox_graph`。

**这个手法能工作，但有三个坑**：
1. **模块身份分裂**：`sms_tool.mailbox_graph` 与 `sms_tool.providers.mailbox_graph`
   变成同一个对象，`isinstance` / pickle / `inspect.getsourcefile` 都会给出意外答案；
2. **`from sms_tool.mailbox_graph import f` 的绑定发生在替换之前**，
   所以按名导入拿到的是真模块的函数，而属性访问走的是替换后的模块——
   两者在 patch 场景下行为不同；
3. 依赖"导入顺序"：谁先 import 决定 `sys.modules` 里的最终形态。

**建议**：保留壳（它们解决了真实的兼容问题），但**统一改成显式 re-export**
（`from .providers.mailbox_graph import *` 或逐项 `from ... import X`），
不要用 `sys.modules[__name__] = _impl`。§4.1 的新子包若要建壳，也用显式 re-export。

### 🟡 4.3 `providers/` 命名方向与分层相反

```
providers/cfworker_mailbox.py    591 行  ← 低层 HTTP 客户端
providers/mailbox_cfworker.py    108 行  ← 上层封装（from .cfworker_mailbox import CFWorkerMailboxClient）

providers/smailr_mailbox.py      421 行  ← 低层 REST 客户端
providers/mailbox_smailr.py      ...     ← 上层封装（from .smailr_mailbox import SmailrClient）
```

同一个 provider 的两层，文件名是**词序颠倒**的，且没有任何命名约定能看出
哪一个是低层、哪一个是上层。新人极易改错文件。

> 注：这两个"低层客户端"文件**不是死代码**，它们被同包的上层封装以相对导入使用
> （我第一次用 grep 统计时因未覆盖同级相对导入而误判，已修正）。

**建议**：统一为 `<provider>_client.py`（低层） + `mailbox_<provider>.py`（上层），
或直接用 `providers/<provider>/client.py` + `providers/<provider>/workflow.py`。

### ✅ 4.4 仓库卫生：良好，无需处理

```
git 跟踪文件总数        669
  dist/                   0   ✅
  runtime/                0   ✅（磁盘上 112,829 个文件，全部未被跟踪）
  sessions/               0   ✅（磁盘上 1,265 个文件，全部未被跟踪）
```

`.gitignore` 覆盖到位，构建产物与运行时数据没有污染仓库。**这一项无需动作。**

---

## 5. 附：协议注册专项结论（逐条标注验证状态）

### 5.1 🔴 DI 接口过宽 —— **已实测确认**

`RegistrationOperations`（`sms_tool/registration_operations.py`，77 行）是 frozen dataclass，
字段 59 个；`bind()` 从调用方 namespace 逐字段抓取：

```python
# registration_operations.py:75-77
@classmethod
def bind(cls, namespace: Mapping[str, Any]) -> "RegistrationOperations":
    return cls(**{item.name: namespace[item.name] for item in fields(cls)})
```

ADR-0009 已在 Consequences 中承认这是下一步。
**注意**：`bind()` 用 `namespace[item.name]`，缺键是**调用时 KeyError**，
不是构造时报错——新增字段时容易漏掉某个 bind 站点。

### 5.2 🔴 `_validate_email_otp` 三份定义并存 —— **已实测确认**

全仓有三个同名不同签名的定义：

| 位置 | 签名 | 返回 | 调用方 |
|---|---|---|---|
| `account_creation.py:46` | `(session, auth_base, base_headers, code, sentinel_data=None, use_sentinel=True)` | tuple | **生产主路径**：`auth_flow.py:5` 导入、`registration_handlers.py:710/730` 经 `registration` 门面调用 |
| `account_2fa.py:82` | `(session, code, base_headers)` | `str` | 仅 `account_2fa.py:327` 自用 |
| `http_utils.py:108` | 第三套 | tuple | **仅 `tests/test_http_utils_pure.py:331` 调用** |

**关键影响**：`http_utils.py:108` 这一份有 46 个用例 / 32 条变异守护，
但**生产代码目前不经过它**——生产走的是 `account_creation.py:46`。
也就是说，注册链路真正执行的 OTP 校验逻辑，与测试守护的那份**不是同一份**。

> ⚠️ 这不一定是 bug：`http_utils` 版可能是正在迁移中的统一实现。
> 但**现状必须明确**：要么把生产切到 `http_utils` 版，要么给
> `account_creation` 版补同等强度的测试。当前状态是"测了一份，跑的是另一份"。

### 5.3 🟡 存储层双轨：27 个走 `runtime_file`，12 个裸 `open()`

- 使用 `runtime_file`（统一路径层）的模块：**27 个**
- 仍直接 `open()` 的模块（各写各的）：**12 个**，含
  `registration_progress.py`、`sentinel_tokens.py`、`phone_reuse.py`、
  `payment_batch.py`、`captcha_solver.py`、`config.py`、`http_client.py` 等

这正是 09-03 必须加 `isolated_runtime` autouse 沙箱的原因——
只有走 `paths.runtime_dir` 的模块才会被沙箱重定位。

### 5.4 🟡 sentinel 能力分散在两个模块

```
codex_sentinel.py:34  import_cached_auth_cookies()
codex_sentinel.py:41  with_sentinel()
sentinel_tokens.py:89 _sentinel_device_id()
```

`sentinel_tokens.py` 有 **650 行**、27 处 `print`，是账号隔离安全契约
（`oai-did` 设备指纹剔除）的另一半实现，与 `codex_sentinel.py`（67 行）分开存放。

### 5.5 🟡 注册链路的超长函数

```
166 行  commands/registration.py:280  _persist_registration_result_core()
164 行  auth_flow.py:565             _login_existing_account_with_email_otp()
157 行  auth_flow.py:333             _prepare_signup_auth_state()
151 行  session_builder.py:20        build_session_file()
144 行  codex_oauth.py:338           _passwordless_login_and_exchange()
130 行  account_2fa.py:226           setup_totp_2fa()
```

### 5.6 ⚪ 本次**未验证**、不作为结论的项

以下条目在初稿中出现过，但本次扫描**没有取得确凿证据**，故不列为结论：

- "成功/可重试判据在协议注册链路六处各写一遍" —— 实测 `retryable` 的重复判定
  集中在**支付链路**（`gcash_transport.py` 一处 8 条、`gcash_provider.py` 5 条、
  `payment_batch.py` 3 条），协议注册链路未发现。见 §2.6。
- "阶段名四套并行体系"、"时间预算无统一函数" —— 未做穷举验证。
- "8 个模块各自 open() 写 runtime" —— 实际为 12 个（见 §5.3）。

---

## 6. 建议的落地顺序

按「收益 / 风险」排序，前 3 项建议先做：

| # | 事项 | 理由 | 风险 |
|---|---|---|---|
| 1 | 断 `http_client → registration_policy`（顶层反向依赖） | 唯一真正会成环的底层反向依赖；改动面小 | 低 |
| 2 | `RegistrationOperations` 59 字段按语义分组（ADR-0009 已规划） | 官方承认的下一步；可先加分组不改语义 | 中 |
| 3 | 统一查优惠三处入口的 `proxy_pool` 与 `trial_eligible` 口径 | 直接导致"自动查 vs 手动查"出口 IP 行为不一致 | 低 |
| 4 | `run_browser_registration()`：把可测的**决策片段**提成纯函数（沿用 `recovery.py` 模式），**不做按行数的机械切分** | 571 行线性编排，"顺序即语义"；机械切分会打散隐含的时序约束 | 中 |
| 5 | 把 113 KB 架构快照的 Boundary Rules 提炼回 `docs/architecture.md` | 活文档比快照薄两个数量级 | 低 |
| 6 | 补扇入 >=10 模块的模块 docstring（30 个文件中约 10 个） | 可量化收敛 | 低 |
| 7 | `providers/` 命名统一（低层 `*_client` / 上层 `mailbox_*`） | 消除改错文件的风险 | 低（纯改名） |
| 8 | `account_*` 15 个文件收进 `accounts/` 子包（**壳 + 渐进**） | 根目录平铺 141 个 | **高**（patch 失效面） |
| 9 | 229 处延迟 import 纳入棘轮（不清理，只禁止新增） | 防止依赖方向继续劣化 | 低 |

**明确不建议做的**：
- 不要动 `pay_link/` 的 6 处父壳反向依赖与 `browser_flow/orchestrator` 的 2 处——
  它们是测试的 patch 注入面（见 §1.4）；
- 不要为了"消除延迟 import"批量改 import 位置——收益低于回归风险；
- **不要按行数机械切分 `run_browser_registration()`**——它的线性结构承载时序约束，
  且异常恢复已是独立纯策略层（见 §2.3）；
- 不要重构 `external_sessions/managed.py` 的模板方法结构——723 行对应 4 个
  反检测浏览器的固有启动复杂度，抽象层次是对的。

---

## 7. 本次扫描的方法与口径

- 规模/文档统计：AST 解析 `sms_tool/**/*.py`（排除 `__pycache__`），
  脚本 `F:\tmp\scan20260907\stats.py`、`longfunc.py`。
- 依赖图：AST 提取 `Import` / `ImportFrom`（含相对导入层级还原），
  Tarjan SCC 求环，脚本 `F:\tmp\scan20260907\arch.py`。
  已处理已知的 BOM 问题（`phone_proxy.py` / `codex_oauth.py` 用 `utf-8-sig` 读取）。
- 已按项目既有铁律**排除 `dist/` 与 `runtime/` 源码副本**，
  否则 grep 结果会翻 3 倍且死代码结论全错。
- **未验证项**：本报告未做变异测试，未改任何生产代码；
  涉及行为变更的建议（如统一判据）实施前需先补测试。
