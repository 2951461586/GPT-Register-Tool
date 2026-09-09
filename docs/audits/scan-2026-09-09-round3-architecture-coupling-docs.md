# 第三轮扫描：注册/支付/测活/日志 —— 架构、耦合与文档规范

- 日期：2026-09-09
- 范围：协议注册、无头浏览器注册、批量支付链接提取、账号测活、查优惠、日志输出
- 性质：**只读审计**。本轮聚焦**代码架构 / 模块耦合 / 文档规范**，不复述前两轮已落地的 P0 正确性问题
  （见 `scan-2026-09-08-proxy-fingerprint-pool-refactor.md`、`scan-2026-09-08-round2-protocol-browser-registration.md`）
- 标注约定：**[确证]** = 已用命令实测或读到源码行；**[推断]** = 基于证据的推测

---

## 0. 结论先行

**有优化空间，但没有一条是"架构塌了"级别的问题。** 六个模块里：

1. **日志输出**问题最实——存在一个**已发生的数据泄漏**（邮箱明文落盘），且"两套日志体系互不相通"是三年积累的结构债。
2. **批量支付链接提取**次之——幂等判定有一处运算符优先级 bug，会把"钱可能已扣、结果未知"的行静默跳过；另有约 100 行确证死代码。
3. **账号测活 / 查优惠**——功能是好的，但存在**四层测活路径 + 至少三套状态枚举**的横向重复，以及一个"配置项写了但从未生效"。
4. **协议注册 / 无头浏览器注册**——抽象上一处硬伤（`bind(globals())` 动态装配），但错误处理分层是**对的，不要合并**。
5. **文档规范**——不是"缺文档"，是**文档在腐烂**：行号失效、符号消失、README 自相矛盾。

全部发现按性价比排序见 §7。**前 3 条（泄漏 / 幂等 bug / 动态装配）建议优先。**

---

## 1. 协议注册 + 无头浏览器注册

### 1.1 模块规模

| 文件 | 行数 | 判定 |
|---|---|---|
| `codex_oauth.py` | 1161 / 41 函数 | 大但内聚（单一域），**不建议动** |
| `registration_handlers.py` | **1079** | 唯一真正的上帝模块 |
| `external_sessions/managed.py` | 693 / 4 个 session 类 + 9 处函数内 import | 次之 |
| `registration.py` | 362（其中 200+ 行是兼容 import） | 有意保留 |

`registration_handlers.py` 混了 6 件事 **[确证]**：HTTP 协议调用（`:655` `request_with_retry`、`:798` create_account）、状态机推进（`:263` `_run_stage`）、落盘持久化（`:349` `_persist_checkpoint`）、2FA 编排（`:914` 内含两个闭包）、结果装配（`:982` finalize，85 行）、进度打印（全文 20+ 处 `print`）。

### 1.2 🔴 `RegistrationOperations.bind(globals())` —— 动态装配

**[确证]** `sms_tool/registration.py:264`：

```python
operations=RegistrationOperations.bind(globals()),
```

协议工作流的 55 个依赖全部从 `registration.py` 的 **globals 动态装配**。后果：

- 删掉顶部任一 import，静态分析（IDE、ruff、依赖图工具）**完全看不到这条边**；
- 要等到运行时 `registration_operations.py:191` 抛 `KeyError` 才暴露。

这是本轮在注册链路里找到的唯一"架构级"问题。

### 1.3 两条链路的抽象是否对齐

**[确证] `registration_drivers/base.py` 不是抽象基类，是注册表。** 136 行里只有 `DRIVERS` 元数据 + 别名表 + `BrowserRegistrationError`，**没有任何 `register()` 接口，无类继承它**。协议链路对它的全部使用只有 `registration.py:198` 的 `normalize_registration_driver`。

真正的分发点是 `registration.py:233` 的硬编码字符串分支 `if selected_driver != "protocol":` —— **不是多态**。

**正面发现**：下游接缝是齐的。`orchestrator.py:30-32` 两条链路共用 `build_registration_result` 与 `RegistrationStateMachine`。**接缝在结果层，不在驱动层**——这个设计实际上是合理的，不必为了"对齐"硬造基类。

**缺口**：`base.py:115` 的 `driver_capabilities` 7 个能力标志**只被写进遥测**（`orchestrator.py:587/617/642`），全仓无任何 `if capabilities[...]` 分支 —— 元数据无约束力，要么用起来要么删。

### 1.4 配置耦合

- 范围内约 133 处 `config.get(...)`，分散式。
- **[确证] `config_schema.json` 只有 794 字节**，内容是 `version/shards/ownership` —— **不是 value schema**，唯一消费方是 `scripts/config_schema_check.py:29` + 1 个测试。**没有任何 key 级校验。**
- **[确证] 读了不存在/拼错的键（全部有 `or default` 兜底，故静默失效）**：
  `fingerprint_pool`、`browser_profile_pool`、`batch_circuit_breaker_threshold`/`_enabled`、
  `proxy_scheme_fallback`、`require_registration_refresh_token`、`browser_proxy_country_check`。

### 1.5 ✅ 错误处理：这是正确分层，不要合并

范围是 5 个模块，但依赖**单向**且每份 docstring 都写明了切分理由：

```
error_classification.py  (纯分类, stdlib only)
   ← registration_policy.py:16   RETRYABLE_CLASSES / :40 advice
      ← registration_retry_guard.py:37  按邮箱·跨批落盘
         ← batch_circuit_breaker.py:34  按批·内存
backoff.py:12/20  独立 stdlib-only
```

**唯一缺口 [确证]**：`BatchCircuitBreaker` 只在 `batch_runner.py:150` 实例化，**单发路径不经过它**。

---

## 2. 批量支付链接提取

范围 17,578 行。Top5：`paypal_extract.py` 1178、`paypal_reverse.py` 1145、`payment_batch.py` 968、`wallet_provider.py` 918、`paypal_proxy.py` 812。

### 2.1 分层其实是清楚的（推翻"四份重复实现"的初判）

**[确证]** 是三层 + 一个 7 行 shim：

- L1 原语 `paypal_extract.py:277 class PPLinkExtractor`
- L2 编排 `paypal_link/gen_link.py:524 generate_pp_link`
- L3 门面 `pay_link/core.py:35 generate_payment_link`
- `gen_pp_link.py` 仅 7 行 `from .paypal_link import *`

三份 reconciliation 也只有一份真身：`payment_reconciliation.py`(34行门面) → `paypal_reconciliation.py`(7行shim) → `paypal_link/reconciliation.py:301`(1305行)。

**但有一个真问题 [确证，2026-09-09 已修复 → §13]**：`paypal_link/gen_link.py:1215` **又定义了一个同名的 `generate_payment_link`**，内部 `:1224` 反向 `from ..payment_link_manager import generate_payment_link as managed_generate` 转发。`paypal_link/__init__.py:153` 与 `pay_link/__init__.py:55` 各导出一份。**同名双实现共存**，后续维护者必然踩坑。
现已删除 `paypal_link` 侧的转发空壳与其导出，并加了门禁（详见 §13）。

### 2.2 🔴 `_checkpoint_row_resumable` 运算符优先级 **[确证，已复核]**

`sms_tool/payment_batch.py:868-874`：

```python
def _checkpoint_row_resumable(row):
    """Resume only completed success or an explicitly non-retryable failure."""
    contract = PaymentResult.from_mapping(row)
    if contract.outcome.requires_reconciliation or contract.outcome.side_effect_started and not contract.ok:
        return True
```

两个问题叠加：

1. **优先级**：Python 求值成 `A or (B and not C)`，而 docstring 的语义暗示 `(A or B) and not C`。
2. **语义矛盾**（更严重）：调用点在 `payment_batch.py:163-172` —— 返回 `True` 意味着**该行结果被复用、续跑时跳过**。于是 `requires_reconciliation=True`（**结果未知、钱可能已扣**）的行被判为"已完成"而**永久跳过**，既不会重试也没有对账入口。

docstring 说 "Resume only completed success or an explicitly non-retryable failure"，与 `requires_reconciliation → True` 直接冲突。

### 2.3 幂等与断点续跑：骨架是对的

**[确证]** `payment_operation.py:122-131` 用 `sha256(idempotency_key)` 落 `<hash>.json` + `CrossProcessSemaphore`（跨进程互斥，timeout=30）；`payment_batch.py:436` 传 `idempotency_key=f"{batch_id}:{account_ref}"`，跨批次稳定。断点续跑有 `_load_checkpoint:852` + `_batch_run_signature:878`（version=2，hash 覆盖 method/matrix/kwargs/proxy/retries）。

**推论 [推断]**：重试与幂等在最危险的分支上互斥 —— `payment_batch.py:432` 的重试循环复用同一 key，而 `payment_operation.py:195 _replay_allowed` 对 `side_effect_started` 返回 False → 已产生副作用的失败会撞 `PaymentOperationConflict`。防重复是对的，但意味着 `retries` 参数在这个分支上实际失效。**建议确认这是有意为之。**

**无 retention [确证]**：`runtime/payment_batches/` 已有 3879 个文件、`payment_operations/` 807 个、`payment_link_runs.jsonl` 4 MB（append-only）。

### 2.4 适配器体系

- **8 个适配器 / 15 个方法**（`payment_methods.json`），全部是 `FunctionPaymentAdapter` + 闭包 runner（`pay_link/registry.py:102-109`）。
- **[确证] 注册是静态的**：`registry.py:289 PAYMENT_ADAPTERS = build_default_payment_registry()` 模块导入时固化；`registry.py:155 register_payment_adapter` **零调用** —— 动态扩展是空头 API。
- **[确证] 有适配器绕过 base**：`adapters.py:381 RegionalPaymentAdapter` 是独立鸭子类型，不实现 `payment_adapters.py:11 PaymentAdapter` Protocol、不进注册表。
- 新增渠道成本：复用已有 kind → 改 2 处；新增 kind → 改 3~4 处。**可接受。**

### 2.5 耦合：与注册双向

**[确证]** 注册 → 支付：`commands/registration.py:23`、`batch_runner.py:9`、`registration_handlers.py:75/431/987`、`accounts/account_recovery.py:1109`、`accounts/account_promotion.py:149/186`。
支付 → 注册：`payment_batch.py:216/238/289`。**双向耦合。**

### 2.6 错误处理：两套不互通

**[确证]** 支付链路用 `PaymentResult`（`payment_contracts.py`，`error.code/stage/retryable` + `outcome.requires_reconciliation/side_effect_started`）；注册链路用 `classify_error` 产出 `failure_class` 字符串。**全部 payment 文件 `failure_class` 命中数为 0。**

---

## 3. 账号测活

### 3.1 四层，但有两条平行主路径

| 层 | 位置 | 语义 | 生产调用 |
|---|---|---|---|
| L0 | `accounts/account_liveness.py:141 probe_account_liveness` | 唯一 HTTP 探针，打 `wham/usage`（`:24`），无副作用 | **9 处** |
| L1 | `accounts/account_recovery.py:37 refresh_local_quota_statuses` | 批量编排器，UI「账号测活(N)」真身 | `commands/accounts.py:245`、`account_scan.py:728` |
| L2 | `accounts/account_health.py` | 纯契约，`HealthState` 6 值，无 HTTP | 仅 `account_health_queue` + `store/markers.py:123` |
| L3 | `accounts/account_health_queue.py` | 持久化队列，落 `runtime/account_health/queue.json`，后台 worker | 仅 `commands/registration.py:416` |

**[确证] L1 与 L3 是两条平行路径，结果字段不同** —— L3 的 plan 检查调的是 `check_account_promotion`（`:356-361`），不是 wham/usage。

**非测活（命名误导）**：`scripts/mailbox_pool_liveness.py:20` 探的是**邮箱池凭据**（`mailbox._fetch_mailbox_messages`），与账号测活无关。

### 3.2 并发

全线程池，无 asyncio / 多进程 **[确证]**。

- L1：`max_workers = min(16, len)`（`:61`）；`heavy_lane_slots = min(max_workers, max(2, max_workers//2))`（`:69`）。
- **[确证] `heavy_lane_slots` 是信号量而非预留线程**：`max_workers ≤ 2` 时 `heavy == max_workers`，信号量完全失效；且 `browser_slots` 与 `relogin_slots` 两个 lane 容量相同（`:73/:77`），浏览器回退 + 重登合计可占 `2×heavy_lane_slots` —— 与 `:69` 注释宣称的"分开 lane"不符 **[推断]**。
- L3：又一套 `_BROWSER_FALLBACK_SLOTS`，`process_account_health_jobs` 上限 8。
- **[确证] 三处超时预算口径不一**：`cli.py:288 --quota-account-timeout` 默认 120，而 `account_scan.py:738-741` 从 `account_health.*_seconds` 取 240/900/360。

### 3.3 🔧 配置项写了但从未生效 **[确证，已复核]**

`account_health_queue.py:121` 读 `health.get("workers") or 2`：

| 文件 | `account_health` 段 |
|---|---|
| `config.json` | `{"use_registration_affinity": true, "batch_timeout_seconds": 900, "account_timeout_seconds": 360, "relogin_cooldown_seconds": 300}` —— **无 `workers`** |
| `config.example.json` | 同上 **+ `workers: 2`** |
| `runtime.json` | 只有 2 个键 |

→ 队列并发**恒为默认 2，UI 无法调节**；且 **`config.json` 与 `config.example.json` 已漂移**。

### 3.4 状态机：至少 3 套 + C# 第 4 套

1. `HealthState`（`account_health.py:16-22`）：healthy / token_invalid / recovered / deactivated / failed / unknown
2. 探针字符串（`account_liveness.py:459-490`）：`status ∈ {active, token_invalid, unknown}` + **中文** `quota_status`（`401失效`/`额度不足`/`可用`/`HTTP n`）
3. `scan_status`（`account_scan.py:48 _SCAN_STATUS_BY_FAILURE_CLASS`）
4. C# 侧 `AccountStatusInterpreter.cs:56-85` —— 注释自承 `GetQuotaStatus` 已删但未清理周边

写回 `store/markers.py` 的 raw_json 内嵌 `quota`/`account_health`/`promotion` 三子对象。**跨层无映射表。**

### 3.5 耦合：存在环

**[确证]** `account_liveness.py:98-99` 与 `account_recovery.py:894-895` 函数内 `from ..registration_drivers import ...`、`account_recovery.py:591` `from ..registration import ...`，而 `registration.py:205` 正向包装 `account_liveness` —— **靠 lazy import 掩盖的双向依赖**。
另：`registration.py:205-207` 的转发壳与 `accounts/__init__.py:6-8`「deliberately **no** forwarding shells」自相矛盾。

---

## 4. 查优惠

入口 `accounts/account_promotion.py`，端点 `ACCOUNTS_CHECK_URL = /backend-api/accounts/check/v4-2023-04-27`（`:30-31`）。

| 维度 | 现状（2026-09-09 复核后） | 判定 |
|---|---|---|
| 限流 | **无全局限流** | ⚠️ **本轮不加**，见 §14.2 |
| 重试 | 代理轮换重试**本来就存在**（`:380-387` 按 `_promotion_proxy_candidates` 逐个试），但 `_retryable_promotion_transport` 只认传输错误，**429 不重试** | ✅ 已补 429（401 **故意**不重试），见 §14.3 |
| 失败表达 | **部分不成立**：内部聚合 `_promotion_status_code`（`:485`）读的是 `probe.status_code` 真整数，不是中文串；C# `BackendResultInterpreter.cs:88-98` 解析的是**任务名**（`账号优惠检测`）不是状态值。真问题只有一处：`account_recovery.py:1054` 拿 `promotion_status == "AT失效"` 当判别式 | ✅ 已修，见 §14.4 |
| 代理 | 按 email 哈希轮换 `_promotion_proxy_candidates`（`:460`），lane 与测活分开（`:262` vs `:179`） | 好 |
| 并发 | `refresh_promotion_statuses`（`:328`）默认 `workers=4`，上限 16 | 好 |

**与测活共用** `browser_fetch_for_account`、`bind_account_identity`、`chatgpt_headers` —— 复用是对的。

**主要风险**：无限流 + 无 429 重试，批量跑时容易触发风控；中文字面量当状态码，跨语言解析脆弱。
**2026-09-09 复核**：三条里两条要打折 —— 重试机制其实**存在**只是不认 429；"中文字面量当状态码"
只有 1 处真实例（另 2 处指控不成立）。限流**未在本轮加**。详见 §14。

---

## 5. 日志输出模块

### 5.1 🔴🔴 邮箱明文落盘 —— 已发生的数据泄漏 **[确证，已用实跑验证]**

`sanitizer.py` 有两个强度不同的函数：

- `sanitize_text`（`:64`）—— 只跑 `text_patterns`
- `sanitize_log_text`（`:71`）—— 额外跑 `_EMAIL_PATTERN` 做 `bo***@gmail.com` 掩码

**只有 `logging_setup.py` 的两个 handler 用了强版本**（`:57`、`:174`）。而 **stdout 路径用的是弱版本**：

- `diagnostics.py:21` `safe_print` → `sanitize_text`
- `diagnostics.py:66` `SanitizingTextIO.write` → `sanitize_text`

实测输出（本机 `.venv` 跑）：

```
WEAK  (sanitize_text)    : account bob.alice@gmail.com token [REDACTED] proxy http://[REDACTED]@1.2.3.4:8080
STRONG(sanitize_log_text): account bo***@gmail.com     token [REDACTED] proxy http://[REDACTED]@1.2.3.4:8080
```

**泄漏链闭合 [确证]**：

```
30 处 print(f"...{email}...")
  → sanitize_text（弱，不脱敏邮箱）
  → stdout
  → C# PythonBackendClient.cs:66 PumpAsync(process.StandardOutput)
  → SensitiveDataSanitizer.Redact —— 规则从 sensitive_policy.json 的 text_patterns 加载，
    与 Python 弱版同源，**同样没有 email 规则**
  → AppHost.cs:24 落 runtime/app_*.log
```

C# 侧三个调用点（`BackendResultInterpreter.cs:399`、`BackendTaskCoordinator.cs:71`、`MainWindow.Inbox.cs:201`）都调用了 `Redact`，但救不了邮箱。

**代理密码、Bearer/JWT、BA-token、API key 覆盖良好**；`token_telemetry.py:29` 对 token 只留 sha256[:16]，安全。**只有邮箱这一条漏网。**

### 5.2 五条并行支路，不是一条流水线 **[确证]**

| 支路 | 入口 | 落点 | 消费者 |
|---|---|---|---|
| A 结构化事件 | `emit_event`(`desktop_ipc.py:117`) → `_envelope`(:71) → print(:123) | stdout | C# `BackendProgressEventParser` |
| B 文本日志 | `print()` × 574 | stdout | C# `BackendLogPresenter.FormatLine`(:92) |
| C Python logging | `getLogger` × 26（219 个 py 中仅 **12%** 有 logger） | `runtime/logs/sms_tool.log` + `.jsonl` | **无人** |
| D 进度落盘 | `registration_progress.py:151 persist` | `registration_progress.jsonl` | 离线聚合 |
| E 关联元数据 | `telemetry.py:10 correlation_fields` | 注入 C/D | — |

**关键点**：`configure_logging(to_console=False)`（唯一调用点 `cli.py:426`）意味着 **C 与 stdout 已解耦**——logging 只进文件，WPF 面板看到的 100% 是 print。**print 与 logging 内容零重叠，是互斥的两套事实来源。**

A 与 B 共享同一 stdout 管道，仅靠 `@@SMSWORKBENCH_V2@@` 前缀区分。

`registration_pulse` 不是第三套进度上报（它是批量调度策略，`:108`），但它用裸 `print` 自行输出进度（`:161/179/202/207`）—— **事实上的第 6 个出口**。

### 5.3 轮转与噪声

- `RotatingFileHandler` 5 MiB × 5 备份 × 2 文件（`logging_setup.py:28-29`、`:244-251`），上限约 60 MiB。实测 `sms_tool.log` 2.36 MB、`.jsonl` 3.41 MB，**均未触发轮转**。无限增长风险低。
- 遗留污染：根目录 `logs/sms_tool.log` 仅 74 字节，停更于 09-02，是 `_default_log_path()`（`:206`）的历史残留，与现行 `runtime/logs/` 并存。
- **[确证] 热路径噪声**：缩进 ≥8（循环体内）的 `print` **378 处**、`logger.*` **74 处**。
  C# 侧用**硬编码黑名单**兜底（`BackendLogPresenter.cs:38-55`，11 条前缀），注释自承"每账号每阶段触发，26 个账号约 150 行无价值" —— **这是补丁，不是治理**。
- `print` Top5：`paypal_reverse.py` 38、`registration_handlers.py` 32、`phone_reuse.py` 31、`nodriver_paypal.py` 31、`commands/payment_links.py` 30。
  文档自承"上一状态是 534 个 print，零轮转"（`logging_setup.py:15`）—— **三年后不降反升至 574**。

### 5.4 IPC schema 门禁：一半形同虚设

**[确证]** `ipc_schema.json` 描述的是**常驻 desktop-read 的 op 协议**（version=1 / ops / error_codes），与 v2 事件信封（`smsworkbench.ipc.v2`, version=2）**不是同一物**——命名强误导。

`scripts/ipc_schema_check.py` 只校验 `PROTOCOL_VERSION` + `SUPPORTED_OPS` 两项（`:20-26`）：

- `error_codes` **全仓无任何校验引用**
- **v2 事件 payload 的 17 个字段完全无 schema 校验**

**[确证] CI 已挂载**（`.github/workflows/ci.yml` 末尾依次跑 config_schema_check / ipc_schema_check / docs_consistency_scan，实测三个均通过）。
**[确证] pre-commit 未挂载**：`.pre-commit-config.yaml` 不存在，`git config core.hooksPath` 为空，`.git/hooks/` 仅 sample → **`scripts/precommit_guard.py` 在本机从未运行**，只靠 CI `--all` 兜底。

---

## 6. 文档规范

### 6.1 文档在腐烂，不是缺文档 —— ✅ 已落地（2026-09-09 同日修复，见 §10）

**[确证，已复核]** 三处硬伤（审计时抽样 3 处，实际全量复核后共 **42 处**失效，见 §10.1）：

| 位置 | 文档写的 | 代码实际 |
|---|---|---|
| `docs/registration-and-proxy-architecture.md:149/237` | `registration_drivers/playwright.py:1518/:1453/:1622` | **该文件自 09-05 拆分后只剩 27 行** |
| 同文档 :211-214 | `RegistrationDriver` 在 `base.py:9`、`normalize_registration_driver` 在 `:29`、`BrowserRegistrationError` 在 `:57` | 实际 **`:23` / `:94` / `:130`** |
| `README.md:293` | `-> @@SMSWORKBENCH_IPC_V1@@` | 实为 **`@@SMSWORKBENCH_V2@@`**（`desktop_ipc.py:31`） |

同文档 `:60` 称 `registration.py`"不含实现" —— 但 `:229-264` 含两链路分发 + `bind(globals())` 装配。
`README.md:139`"默认仍为 protocol" 与 `:144`"camoufox 是当前默认"自相矛盾。

> **抽样偏差提醒**：审计时只报了 3 处，是因为按"看起来可疑"抽样。实际用解析器全量
> 比对后是 **74 处引用里 42 处失效**（57%），其中 `proxy_entry.py` / `sentinel_tokens.py` /
> `registration_preflight.py` 三个模块**全部正确**，而 `auth_headers.py` 与
> `browser_fingerprint_pool.py` 几乎全错。抽样审计不能用来下"文档整体健康"这类结论。

**7 个文档符号已不存在**（含 tests/ 全量复核）：`_resolve_proxy_geo`、`kickoff_otp_delivery`、`measured_exit_ip`、`ua_for_impersonate`、`webrtc_ip`、`fingerprint_for_impersonate`、`http_policy`。
**[推断]** 这些可能并非"功能被删"，而是审计文档记录了**当时提议但未落地**的方案 —— 建议按此假设先核对 git log 再决定删不删文档。

### 6.2 审计目录自身不规范

`docs/audits/` 22 份文档：

- **`README.md` 仅 24 行**，是命名约定 + 免责声明（"Individual files may describe superseded code and must not be treated as the current architecture contract"），**不是内容索引** —— 22 份无一被逐条列出。
- **命名违规 6/22**（`scan-` 连字符 vs 约定的 `scan_`）。
- 混入非 md 文件 `browser_profiles_manifest_20260909.txt`。

### 6.3 重复与过度承诺

- `README.md` 672 行（38 KB）含"项目架构"整章，与 `docs/architecture.md`(180行)、`docs/directory-map.md`(148行) 内容重叠；`docs_consistency_scan.py:23-26` 只强制三份 README 指向最新 release note，**不检测重复**。
- `README.md:195/654`"日志会脱敏 API Key 和 Service Token" —— 未提邮箱，而 §5.1 证明邮箱未脱敏。**属过度承诺。**

---

## 7. 整改清单（按性价比排序）

### P0 —— ✅ 已落地（2026-09-09 同日修复）

| # | 问题 | 位置 | 落地方式 | 状态 |
|---|---|---|---|---|
| 1 | **邮箱明文落盘** | `diagnostics.py` / `sanitizer.py` / `sensitive_policy.json` / `SensitiveDataSanitizer.cs` | 新增 policy 段 `log_text_patterns`（日志专用，与凭据类的 `text_patterns` 分开）；Python 与 C# **从同一份 policy 读取**，一份规则覆盖 IPC 两侧。`safe_print` / `SanitizingTextIO.write` / `safe_exception` 改用 `sanitize_log_text` | ✅ 实测 `bob.alice@gmail.com → bo***@gmail.com`，与 `mask_account` 样式一致，代理凭据未被误伤 |
| 2 | **续跑判定语义矛盾** | `payment_batch.py` `_checkpoint_row_resumable` / `_batch_counts` | 拆成两个独立子句（去歧义，行为不变）；新增 `counts.reconciliation_required` **按标记字段本身**统计（原来只按 status 口径，会漏掉非 unknown 来源） | ✅ 变异测试双杀 |
| 3 | **动态装配不可静态分析** | `registration.py` `bind(globals())` | 新增 `_email_registration_operations()`，**59 个依赖逐个显式命名**；映射在**函数内**构建以保留 `patch.object` 注入面 | ✅ 6 条门禁测试 |

> **P0-2 的认知修正**：原文"无对账入口"是夸大的。报告里本来就有 `counts.unknown`，行级也带
> `requires_reconciliation` 字段。真问题是**标记的 4 个来源与报告的 status 口径不一致** —— 由
> executor 直接置位、`status` 从未变成 `unknown` 的行在汇总里统计不到。已按标记字段本身补计数。
>
> **P0-3 的修正**：原报告写 `registration.py:264`，实际函数名为 **`run_email`**（非 `run_registration`），
> 字段数为 **59**（非 55）。

### P1 —— 性价比高，成本低

| # | 问题 | 位置 | 修复 |
|---|---|---|---|
| 4 | ~~约 100 行确证死代码~~ **原结论部分错误，见 §11** | 见 §11.1 | ✅ **部分落地**：8 个候选中 5 个确证已删（约 50 行）；2 个**不是死代码**（内部互引）；2 个模块是**"有测试但零生产调用"**，属产品决策，未删 |
| 5 | `generate_payment_link` 同名双实现 | `pay_link/core.py:35` vs `paypal_link/gen_link.py:1215` | ✅ **已落地**：删掉 `paypal_link` 侧转发空壳 + 导出，并加门禁防复发（见 §13） |
| 6 | 配置无 key 级校验 + `config.json`/`example` 漂移 | `config_schema.json`(794B) ；`account_health.workers` | 补 value schema 或至少加"未知键告警"；同步 `workers` |
| 7 | 文档行号/符号失效（实为 42 处）+ README V1/V2 | 见 §6.1 | ✅ **已落地**：42 处行号修正 + `base.py`/`client.py` 歧义简称改全路径 + README V1→V2；并加了**两层门禁**让漂移可被测试抓到（见 §10） |
| 8 | **本机 pre-commit 从未安装** | `core.hooksPath` 空、`.pre-commit-config.yaml` 不存在 | ✅ **已落地**：`core.hooksPath -> .githooks`，端到端实测能拦能放（见 §12） |
| 9 | 查优惠无限流 / 无 401·429 重试 / 中文字面量当状态码 | `account_promotion.py:317-320` | 加重试 + 改用错误码枚举 |
| 10 | v2 事件 payload 无 schema 校验；`error_codes` 是死字段 | `ipc_schema_check.py:20-26` | 补 envelope schema，或删 `error_codes` 消除假安全感 |

### P2 —— 长期 / 见仁见智

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 11 | `registration_handlers.py` 1079 行 6 职责、docstring 8% | — | 拆需谨慎（是测试的 patch 注入面） |
| 12 | 热路径 378 处 print；logger 覆盖率 12% | — | ⚠️ **只落地门禁，未改任何 print**：实测 **495 处**（非 378）；`cli.py:418-424` 明写 stdout 是 WPF 的 **IPC 通道**、`to_console=False` 是刻意的 → **改 logger = CLI 用户失去进度输出，是行为变更不是重构**；且 C# 按行前缀匹配，加了 formatter 装饰后黑名单会失效、噪声反而回来。另发现 `auth_flow._print_protocol_diagnostic()` **已实现「健康走 logger、异常走 print」的正确范式**（覆盖率 12% 低估了实际质量）。已加跨语言契约门禁 `tests/test_backend_log_noise_contract.py`（13 个前缀实测**全部有效，未腐烂**），详见 §24 |
| 13 | 三套账号状态枚举 + C# 第四套，无映射表 | `store/normalize.py:268` / `AccountStatusInterpreter.cs:162` | ✅ **已落地**：**"三套枚举"不成立**（Python 侧本就有单一真源 + 已有映射表），真问题是 C# 不认得 4 个失败状态词（见 §19） |
| 14 | 支付/注册双向耦合；`failure_class` ↔ `error_code` 不互通 | `error_classification.py:119` / `account_scan.py:48` | ✅ **已落地**："建全量映射表"不可行（`error_code` 是开放集合），改锁 **failure_class 覆盖**；并修掉 `cancelled` 被算成 `scan_failed`（见 §20） |
| 15 | `heavy_lane_slots` 小并发下失效 | `account_recovery.py:69/73/77` | ✅ **已落地**：抽成 `_heavy_lane_slots()`，保证池 >= 2 时严格窄于池（见 §18） |
| 16 | `runtime/` 无 retention（3879 批次文件 + 4MB jsonl） | — | ✅ **工具已落地**（未执行清理）：实测 9618 文件 / 99.5 MiB，**主体是 `payment_batch_locks/gates/` 3949 个空 `slot-0.lock`（41%）**，审计漏了它；且 `accounts.sqlite3` 67 MiB 是账号主库 → **不能按年龄删**。已交付 `scripts/runtime_retention.py`：规则驱动 + 硬否决名单 + **relocate 不 delete** + 默认 dry-run，顺带覆盖 P0-1 `browser_profiles`（见 §23） |
| 17 | 审计目录命名 6/22 违规 + 无内容索引 | `docs/audits/` | ✅ **已落地**：违规成立但**改名不如改约定**（4 个 `scan-` 有 11 处交叉引用、22/24 文件用连字符）→ README 约定改为 `scan-YYYY-MM-DD-<topic>.md`；新建 24 份文件的分组索引 + 防腐烂门禁（见 §22） |
| 18 | 遗留 `logs/sms_tool.log`（74 字节，09-02 停更） | — | ✅ **已落地**（已备份删除）：**"遗留"的定性偏保守** —— 它是 `logging_setup.py` 路径回落 bug 修复当天的一次性探针残留（内容仅 `logging-setup-guard-marker` 一行）；同目录 `build_wpf.log` / `roxynet/` 也零引用但**未删**（见 §21） |

### ⛔ 明确**不要**动

- **错误处理 5 模块分层**（`error_classification` → `registration_policy` → `retry_guard` → `circuit_breaker` + 独立 `backoff`）—— 单向依赖、每份都写明切分理由，**是对的设计，合并会引入 transport→policy 反向边**。
- **`camoufox.py` / `cloak.py` / `roxy.py` / `playwright.py` 的 12~27 行转发壳** —— 有意的 patch 入口，docstring 明写"只导出公共 API 让旧 patch 响亮失败"。
- **`codex_oauth.py` 1161 行** —— 大但内聚，且 `registration_handlers.py:185` 已把它从注册链路归一化掉。
- **`registration_drivers/base.py` 不做成抽象基类** —— 接缝在结果层（两条链路共用 `build_registration_result` 与 `RegistrationStateMachine`）是合理的，不必为对齐硬造基类。

---

## 8. 本轮方法学备注

- 四个方向并行只读扫描（注册链路 / 支付链路 / 测活+优惠 / 日志+文档），覆盖 `sms_tool/` 66,779 行 + `SmsWorkbench/` C# 侧。
- **零引用判定一律 grep 全仓符号名**（含 `sms_tool/`、`tests/`、`scripts/`、`SmsWorkbench/`、C# IPC 命令名），不只看 import 行。
- §7 中 P0/P1 的关键结论（邮箱脱敏、幂等优先级、死代码、文档漂移、pre-commit、配置项缺失）**已由主代理用独立命令二次复核**，非子代理自述。

---

## 9. P0 落地验证记录（2026-09-09）

### 9.1 改动清单

| 文件 | 改动 |
|---|---|
| `sensitive_policy.json` | 新增 `log_text_patterns` 段（1 条 `account_email`） |
| `sms_tool/sanitizer.py` | 从 policy 加载 `log_text_patterns`；无该段时回落原硬编码 `_EMAIL_PATTERN` |
| `sms_tool/diagnostics.py` | `safe_print` / `SanitizingTextIO.write` / `safe_exception` 改用 `sanitize_log_text` |
| `SmsWorkbench/SensitiveDataSanitizer.cs` | 加载 `log_text_patterns` 并追加到 `Patterns`（含 null 保护）；`Redact` 语义明确为日志强度 |
| `sms_tool/payment_batch.py` | `_checkpoint_row_resumable` 拆双子句 + 修正 docstring；`_batch_counts` 新增 `reconciliation_required` |
| `sms_tool/registration.py` | 新增 `_email_registration_operations()`，59 个依赖显式命名 |
| `tests/test_payment_batch.py` | +4 条 |
| `tests/test_registration_operations.py` | +2 条 |

### 9.2 验证结果

- **P0-1 端到端**：`safe_print` 与 `SanitizingTextIO` 两条 stdout 路径实测均脱敏；
  `bob.alice@gmail.com → bo***@gmail.com`，与 `mask_account` 一致；
  `http://user:pw@1.2.3.4:8080`、`socks5://alice:secret@10.0.0.1:1080` 未被误伤。
- **P0-2 变异测试**：两个变异体（移除结算标志判定 / 删除新计数）**全部被杀死**。
- **P0-3 patch 注入面**：`patch.object(registration, "think_stage", ...)` 实测生效且退出后恢复 ——
  这是最危险的点，若把映射提到模块级常量会静默冻结 patched 值，已写成测试锁住。
- **P0-3 静态门禁**：把调用点改回 `bind(globals())` 后门禁测试**如期失败**（用 AST 检测而非字符串匹配，
  因为 helper 的 docstring 里会合法地提到 `bind(globals())`）。
- **全量套件**：`3167 passed / 6 skipped / 5 warnings / 579 subtests`，**0 失败**（123.89s）。

### 9.3 ⚠️ 两个遗留事项

1. **C# 改动未编译验证**。`dotnet restore` 对三个 csproj 全报
   `NuGet.targets(782,5): Value cannot be null. (Parameter 'path1')`（含未改动的 `SmsWorkbench.Contracts`），
   `C:\Users\29514\.nuget\packages` 为空。C# 侧改动的正确性仅靠人工审阅 + 与 Python 侧规则同源保证。
   **重新打包前必须先修好 dotnet 链并编译一次。**
2. **既成泄漏未清理**：`runtime/app_*.log` 的历史内容里已含明文邮箱，本次改动只阻断新增，不回溯清理。
3. **预存在 flaky（与本轮无关）**：`tests/test_registration_otp_strategy.py::
   test_otp_request_uses_shared_browser_impersonation` 在 `-k registration` 子集里失败
   （`chrome146 != firefox144`），**在干净 HEAD 上同样失败**，全量跑则通过 —— 顺序/状态相关，
   疑似 round2 P0-3（otp_strategy 硬编码 `AUTH_IMPERSONATE`）的残留 + thread-local profile 状态泄漏。
   建议单独开一轮处理。

---

## 10. P1-4 文档漂移修复记录（2026-09-09）

### 10.1 真实规模：42 处，不是 3 处

审计 §6.1 报了 3 处，是按"看起来可疑"抽样的结果。用解析器把文档里每个
`file.py:NNN` 与其邻近符号名配对、再用 `ast` 定位真实定义行后，**74 处引用中 42 处失效（57%）**：

| 模块 | 引用数 | 失效 | 说明 |
|---|---|---|---|
| `auth_headers.py` | 5 | 5 | `AUTH_FINGERPRINT_PROFILES` 17→34、`sentinel_fingerprint` 279→625、`openai_auth_headers` 348→710、`_GEO_PROFILES` 70→313 |
| `browser_fingerprint_pool.py` | 7 | 7 | 全错，最大偏移 `select_browser_profile` 363→**531** |
| `registration_drivers/base.py` | 4 | 4 | 9→23 / 19→85 / 29→94 / 57→130 |
| `fingerprint_pool.py` | 4 | 3 | 仅 `FingerprintPool:118` 仍正确 |
| `registration_drivers/playwright.py` | 3 | 3 | 模块已拆分，改指 `browser_flow/orchestrator.py:69` / `browser_flow/flow_steps.py:139` |
| `browser_pool.py` | 4 | 3 | `PoolConfig` 60→61（`@dataclass` 装饰器行 vs class 行） |
| `proxy_routing.py` | 2 | 2 | `select_operation_proxy` 107→115 |
| `sentinel/client.py` | 1 | 1 | `:312` 现为 `if supplied is not None:`，实际分支在 `:315` |
| `proxy_entry.py` | 6 | **0** | 全部正确 |
| `sentinel_tokens.py` | 4 | **0** | 全部正确 |
| `registration_preflight.py` | 2 | **0** | 全部正确 |

**顺带修掉一个真歧义**：文档用简称 `base.py`，但 `sms_tool/` 下有**两个**
（`pay_link/base.py` 与 `registration_drivers/base.py`），读者可能落错文件；
`client.py` 同理。已全部改为完整路径。

### 10.2 根本原因：文档漂移此前无法被检测

原 `scripts/docs_consistency_scan.py` 只做**文件级**校验（`sms_tool/providers/*.py` 是否存在），
不做行号，也不覆盖 `registration_drivers/`。所以 `playwright.py:1518` 指向一个 27 行的文件，
**没有任何门禁会报警**。改数字只是把腐烂重置一次，下次重构照旧。

已加**两层校验**（`scripts/docs_consistency_scan.py`）：

| 层 | 覆盖范围 | 抓什么 | 抓不到什么 |
|---|---|---|---|
| **弱** | 全部活跃文档的 `file.py:NNN` | 文件不存在 / 行号越界（模块被拆分或删除） | `base.py:9` 变成第 23 行 —— 文件只是变长了 |
| **强** | 形如 `\| `Symbol` \| `path.py:NNN` \|` 的表格行 | 用 `ast` 解析符号真实定义行，不符即报 | 散文里指向函数体内部的指针（**故意放过**，见下） |

**为什么散文引用只做弱校验**：`browser_pool.py:70` 是一条注释、
`proxy_routing.py:68` 是 `if lane == "browser_registration":`、
`client.py:315` 是 `if backend == "node_runner":` —— 这些是**合法的**
"指向函数体/注释"引用，强校验会 100% 误报。门禁**绝不能对自己不理解的东西报警**，
所以散文层只保留零误报的弱校验，符号表才上强校验。

`config.json` 的引用（如 `:227`）只跟踪不校验 —— 它被 gitignore 且由用户编辑，
行号不是我们能管的。

### 10.3 验证

- **门禁自身做了变异测试**（改回错误状态看它是否报警），5/5 符合预期：

  | 变异体 | 位置 | 结果 |
  |---|---|---|
  | M1 符号表 off-by-one（`base.py:23`→`:24`） | 第 211 行 | ✅ 杀死（强层） |
  | M2 指向被拆分的模块（`orchestrator.py:69`→`playwright.py:1518`） | 第 237 行 | ✅ 杀死（弱层） |
  | M3 行号越界（`:531`→`:9999`） | 第 234 行 | ✅ 杀死（弱层） |
  | M4 散文之外的陈旧行号（`fingerprint_pool.py:285`→`:229`） | 第 231 行 | ✅ 杀死（强层） |
  | M5 散文引用漂移（`base.py:23`→`:24`，第 32 行） | — | ✅ **如期放过**（弱层不覆盖，设计如此） |

- **第一次变异测试有 2 个"存活"，是我的变异打错了行**（`str.replace` 命中第 32 行散文
  而非第 211 行表格），**不是门禁失效**。改成按行号精确变异后全部杀死。
  —— 变异体存活时先怀疑变异本身，这是本轮第二次印证。
- **新增 5 条测试**（`tests/test_docs_consistency.py`）：符号表匹配 / 漂移 / 越界 /
  模块不存在 / json 引用不受管。
- **全量套件**：`3172 passed / 6 skipped / 5 warnings / 579 subtests`，**0 失败**（141.69s），
  比 P0 落地后的 3167 正好多 5 条。

---

## 11. P1-1 死代码：原审计结论复核后被推翻一半（2026-09-09）

### 11.1 复核结果（全类型、全目录，含 tests/ scripts/ C# XAML JS JSON）

原结论写"均只定义 + `__all__` 导出，零生产引用；已 grep 全仓复核"。**复核后 8 个候选里 3 类问题**：

| 候选 | 原判 | 复核 | 处置 |
|---|---|---|---|
| `_TERMINAL_STATES` (`base.py:184`) | 死 | ❌ **被 `_NON_SUCCESS_TERMINAL_STATES`(188)、`_TRANSITIONS`(197/198) 引用** | 随集群删（集群整体无人用） |
| `_NON_SUCCESS_TERMINAL_STATES` (`:188`) | 死 | ❌ 被 `_TRANSITIONS`(193-196) 引用 | 随集群删 |
| `_TRANSITIONS` (`:192-198`) | 死 | ✅ 确证（仅 `__init__.py` 导出） | **已删** |
| `_manager_error_stage` (`:63`) | 死 | ✅ 确证 | **已删** |
| `list_account_health_jobs` (`accounts/account_health_queue.py:152`) | 死 | ✅ 确证（路径原写 `account_health_queue.py`，实际在 `accounts/` 下） | **已删** |
| `register_payment_adapter` (`registry.py:155`) | 死 | ✅ 引用面确证为零，但 docstring 明写"useful for new methods/tests" | **保留**——是**有意的扩展点**，删掉等于移除接入新支付方式的能力 |
| `payment_reconciliation.py` (34行) | 死 | ❌ **`tests/test_payment_reconciliation.py:1` 导入并在用** | **未删** |
| `payment_country_catalog.py` (43行) | 死 | ❌ **`tests/test_payment_proxy_health.py:12` 导入并在用** | **未删** |

### 11.2 后两个模块的真实状态：不是死代码，是"接线未完成"

单独查了模块内函数在生产代码里的调用点：

| 函数 | 生产调用 | 测试调用 |
|---|---|---|
| `reconcile_payment_result` | **0**（仅自身 def + `__all__`） | 3 |
| `is_paypal_supported` | **0**（仅自身 def） | 6 |
| `validate_paypal_country` | **0**（仅自身 def） | 5 |
| `PAYPAL_SUPPORTED_COUNTRIES` | 1（被同模块 `is_paypal_supported` 用） | 0 |

即：这两个模块**写完了、测试了，但没有任何生产代码调用**。这不是"死代码"，
是**"功能已就绪但未接线"** —— 可能是有意预留，也可能是接线的活漏了。
**删它们要连带删两个测试文件，属产品决策，不是纯技术清理。** 建议老板确认意图后再动：
要么接线（更可能是对的），要么连测试一起删。

### 11.3 已删除部分与验证

**删了 5 个符号、约 50 行**（不是原估的 100 行）：

| 文件 | 删除内容 |
|---|---|
| `sms_tool/pay_link/base.py` | `_manager_error_stage`(10行) + 状态机三件套 `_TERMINAL_STATES`/`_NON_SUCCESS_TERMINAL_STATES`/`_TRANSITIONS`(17行) |
| `sms_tool/pay_link/__init__.py` | 对应 2 处 import + 4 处 `__all__` 条目 |
| `sms_tool/accounts/account_health_queue.py` | `list_account_health_jobs`(11行) + `__all__` 条目 |

**验证**：
- 删除后 `import sms_tool.pay_link` 正常，5 个符号 `hasattr` 均为 False，`register_payment_adapter` 仍在，`__all__` 从 54 降到 50。
- `_public_item` 曾是被删函数的唯一依赖，但另有 3 处使用，已确认保留。
- **全量套件 3172 passed / 6 skipped / 579 subtests，0 失败**（134.10s）。
- **踩坑**：`pay_link/__init__.py` 有**两个** `from .base import` 块，第一次只改了 `__all__` 和第二块，
  漏了顶部第 7 行 → `ImportError`。**星号导入壳改导出名时必须全文件查，不能只改 `__all__`**。

---

## 12. P1-5 安装 pre-commit 凭据守卫（2026-09-09）

### 12.1 状态：钩子写好了但从未生效

`.githooks/pre-commit` **自 08-31 就在仓库里**，但 `core.hooksPath` 为空 ——
即 `git commit` 走的一直是 `.git/hooks/`（空的），**这个守卫一天都没运行过**。
该仓 2026-08-30 曾把 8 个明文凭据提交进历史，之后补了内容级守卫却没接上。

已执行 `scripts/install_git_hooks.py` → `core.hooksPath = .githooks`，`--status` 报 `ACTIVE`。

### 12.2 验证：不发起 commit 也能测（避免测失败反而造出提交）

直接 `git commit` 测试有个风险：**万一钩子没拦住，就真的造出一个含假凭据的提交**。
所以改为直接执行 `sh .githooks/pre-commit` —— 这正是 `git commit` 会走的路径，但不会创建提交。

| 用例 | 内容 | 期望 | 实测 |
|---|---|---|---|
| 应拦 | `http://alice:Sup3rS3cret@10.0.0.1:8080` | 阻断 | ✅ 退出码 1，报 `guard_probe_tmp.txt:4: hardcoded secret in 'proxy-url-with-credentials'` |
| 应放 | `http://user:password@10.0.0.1:8080` | 放行 | ✅ 退出码 0，`clean (1 staged files)` |

**探针必须放对位置**：守卫的 `DOC_OR_TEST` 规则豁免 `docs/`、`tests/` 和 `.md` 文件，
探针若放 `tests/` 下会被跳过 —— 那样测出来的是**假绿**。已放在仓库根目录。

装之前先跑 `precommit_guard.py --all --dry`：**690 个受版本控制文件全部干净**。
另确认 6 个敏感文件（`config.json`/`proxy.json`/`runtime.json`/`payment.json`/`session.json`/
`mailbox_tokens.txt`）**均已 gitignore 且不在版本控制中**，且 `git log --all` 对它们的提交数为 **0**
—— 08-30 的泄漏在工作区和历史两个层面都已处置完毕。

守卫逻辑本身已有 **26 条测试**（`tests/test_precommit_guard.py`），全部通过。

### 12.3 影响与回滚

- **影响**：此后每次 `git commit` 会扫描暂存文件，命中凭据则拒绝提交。
- **误报兜底**：`git commit --no-verify` 可绕过（守卫自己的提示语里也写了）。
- **回滚**：`python scripts/install_git_hooks.py --uninstall`（取消 `core.hooksPath`，恢复 `.git/hooks`）。
- 钩子用 `python3`/`python`/`py` 中第一个可用的；本机解析到托管版 3.13.12，守卫只用标准库，无依赖风险。

---

## 13. P1-2 消除 `generate_payment_link` 同名双实现（2026-09-09）

### 13.1 先判定谁是影子 —— 不能凭名字删

两个同名函数，删除前必须确认真身。判定结果与证据：

| 候选 | 位置 | 函数体 | 谁在调用 | 判定 |
|---|---|---|---|---|
| A | `sms_tool/pay_link/core.py:35` | 完整实现（编排 + 落盘 + 异常处理） | `commands/payment.py:592`、`payment_batch.py:24` 均从 `..payment_link_manager` 导入；`payment_link_manager.py` 是 `from .pay_link import *` 壳 | **真身** |
| B | `sms_tool/paypal_link/gen_link.py:1215` | 仅 `from ..payment_link_manager import generate_payment_link as managed_generate` + 原样转发 | **全仓 0 处**（见下） | **影子，删** |

支撑"B 零调用"的全类型搜索（`.py/.cs/.xaml/.js/.json/.ps1/.md/.txt/.yml`，含 `tests/`、`scripts/`、C#）：

- `sms_tool/` 内 `generate_payment_link` 的 12 处命中里，B 只出现在**它自己**（定义 `:1215` + 自引用 `:1224`）
  和 `paypal_link/__init__.py` 的导出（`:153` 导入、`:201` `__all__`）——**无任何生产调用**
- `scripts/`、`SmsWorkbench/**`、`services/**` 全部 **0 命中**
- 唯一相关测试 `tests/test_paypal_gen_link_pure.py:29` 只 `from sms_tool.paypal_link import gen_link`，
  20 条用例全是 alias/url 相关，**不含任何 `generate_payment_link` 用例**
- 非 Python 文件里只有审计报告与记忆在讨论它，无代码引用

**星号导入壳的连锁面**（`gen_pp_link.py:6-7`、`paypal_reconciliation.py:6-7` 都是
`from .paypal_link import *`）：两个壳文件内 `generate_payment_link` 命中数均为 **0**，删导出不影响它们。

### 13.2 已落地

| 文件 | 改动 |
|---|---|
| `sms_tool/paypal_link/gen_link.py` | 删 `:1215-1233` 转发空壳（19 行） |
| `sms_tool/paypal_link/__init__.py` | 删 `:153` 导入项、`:201` `__all__` 条目 |
| `scripts/delayed_import_baseline.json` | `sms_tool/paypal_link/gen_link.py: 4 → 3`，`total: 401 → 400` |
| `tests/test_payment_link_entrypoint_unique.py` | **新增**门禁（3 条） |

**基线改法**：没有直接跑 `--update-baseline`（那会把整棵树重算，可能顺手把别处的意外增长合法化）。
改为先跑一次性对账脚本比较 `per_file` 差异，确认**只有 `gen_link.py` 一项 `4 → 3`**、无新增文件、
无其他变化，再手工改这两处。改完 400 == 实际 400。

### 13.3 门禁：改完只是重置腐烂，门禁才是根治

删掉影子只修了今天这棵树。同名双实现之所以能活下来，是因为**没有任何机制能发现它**——
测试 `patch("sms_tool.payment_link_manager.generate_payment_link")` 对走 `paypal_link` 那条路
的调用方完全不可见，反之亦然。所以补 `tests/test_payment_link_entrypoint_unique.py`：

1. **AST 扫 `sms_tool/**` 里所有 `def generate_payment_link`，断言恰好 1 处**，且位于 `sms_tool/pay_link/`。
   （用 AST 而非 `hasattr`，所以 import 形式的再导出不会被误算成"定义"）
2. `sms_tool.payment_link_manager.generate_payment_link.__module__ == "sms_tool.pay_link.core"`
   —— 公共壳必须解析到唯一实现
3. `sms_tool.paypal_link` 不得再持有该名字（`hasattr` + `__all__` 双重断言）

**变异验证（2 个变异体，均被抓，非假绿）**：

| 变异体 | 做法 | 结果 |
|---|---|---|
| M1 重新定义 | 把转发函数原样插回 `gen_link.py` | ✅ 红：`is defined 2 times (sms_tool/pay_link/core.py, sms_tool/paypal_link/gen_link.py)` |
| M2 换个姿势复发 | 不重新定义，改在 `paypal_link/__init__.py` 加 `from ..payment_link_manager import generate_payment_link`（"为了兼容再 re-export 一次"——**这是更可能真实发生的回归路径**） | ✅ 红：`paypal_link re-exports generate_payment_link again` |

两个变异体都只让**对应那一条**测试红、另外两条保持绿，说明断言没有互相掩盖。

### 13.4 验证与回滚

- 支付链路 12 个测试文件子集：**304 passed / 2 skipped / 123 subtests**（9.21s），
  含 `test_payment_batch.py`、`test_payment_cli_contract.py`、`test_payment_link_manager.py`、
  `test_paypal_gen_link_pure.py`、`test_unbound_name_guard.py`（后者校验 `__all__` 里的名字都真的存在）
- 全量套件结果见文末记录的 `3172 → ...`
- **回滚**：`git checkout -- sms_tool/paypal_link/ scripts/delayed_import_baseline.json`
  并删除 `tests/test_payment_link_entrypoint_unique.py`。三个改动都在同一逻辑单元内，无跨模块依赖。
- **残留风险**：`sms_tool/paypal_link` 名义上仍是"对外可用的 paypal 包"，若有仓库外的调用方
  （未纳入本仓）曾 `from sms_tool.paypal_link import generate_payment_link`，删除后会 `ImportError`。
  本仓范围内确认 0 调用；**仓库外调用方无法被本仓搜索证伪**，属于已知不可验证项。

---

## 14. P1-6 查优惠：429 重试 + 修中文判别式（2026-09-09）

### 14.1 三条指控的复核结果 —— 两条要打折

| 审计原话 | 复核 | 结论 |
|---|---|---|
| 「无 401·429 重试」 | 重试**机制本来就存在**：`refresh_promotion_statuses` 的 `run()` 里
（`account_promotion.py:380-387`）按 `_promotion_proxy_candidates` 逐个代理试，只是
`_retryable_promotion_transport` 只匹配 `curl:(5)/(7)/(28)`/`timeout`。 | **部分成立**：429 该补；
**401 不该补**（见 14.3） |
| 「中文字面量当状态码」 | `_promotion_status_code`（`:485`）读的是 `(item["probe"])["status_code"]`，
**是真整数不是中文串**；C# `BackendResultInterpreter.cs:88-98` 解析的是**任务名**
`账号优惠检测`，也不是状态值。真实例只有 `account_recovery.py:1054`。 | **1/3 成立** |
| 「无限流」 | 全仓 `sms_tool/accounts/` 里**测活也没有限流**（只有 `account_2fa.py` /
`account_creation.py` 的零散 `time.sleep`）——**不是查优惠独有的缺口**。 | **本轮不加**，见 14.2 |

### 14.2 限流：本轮**不**加，理由

1. **不是 promotion 独有**：测活（`account_liveness`）同样无限流，只给查优惠加会制造不一致。
2. **参数是产品/风控决策**：每分钟多少请求、按账号还是按出口 IP 限、超限时排队还是丢弃——
   这些我无法从代码推断，猜一个数字写死等于把猜测固化成行为。
3. **成本收益**：查优惠已经按 email 哈希做代理轮换（`_promotion_proxy_candidates:460`），
   负载天然分散；而 **429 重试是对真实背压的响应**——比起预先猜一个速率，等服务端说"慢点"再退
   更准确，也不会在正常路径上白白拖慢批量。
4. **验证受限**：任何节流参数都会改变批量耗时，而 UI 侧（C#）无法编译验证（dotnet 链坏）。

**建议**：如果确实要限流，做成 `runtime` 配置里可调的 `promotion.min_interval_seconds`，
默认 0（关闭），并与测活共用同一个节流器 —— 但这是独立的一次改动，不混在本轮里。

### 14.3 429 重试：已落地（`sms_tool/accounts/account_promotion.py`）

**为什么 401 不重试**：401 = access token 已死，重试只是白烧一个代理槽位和一次超时。
`test_promotion_401_stays_in_promotion_namespace` 已经把"401 不得降级账号状态"锁住了，
重试会与这个语义打架（多一次失败、多一段延迟，结果一样）。

**实现**（在原有代理轮换循环里扩，不新建循环）：

```python
if probe.get("ok"):
    break
backoff = _promotion_throttle_backoff(probe)
if backoff is not None:
    time.sleep(backoff)
    if index >= len(candidates) - 1:
        probe = check_account_promotion(...)   # 池用尽：同一出口延迟重试一次
        break
    continue                                    # 否则换一个 IP（更强的修复）
if not _retryable_promotion_transport(probe) or index >= len(candidates) - 1:
    break
```

- 只认 `PROMOTION_THROTTLE_STATUS = 429`，**不认 5xx**（5xx 是否该烧一次代理轮换属产品决策，未擅自定）
- 总尝试次数上界 `len(candidates) + 1`，持续 429 不会拖垮批量
- `Retry-After` 优先，钳到 `PROMOTION_THROTTLE_MAX_BACKOFF = 5.0`；缺失或非法值回落
  `PROMOTION_THROTTLE_DEFAULT_BACKOFF = 1.5`。**钳位是必需的**——否则一个 `Retry-After: 3600`
  能把批量挂死一小时
- 传输错误路径**行为不变**（轮换、不 sleep），由 `test_transport_errors_still_rotate_without_sleeping` 锁住

### 14.4 中文判别式：已修（`sms_tool/accounts/account_recovery.py`）

`mark_promotion_status` **本来就在落机器可读的 `promotion.status_code`**（`store/markers.py:221`，
401 还会从 `error == token_invalid` 反推）——所以判别式根本不需要读中文标签。

新增 `_promotion_auth_failure(data)`：**优先读 `promotion.status_code`**，只有老记录
（该字段还不存在时）才回落到中文标签。**标签回落是刻意保留的**——已落库的历史数据没有
迁移，删掉回落会让老账号的过期标记清不掉。

`promotion_status` / `promotion.status` 的**中文显示串本身不改**：它是 C# 优惠状态列的显示值，
动它要连带改 C#，而 dotnet 链坏、无法编译验证。

### 14.5 验证

新增 `tests/test_promotion_throttle_retry.py`（13 条）：

- 429 换 IP 重试 / 池尽时同出口重试一次 / 最多重试一次 / **401 不重试** / 其它 4xx 不重试 /
  传输错误仍只轮换不 sleep
- `Retry-After`：正常值 3s、超限值 3600s 被钳到 5.0、非法值 `"soon"` 回落 1.5
- `_promotion_auth_failure`：有 code 时以 code 为准（标签写成什么都无所谓）、
  有 code 时旧标签不得覆盖、无 code 时回落标签、非 dict 不炸

**所有测试都桩掉 `time.sleep`**（`no_sleep` fixture 记录请求值）——套件 0.60 秒跑完，
否则每条都要真等 1.5 秒且结果顺序相关。

**变异验证 3 个，均被抓**：

| 变异体 | 结果 |
|---|---|
| M1 `PROMOTION_THROTTLE_STATUS = 429 → 401` | ✅ 7 条红，含 `test_401_is_never_retried` |
| M2 去掉 `time.sleep(backoff)`（重试但不减速） | ✅ 6 条红 |
| M3 `_promotion_auth_failure` 退回只用中文标签 | ✅ 2 条红 |

### 14.6 未做的事（明确列出）

- **限流**（见 14.2）
- **5xx 重试**：未定，属产品决策
- **`browser_fetch` 分支的 `Retry-After`**：浏览器通道拿不到响应头，走默认退避。
  429 仍会被重试，只是等待时长不是服务端建议值
- **C# 侧任何改动**：dotnet 链坏，无法编译验证

---

## 15. P1-3 配置校验：审计两条指控，一条不成立、一条指错对象（2026-09-09）

### 15.1 「没有任何 key 级校验」—— 不成立

`config.validate_config`（`sms_tool/config.py:371+`）**已经有**相当完整的校验：

| 类别 | 已有 |
|---|---|
| 类型 | `chatgpt`/`proxy`/`account_health`/`registration`/`drivers` 各段的对象/数组/布尔校验 |
| 数值 | `_validate_positive_numbers`（`registration` 的 7 个键） |
| URL | `chatgpt.*_url`、`registration.drivers.*.{api_base,cdp_base,start_url}` 的 scheme 白名单 |
| **未知键** | `account_health.proxies` 的 lane（`:423`）、`registration.drivers`（`:457`） |

真缺口比"没有"窄得多：**`account_health` 的标量键**（`workers`/`max_pending`/超时）从未做类型校验，
`"workers": "four"` 会一路走到 `account_health_queue.py:121` 的 `int()` 才炸。

### 15.2 「`config.json`/`example` 漂移（`account_health.workers`）」—— 指错对象

| 文件 | `account_health` 键数 | 是否生效 |
|---|---|---|
| `config.example.json` | 10（含 `workers: 2`、`max_pending: 1000`、`proxies`、`proxy_pool`） | 文档/模板 |
| `config.json`（43 KB，09-09 还在被改） | 4 | ❌ **分片存在时 CFG 根本不读它** |
| `runtime.json` | 2 | ✅ **真正生效** |

`load_merged_config`（`config.py:131-148`）：只要 `runtime/proxy/payment.json` 任一存在就只读分片，
**`config.json` 只在分片全部缺失时才被迁移**。三个分片都在仓库里 → 根 `config.json` 是死文件。

所以"example 与 config.json 漂移"这个对比没有意义——**比的对象就不参与运行**。
而 `runtime.json` 只有 2 个键也不是漂移：配置本来就是"只写覆盖项"，缺省值在代码里。

🔴 **但顺带挖出一条线索**：有 **5 个模块定义了 `DEFAULT_CONFIG_PATH = PROJECT_ROOT/config.json`**
——`omakse_client.py:35`、`paypal_link/gen_link.py:1223`、`paypal_protocol.py:31`、
`upi_link.py:60`、`scripts/probe_account_liveness.py:40`。
初判是「绕过 CFG/分片直读」，**复核后 4/5 是假警报，另有 2 处真绕过被新扫出**。
完整核查与处置见 **§17**。

> 修正说明：这里最初的「5 个模块绕过」判断**是错的**——这些 `DEFAULT_CONFIG_PATH`
> 是**哨兵值**（`_load_json` 内部判断路径等于它时转 `load_merged_config()`），
> 不是真读文件。以 §17 的复核结果为准。

### 15.3 真正的用户体验 bug：静默钳位

`account_health_queue.py:121`：

```python
workers = max(1, min(int(health.get("workers") or 2), 8))
```

用户写 `"workers": 16` → **配置接受、落盘、然后静默当 8 跑**，全程无提示。这比"没有校验"更坑——
校验缺失至少会在下游炸，静默钳位什么都不说。

### 15.4 已落地（`sms_tool/config.py`）

1. **`account_health` 标量类型校验**：把 `workers`/`max_pending`/`batch_timeout_seconds`/
   `account_timeout_seconds`/`relogin_cooldown_seconds`/`fail_skip_after`/`fail_cooldown_seconds`
   接进已有的 `_validate_positive_numbers`（缺键跳过，0 允许——`relogin_cooldown_seconds=0` 表示关冷却）
2. **非致命告警 `config_warnings()`**：
   - 未知 `account_health` 键（白名单 `ACCOUNT_HEALTH_KEYS`，**故意列得宽松**——
     少列会误报合法配置，多列只是漏报一次拼写错误）
   - `workers` 落在 1..8 之外时提示"会按 N 运行"

**为什么未知键是告警不是报错**：把它做成硬失败，会在"用户加了本版本还不认识的键"时
直接搞坏一个原本能跑的安装。梯度设计：类型错 = 报错（真的会炸），不认识 = 告警（可能只是超前）。

### 15.5 验证

新增 `tests/test_config_account_health_validation.py`（15 条）：
未知键告警 / 已知键安静 / **example 里每个键都在白名单内**（防文档与代码再次漂移）/
非 Mapping 段忽略 / workers 超上下限告警带钳位值 / 区间内安静 / 非数字与布尔不误报 /
`workers=-1`、`"four"`、`max_pending="lots"` 报错 / 缺键合法 / `0` 合法 / 越界但仍可加载。

**变异验证 3 个，均被抓**：

| 变异体 | 结果 |
|---|---|
| M1 去掉 `workers`/`max_pending` 的正数校验 | ✅ 3 条红（`DID NOT RAISE ConfigError`） |
| M2 未知键列表置空 | ✅ 1 条红 |
| M3 钳位告警条件恒假 | ✅ 2 条红 |

### 15.6 未做

- **5 个模块直读 `config.json`**（见 15.2）——需先做零引用核查，风险高于 P1，建议单开一轮
- **`config_schema.json` 扩成 value schema**：它是**分片归属清单**（README_EN.md:67 与
  `docs/current/configuration.md:25` 都明写"not JSON Schema"），改名/扩职责会让 C# `ConfigStore` 一起动，
  而 dotnet 链坏无法验证。**不建议**
- **其它段的未知键告警**：同一套机制可以推广，但每段都要各自的白名单，属增量工作

---

## 16. P1-7 IPC：把 `error_codes` 接上，而不是删掉（2026-09-09）

### 16.1 「`error_codes` 是死字段，删掉消除假安全感」—— 建议是反的

零引用核查（`error_codes` 全仓命中）确认：`ipc_schema.json:4` 这一项**确实没有任何校验在引用**。
但"没人引用"≠"没用"——这 5 个码在**两侧都是活的**：

| 侧 | 位置 | 内容 |
|---|---|---|
| Python | `desktop_serve.py:85-89` | `CODE_BAD_REQUEST` … `CODE_INTERNAL` 五个常量 |
| C# | `DesktopReadProtocol.cs:117-138` | `DesktopReadErrorCodes.ToWire` / `Parse` 双向映射 |

**删掉它只会删掉唯一记录契约的地方，漂移依旧不可见**。正确做法是**把检查补上**，
让这个字段从"装饰"变成"真门禁"。审计自己也在 §5 写了"命名强误导"——
正确的响应是补检查，不是删字段。

### 16.2 顺带查出的真实不对称

| 来源 | 码数 | 内容 |
|---|---|---|
| `ipc_schema.json` | 5 | bad_request / unknown_operation / backend_error / watchdog_timeout / internal |
| Python `CODE_*` | 5 | 同上 |
| C# `ToWire` | **8** | 上面 5 个 + `timeout` / `cancelled` / `protocol_mismatch` / `channel_unavailable` |

`channel_unavailable`（通道挂了）、`cancelled`（本地取消）显然是 **C# 客户端本地码**，
永远不会从后端发过来。所以**要求三方集合相等是错的**，会当场误报。

**正确契约是单向的：后端发得出去的码，客户端必须认得。**
（Python 5 个 ⊆ C# `Parse` 可解析集合 —— 实测 5/5 全部命中）

### 16.3 v2 事件信封字段

`desktop_ipc._envelope`（`desktop_ipc.py:71-84`）发：
`schema` / `version` / `type` / `run_id` / `sequence` / `timestamp_ms` / `terminal` / `payload`

C# `BackendProgressEvents.cs:42-62` 从**帧根**读：`version`、`type`、`payload`、`sequence`
（`run_id` 是从 `payload` 里读的，不算信封键）。

### 16.4 已落地

`ipc_schema.json` 新增 `event_envelope_keys`（8 个）；`scripts/ipc_schema_check.py` 新增两处校验：

1. **`error_codes`**：`manifest == Python CODE_*`（精确相等），且
   `Python CODE_* ⊆ C# Parse 可解析集合`（单向，允许 C# 多）
2. **`event_envelope_keys`**：`manifest == Python _envelope 返回的键`（用 AST 取，不用正则）；
   且 **C# 不能读清单外的信封键**

同时把比对逻辑抽成 `check_manifest(manifest, py, py_ipc, cs, cs_events) -> list[str]`，
`main()` 只做 IO + 打印——**否则负向测试根本没法写**（原实现硬读文件）。

### 16.5 踩坑：C# 读取键的正则漏了两种调用形态

第一版只匹配 `root.GetProperty("x")` / `root.TryGetProperty("x")`，实测只抓到 2 个键。
而 `type` 和 `sequence` 是通过辅助函数读的：

```csharp
string.Equals(Text(root, "type"), "event", ...);
Number(root, "sequence");
```

**漏掉它们，等于这个检查对它本来要防的漂移完全失明。** 改成两种形态都匹配后抓到 7 个键
（`run_id` 正确地不在其中——它是 payload 键）。

**同时给"提取器是否为空"加了测试**（`test_extractors_are_not_empty_on_the_real_tree`）——
正则静默变空会让所有比较退化成空集相等，**假绿**。

### 16.6 验证

`tests/test_ipc_schema_check.py` 从 1 条扩到 **12 条**：4 个提取器单测（含两种调用形态的回归）、
契约正向、C# 允许有额外本地码、manifest 缺码、后端码客户端不认、
Python 建了清单外的键、C# 读了清单外的键、真实树通过、提取器非空。

**变异验证 2 个，均被抓**：

| 变异体 | 结果 |
|---|---|
| M1 C# 读取键正则退回只认直接调用 | ✅ 红：`['payload','version'] != ['payload','sequence','type','version']` |
| M2 「客户端认不识」检查置空 | ✅ 红：`test_backend_code_the_client_cannot_parse_is_reported` |

### 16.7 未做

- **`ipc_schema.json` 改名**：审计指出它描述的是 desktop-read op 协议（v1），与 v2 事件信封
  **不是同一物**，命名强误导。改名要动 `.github/workflows/ci.yml` + `tests/README.md` +
  `docs/`，且 C# 侧不可编译验证 → **未动**
- **payload 内部的 schema**：每个 domain 的事件 payload 字段都不同，做全量 schema 收益低、
  维护成本高。只锁**信封**这一层

---

## 17. 追加项：绕开配置分片直读 `config.json`（2026-09-09）

> 起因：§15 核查时顺带扫到「5 个模块定义了 `DEFAULT_CONFIG_PATH`」，初判为
> 「绕过 CFG 和分片直读 config.json」。**复核后 4/5 是假警报**——这是本轮
> 「审计结论必须先回源码复核」的第四次印证，如实记录。

### 17.1 初判 vs 复核

| 位置 | 初判 | 复核结论 | 证据 |
|---|---|---|---|
| `sms_tool/omakse_client.py:35` | 绕过 | **假警报** | `_load_json(:44)` 第 45 行：路径等于 `DEFAULT_CONFIG_PATH` 时转 `load_merged_config()` |
| `sms_tool/paypal_link/gen_link.py:1223` | 绕过 | **假警报**（但见 17.3） | 同上（`:248`）；且实测 `_plm._load_json is gen_link._load_json` → `True` |
| `sms_tool/upi_link.py:60` | 绕过 | **假警报** | 同上（`:65`） |
| `sms_tool/paypal_protocol.py:31` | 绕过 | **零引用死常量** | 全仓 grep 只有定义行；唯一导入方 `paypal_authorization_queue.py:19` 只用 `extract_ba_token` |
| `scripts/probe_account_liveness.py:40` | 绕过 | **真** | `Path("config.json").read_text()`，无哨兵分支 |
| `verify_proxy.py:35`（复查时新扫出） | — | **真** | `open("config.json")`，无哨兵分支 |

**关键实证**：这四个模块的 `DEFAULT_CONFIG_PATH` 是**哨兵值**，不是真读文件——

```
if os.path.abspath(path) == os.path.abspath(DEFAULT_CONFIG_PATH):
    from ..config import load_merged_config
    return load_merged_config()
```

传这个常量时走分片合并，传别的路径才裸读。实测 `_plm._load_json(DEFAULT_CONFIG_PATH)`
返回 **22 个键**，与 `load_merged_config()` 完全一致；而真正的根 `config.json`
只有部分键 —— 证明走的是分片。

**这个模式本身有命名误导**：常量叫 `config.json`，实际语义是「走分片」。
本轮初判失误正是被名字带偏的，已把理由写进 `gen_link.py:1215` 的注释。

### 17.2 唯一坐实的真 bug：`proxy.registration` 类型分裂

全仓扫描（正则：同一行内同时出现读取动作与 `config.json` 字面量）只找出 2 处真绕过。
其中 `probe_account_liveness.py` 的后果是**硬的**：

| 来源 | `proxy.registration` 的值 |
|---|---|
| 分片（`runtime/proxy.json`） | 字符串，**1 个**代理 |
| 根 `config.json` | **96 项列表** |

而 `load_config_proxy()` 的逻辑是 `str(proxy.get(key) or "").strip()` ——
把整个列表的 Python 字符串形式当成代理 URL 返回：

```
'http://lizi1_custom_zone_US_sid_36268881_time_5:451203zhy@us.ipwo.net:7878'   # 改后（分片）
"['http://...', 'http://...', ...]"                                            # 改前（根文件）
```

这个函数的结果直接喂给 `probe_account_liveness(proxy=...)`（`:142`），
也就是**批量测活此前一直拿到一个不可用的代理串**。

### 17.3 顺带挖出：`gen_link.DEFAULT_CONFIG_PATH` 指向一个影子文件

`gen_link.py` 位于 `sms_tool/paypal_link/`，但只上了一层目录：

```python
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))   # sms_tool/paypal_link
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)                  # sms_tool   ← 少一层
```

于是 `DEFAULT_CONFIG_PATH` = `sms_tool/config.json`：1294 B、8-16 修改、
被 `.gitignore:11` 排除、**含过期明文代理凭据**（`gate-hk.kookeey.info:1000`）。

**为什么一直没暴露**：所有生产调用都传这个常量本身，哨兵比较恒真，
错误的取值从不参与实际 IO。但它是个定时炸弹——任何一次传入真实项目根路径的
调用，都会命中一次指向错误文件的裸读。

### 17.4 已落地

| # | 位置 | 改动 |
|---|---|---|
| 1 | `sms_tool/paypal_link/gen_link.py:1219` | `PROJECT_ROOT` 改为上**两层**，指向真实项目根；补注释说明坑 |
| 2 | `sms_tool/paypal_protocol.py:29-31` | 删零引用的 `SCRIPT_DIR`/`PROJECT_ROOT`/`DEFAULT_CONFIG_PATH`；连带删悬空的 `import os` |
| 3 | `scripts/probe_account_liveness.py:38-51` | `load_config_proxy()` 改走 `load_merged_config()`；新增顶层 import |
| 4 | `verify_proxy.py:33-38` | `_load_config()` 改走 `load_merged_config()`；删掉随之无用的 `import json` |

`verify_proxy.py` 只用 `proxy.default` / `proxy.pool` / `paypal.proxies`，
这几个键两边取值一致，所以它的修复收益是**跟随分片 + 不再依赖 cwd**，
不是「已经在错」。如实区分，不夸大。

### 17.5 门禁（`tests/test_config_shard_access.py`，新增 8 条）

不 import 被检查的脚本（避免顶层副作用），全部走 AST / 正则静态分析：

- **`NoDirectConfigJsonReads`** —— 全仓扫描，白名单外禁止直读。
  白名单：`sms_tool/config.py`（分片加载器本体）、`sms_tool/config_usage.py`、
  `services/`（独立服务自带 config.json）、`tests/`（tmp 文件）
- 两条**负向测试**：扫描器必须能抓到明显直读、必须遵守白名单
  （否则「永远返回空」会让主断言退化成假绿）
- **`CanonicalConfigPathIsReal`** —— `DEFAULT_CONFIG_PATH` 必须等于项目根且文件存在；
  另断言 `omakse_client` / `upi_link` / `gen_link` 算出的项目根**互相一致**
- **`DeadConfigConstantsStayDead`** —— 用 AST 取模块顶层赋值名，
  断言 `paypal_protocol` 不再出现那三个常量
- **`ShardLoaderIsWired`** —— canonical 路径必须经 `load_merged_config`

### 17.6 变异验证 4 个，均被抓

| 变异体 | 结果 |
|---|---|
| M1 `load_config_proxy` 退回直读（换用 `open()` 写法，非原样恢复） | ✅ 红，报出 `probe_account_liveness.py:47` |
| M2 `PROJECT_ROOT` 改回只上一层 | ✅ 红 2 条（路径断言 + 兄弟模块一致性） |
| M4 `paypal_protocol` 加回 `DEFAULT_CONFIG_PATH` | ✅ 红 `DeadConfigConstantsStayDead` |
| M5 把扫描器正则改成永不匹配 | ✅ 红 `test_scanner_actually_matches_a_direct_read` |

两条观察，如实记录：

1. `test_gen_link_canonical_path_exists` **抓不住 M2** —— 影子文件真实存在，
   所以「文件存在」这条断言对「指向错的但存在的文件」无效。真正拦住 M2 的是
   另外两条。保留它是因为能防「影子文件被删后静默 `{}`」。
2. M5 是**对负向测试做变异**——验证负向测试本身不是死的。扫描类门禁不做这一步，
   很容易造出一个「永远绿但什么也没查」的门禁。

### 17.7 未做

- **`sms_tool/config.json` 影子文件未删**。它被 `.gitignore:11` 排除（不入库是
  有意的，所以「凭据泄漏」降级），但含过期明文代理凭据，且是 17.3 那个 bug 的
  诱因之一。**交给老板拍板**：删 / 留 / 挪进 `local/`。
- **`services/mail-otp-web/app.py:26`** 进了白名单——它是独立服务的自有配置，
  不是项目根那份。若将来它也需要读主配置，应单独接线而不是复用这个白名单。
- 全仓扫描目前只覆盖 `.py`。C# 侧（`SmsWorkbench/`）读的是分片，不读
  `config.json`，已确认；但**没有自动化门禁**，C# 不可编译验证，留作人工复查项。

---

## 18. P2-15 `heavy_lane_slots`：小并发下隔离失效（2026-09-09）

### 18.1 复核：审计结论成立，但后果要修正

原公式（`account_recovery.py:69`）：

```python
heavy_lane_slots = max(1, min(max_workers, max(2, max_workers // 2)))
```

全量枚举：

| max_workers | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 16 |
|---|---|---|---|---|---|---|---|---|
| 旧 heavy | 1 | **2** | 2 | 2 | 2 | 3 | 4 | 8 |
| 窄于池？ | — | **否** | 是 | 是 | 是 | 是 | 是 | 是 |

**只在 `max_workers ∈ {1, 2}` 时失效**（1 个 worker 时物理上无法隔离，不算缺陷）。

**后果要修正**：审计只说"失效"，没说表现。两处 acquire **都是阻塞式**
（`:172` 的 `browser_slots.acquire(timeout=remaining)`、`:222` 的
`relogin_slots.acquire(timeout=queue_remaining)`，注释明写
"Queue instead of skipping"），所以**不会丢账号**——表现是慢恢复占满 worker 池，
**便宜的 HTTP probe 排队等重活**。

**为什么值得修**：`max_workers=2` 不是边缘值，而是**配置默认值**——
`account_health_queue.py:121` 的 `int(health.get("workers") or 2)`。
即默认配置下 `browser_slots` / `relogin_slots` **从来没起过隔离作用**，
而注释（`:62-68`、`:74-76`）明确宣称它们要
"stay narrower than the probe pool so a slow recovery cannot starve cheap probes"。
**实现与宣称的意图不符**。

### 18.2 已落地

抽成模块级纯函数（可测），并锁住不变量：

```python
def _heavy_lane_slots(max_workers: int) -> int:
    if max_workers <= 1:
        return 1
    return max(1, min(max_workers - 1, max_workers // 2))
```

新旧对比：**只有 `max_workers ∈ {2, 3}` 从 2 降到 1**，4 以上完全不变
（因为 `max_workers - 1 >= max_workers // 2`）。改动面极小。

代价说清楚：全是重活的批次（例如整批都要 401 重登）在 `workers=2` 下并发从
2 降到 1，**吞吐减半**。这是设计意图本身的代价——注释已经选择了
"宁可慢也不能饿死轻 probe"，所以按意图修。

### 18.3 门禁（`tests/test_heavy_lane_slots.py`，新增 44 条）

- 表驱动验证取值（含边界 2/3 与大池 12/16）
- **核心不变量参数化**：`range(2, 33)` 全部满足 `1 <= slots < max_workers`
- 退化池（0 / -5 / 1）不返回 0 或负数
- 负向测试：不能退化成常量（`_heavy_lane_slots(32) > _heavy_lane_slots(8) > _heavy_lane_slots(2)`）

### 18.4 变异验证 2 个 —— 1 个被抓，1 个**存活（等价变异）**

| 变异体 | 结果 |
|---|---|
| M1 恢复旧公式 `max(1, min(mw, max(2, mw//2)))` | ✅ 红 3 条（`test_expected_capacity[2]`、`[3]`、`test_strictly_narrower_than_the_pool[2]`） |
| M2 删掉 `if max_workers <= 1: return 1` 分支 | ⚠️ **存活，44 全绿** |

**M2 是等价变异，不是测试漏了**。删掉分支后 `max_workers=1` 走
`max(1, min(0, 0))` —— 外层的 `max(1, ...)` 已经把结果兜回 1，行为完全相同。
也就是说那个 `if` 分支**本来就是冗余的**。

处置：**保留它**，但在 docstring 里写明"外层 `max(1, ...)` 已兜住下界，此分支冗余，
保留是为了把决策写成代码，并防未来改外层下界时静默退化成 0"。

**方法论收获**：`test_single_worker_pool_has_no_room_to_isolate` 实际断言的是
**外层 `max(1, ...)`** 的行为，不是那个分支。等价变异无法用测试区分——
**遇到存活的变异要先判断它是不是等价的，别急着加测试去抓一个本来就抓不到的东西**。

---

## 19. P2-13 账号状态词表：Python 真源 vs C# 消费方（2026-09-09）

### 19.1 复核：「三套枚举」不成立，Python 侧本就有单一真源

审计写的是「三套账号状态枚举 + C# 第四套，无映射表」。逐个核对后**不成立**：

| 候选 | 实际是什么 |
|---|---|
| `store/normalize._status`（`:268`） | **账号状态的单一真源**，产出状态词 |
| `account_scan._SCAN_STATUS_BY_FAILURE_CLASS`（`:48`） | **已经是映射表**，且注释明写 "reusing the same vocabulary `store/normalize._status` already persists"，四个值与真源**完全一致** |
| `account_cleanup._TERMINAL_STATUSES`（`:13`） | 终态**子集**（3 个），不是独立枚举 |
| `logging_setup._STATUS_LABELS`（`:129`） | **任务/阶段状态**（running/success/failed/cancelled/retry_pending），**不是账号状态** |

也就是说 Python 侧**没有三套**，而是"一个真源 + 一个已经对齐的映射表 + 一个子集 + 一个无关的东西"。
这是本轮第五次印证「审计结论转述两道手会变形」。

### 19.2 真问题在跨语言：C# 不认得 P1-5 新增的失败词汇

``_status`` 的产出全集（AST 提取 ``return`` 常量 + ``explicit in {...}`` 成员）：

```
account_deactivated, at_invalid, network_failed, mailbox_failed, auth_state_failed,
rate_limited, k12_joined, k12_requested, k12_left, k12_verify_failed, pending,
failed, paypal_pm_created, paypal_ready, paypal_failed, registered
```

C# `AccountStatusInterpreter.DisplayAccountStatus`（`:162`）里 `status.Equals(...)` 认得的词，
**在本次修复前不含** `network_failed` / `mailbox_failed` / `auth_state_failed` / `rate_limited`
—— 四个全是 grep 计数 **0**。这四个词是 P1-5（account_scan 显式状态）引入的，
**Python 加了，C# 没跟上**。

### 19.3 后果不是"显示空白"，是**误报成健康账号**

`DisplayAccountStatus` 的兜底是：

```csharp
if (hasRt && access.Length > 0) return "已注册";
```

而网络抖动 / 限流失败的账号**照样有 refresh token 和 access token**
（`_status` 只在 `failure_class == X and success is False` 时才给出这些词，
与 token 是否存在无关）。所以它们会掉进这一行，**被显示成"已注册"**。

用户视角：一批账号测活因网络问题失败，界面上却全是"已注册"。**静默，且方向是往好里错**。

### 19.4 已落地

`AccountStatusInterpreter.cs` 补 4 个分支，插在 `return "已注册"` **之前**（顺序是关键）：

```csharp
if (status.Equals("network_failed", ...)) return "网络失败";
if (status.Equals("mailbox_failed", ...)) return "邮箱失败";
if (status.Equals("auth_state_failed", ...)) return "会话失效";
if (status.Equals("rate_limited", ...)) return "限流";
```

中文文案沿用现有风格（"账号掉号" / "AT失效" / "手机验证"，简短无 emoji）。
**文案是产品决策**，若要改只改这 4 个字符串即可，不影响结构。

### 19.5 门禁（`tests/test_account_status_vocabulary.py`，新增 5 条）

契约**单向**：Python 产出的词 C# 必须认得；反过来不成立（C# 有 `paypalStatus` /
`refreshTokenStatus` 等本地词）。

- `python_status_vocabulary()` —— AST 取 `_status` 的 `return` 常量 + `explicit in {...}` 成员
- `csharp_recognised_statuses()` —— 正则 `status\.Equals\("([a-z0-9_]+)"`。
  **只匹配小写 `status.Equals`**，`paypalStatus.Equals` / `refreshTokenStatus.Equals`
  首字母大写，不会被误抓（它们是别的参数）
- 白名单 `_CS_EXEMPT = {account_deatived, pending, registered}`，每条都写了理由
  （`account_deatived` 是落盘的历史拼写错误、只作输入；`pending` 与 `registered`
  由 C# 兜底正确处理，语义一致）

### 19.6 变异验证 2 个，均被抓

| 变异体 | 结果 |
|---|---|
| M1 删掉 C# 新加的 4 个分支（= 回到"没修"的真实状态） | ✅ 红 2 条 |
| M2 把 C# 提取正则改成 `statusX\.Equals` | ✅ 红 3 条，其中 **`test_csharp_vocabulary_is_not_empty`** 抓住了空集 |

M2 是防假绿的关键：提取器一旦变空，`vocab - known - exempt` 会退化成空集、
断言恒真。P1-7 已经踩过一次（C# 读取键正则漏抓两种形态），这里靠"非空断言"堵住。

### 19.7 未做 / 风险

- 🔴 **C# 改动未编译验证**（dotnet 链坏，`NuGet.targets(782,5)`）。改动是 4 行
  `if (...Equals(...)) return "...";`，与相邻行格式完全一致，风险低，但**没有编译凭据**。
- **中文文案未与老板确认**（"网络失败"/"邮箱失败"/"会话失效"/"限流"）。
- `pending` 状态在有 access token 时仍会显示"已注册"（`normalize.py:291` 的
  `at_probe_pending and access_token`）。这是**既有行为**，不是本次引入，未动；
  若认为 AT 探针待定不该显示"已注册"，需单独决策。
- 本节只查了 `AccountStatusInterpreter.cs`。**漏了 C# 的第二个状态解释器**
  `BackendResultInterpreter.ScanStatusLabel`（消费扫描 status，与账号 status 是
  不同字段）—— 它其实**已经**认得 `network_failed` 等词，两个解释器并不一致。
  已在 §20 把两个都纳入门禁。

---

## 20. P2-14 failure_class 覆盖：用户取消被算成扫描失败（2026-09-09）

### 20.1 复核：「`failure_class` ↔ `error_code` 建映射表」不可行

审计原话是"建映射比解耦划算"。核对后**方向不对**：

- **`error_code` 是开放集合**。各处自定义（`checkout_bad_response`、
  `missing_access_token`、`gcash_provider_failed` …），数以百计且持续增长。
  建"全量映射表"既做不完，也会立刻腐烂。
- **映射其实已经存在**，只是形态是子串匹配而非表：`error_text()`
  （`error_classification.py:99-116`）把 `error_code` 字段拼进待匹配文本，
  `classify_error` 再按 marker 归类成 failure_class。
- 所以真正可锁的不变量不是"两套词汇互通"，而是：
  **`failure_class` 是封闭集合，消费方必须覆盖它**。

### 20.2 真 bug：`cancelled` 静默掉进 `scan_failed`

`classify_error` 产出的 failure_class 全集（AST 提取 `return` 常量）：

```
cancelled, account, mailbox, rate_limit, network, auth_state, unknown
```

而 `_SCAN_STATUS_BY_FAILURE_CLASS`（`account_scan.py:48`）**只映射了 4 个**。
注释（`:59-61`）说明了 `account` / `unknown` 是有意 fallthrough ——
**但没提 `cancelled`**。

查 `CANCELLED_ERROR_MARKERS`（`error_classification.py:86`）：

```python
CANCELLED_ERROR_MARKERS = ("registration_cancelled", "cancelled_by_user")
```

**字面就是"用户主动取消"**，且 `registration_cancelled` 同时被列进
`TERMINAL_ERROR_MARKERS`（硬停止、不可重试）。把它算成 `scan_failed` 有两个后果：

1. 失败计数被用户操作污染；
2. `account_scan.py:481` 用 `status in {"relogin_failed","scan_failed"} and _token_probe_is_invalid(...)`
   推断 token 失效 —— **用户取消会去推断一个根本没发生的 token 故障**。

### 20.3 已落地

| 位置 | 改动 |
|---|---|
| `account_scan.py:48` | 加 `"cancelled": "scan_cancelled"` + 注释说明理由 |
| `commands/accounts.py:386` | 加 `("scan_cancelled", "扫描已取消")` 中文标签 |
| `BackendResultInterpreter.cs:307` | 加 `"scan_cancelled" => "扫描已取消"` |

### 20.4 门禁（`tests/test_scan_status_vocabulary.py`，新增 8 条）

- **`FailureClassCoverage`**：`classify_error` 产出 ⊆ 映射表 ∪ `_FALLTHROUGH_CLASSES`。
  白名单只有 `account` / `unknown`，各带理由（它们要靠 `:481` 的 token_probe 提升，
  映射成独立状态反而绕过那条路径）
- 负向：白名单条目不得腐烂；映射表的键必须是真实的 failure_class（防拼写/弃用）
- **`ScanStatusCrossLanguage`**：映射表的**值**必须被 C# `ScanStatusLabel` 认得
- 两个提取器都配**非空断言**

### 20.5 补上 §19 漏掉的一半

写 §19 时只查了 `AccountStatusInterpreter.cs`。本次发现 C# 有**两个**状态解释器，
消费不同字段：

| C# 函数 | 消费字段 | Python 产出方 |
|---|---|---|
| `AccountStatusInterpreter.DisplayAccountStatus` | 账号 status | `store/normalize._status` |
| `BackendResultInterpreter.ScanStatusLabel` | 扫描 status | `account_scan._scan_failure_status` |

`ScanStatusLabel` 其实**早就有** `network_failed` → "网络失败"，
而 `DisplayAccountStatus` 直到 §19 才补上 —— **两个解释器状态不一致**。
§19 的门禁只覆盖前者，本节补上后者，**两侧才都闭环**。

### 20.6 变异验证 2 个，均被抓

| 变异体 | 结果 |
|---|---|
| M1 删掉 `"cancelled": "scan_cancelled"`（= 回到"取消算失败"） | ✅ 红 3 条 |
| M2 删掉 C# 的 `"scan_cancelled" => "扫描已取消"` | ✅ 红 2 条 |

### 20.7 未做 / 风险

- 🔴 **C# 改动仍未编译验证**（本轮累计 5 行：`AccountStatusInterpreter` 4 行 +
  `BackendResultInterpreter` 1 行）。全是与相邻行同构的单行语句，但**没有编译凭据**。
- 中文文案（"扫描已取消" / "网络失败" / "邮箱失败" / "会话失效" / "限流"）由我按
  现有风格拟定，**产品文案待确认**。
- `failure_class` 的**其他消费方**（`batch_runner.py:312` 的
  `{"network","mailbox","auth_state"}`、`batch_circuit_breaker.ENVIRONMENT_FAILURE_CLASSES`）
  未纳入门禁。它们是有意处理子集，若要覆盖需先确认语义，本轮未动。

---

## 21. P2-18 遗留 `logs/sms_tool.log`：审计原话过时，真身是「已修 bug 的探针残留」（2026-09-09）

### 21.1 审计原话 vs 复核

| | 审计原话 | 复核结果 |
|---|---|---|
| 定性 | 「遗留 `logs/sms_tool.log`（74 字节，09-02 停更），删，消除双日志目录困惑」 | **定性偏保守**。它不只是"遗留"，是 `logging_setup.py` 一个**已修复的路径回落 bug 的现场残留** |
| 危害 | 双日志目录困惑 | 实际危害更低（生产零引用），但**成因有价值**：它证明那个 bug 真实发生过 |

**证据链**：

1. `sms_tool/logging_setup.py:187-197` 的 docstring 自述：

   > "`paths.runtime_file(cfg, filename)` takes the **config** as its first
   > argument, not a directory. The previous call passed `"logs"` there, which
   > raised `AttributeError` on `cfg.get`; the broad `except Exception` then
   > silently fell back to a repo-root `logs/` directory."

   即：日志路径曾经静默落到**仓库根 `logs/`**，而不是 `runtime/logs/`。该 bug 已修
   （`_default_log_path()` 现在走 `runtime_dir(current_config_data()) / "logs"`）。

2. 文件内容只有**一行**：

   ```
   2026-09-02 19:31:24,498 WARNING sms_tool.test logging-setup-guard-marker
   ```

   `sms_tool.test` + `logging-setup-guard-marker` = 修复当天的**一次性验证探针**，
   不是任何业务流程写出来的日志。mtime `09-02 19:31` 与修复时间吻合。

3. **生产零引用**：全仓 grep `sms_tool.log` 命中的都是 `logging_setup.py` 里的字面
   常量（路径由 `_default_log_path()` 决定，落在 `runtime/logs/`），以及
   `tests/conftest.py:146`、`tests/test_logging_setup.py:31/80` —— 后者说的是
   **runtime 沙箱内**的相对路径，与仓库根 `logs/` 无关。

### 21.2 已落地

删除 `logs/sms_tool.log`。**删除前备份到 `runtime/_trash-20260909/logs-sms_tool.log.bak`，
`diff` 校验字节级一致**。

回滚路径：`logs/` 整个目录**不被 git 跟踪**（`git ls-files logs/` 为空），所以没有
git 回滚；备份文件是唯一回滚路径，内容仅 1 行 / 74 字节，已原样留档。

### 21.3 顺带发现（审计清单外，未动）

同一目录下还有两处**同样零引用**的残留，本轮**未删**，等老板确认：

| 路径 | 大小 | mtime | grep `build_wpf` 结果 |
|---|---|---|---|
| `logs/build_wpf.log` | 2298 B | 2026-08-29 | **0 处引用** |
| `logs/roxynet/`（`run_20260825.log` + `run_20260826.log`） | 193 KB | 08-25 / 08-26 | 属 Roxy 浏览器侧输出 |

判断：都不是 Python 侧写出来的（`logging_setup` 只写 `runtime/logs/`），是历史手工/
外部工具产物。删不删不影响功能，但**留着会继续让人以为 `logs/` 是活跃日志目录**——
这恰是 P2-18 想消除的困惑。建议一并清掉，或整个 `logs/` 目录从仓库视角标记为历史。

---

## 22. P2-17 `docs/audits/`：建内容索引 + 命名约定服从既成事实（2026-09-09）

### 22.1 命名：改的是约定，不是文件名

审计原话「命名违规 6/22（`scan-` 连字符 vs 约定的 `scan_`）」。复核后确认违规成立
——`README.md:14` 白纸黑字写的是 `scan_*_YYYY-MM-DD.md`。但**修复方向选了反的**：

| 方案 | 动作 | 代价 |
|---|---|---|
| A（改文件名） | 把 4 个 `scan-*` 改成 `scan_*` | 破坏 **11 处交叉引用**（`docs/` + 工作区记忆），且约定格式要求日期在**末尾**，改名后文件名更难读 |
| **B（改约定）** ✅ | README 改成 `scan-YYYY-MM-DD-<topic>.md` | 0 处破坏；唯一下划线文件显式豁免 |

选 B 的依据是**数出来的**，不是感觉：

- `docs/audits/` 24 个文件里 **22 个用连字符、2 个用下划线**（`scan_headless_browser_proxy_fingerprint_2026-08-29.md`、
  `browser_profiles_manifest_20260909.txt`）。连字符是压倒性既成事实。
- 4 个 `scan-` 文件被 **11 处**交叉引用（`grep -rl` 逐个计数：3/3/3/2）。
- 唯一的 `scan_` 文件被 3 处引用，README 里明确标注 grandfathered。

README 里加了一段「Why `scan-` and not `scan_`」说明，把决策依据写进文件本身，
避免下一个人再按旧约定"修"回去。

### 22.2 内容索引

原 README 24 行，24 份审计文件**无一列出**。新增 `## Contents`，按**类型分组**
（比时间倒序更好导航）：

| 分组 | 数量 |
|---|---|
| Scan reports（针对具体链路的横向扫描） | 5 |
| Audit rounds（按轮次编号的全仓审计） | 10 |
| Module-specific audits | 2 |
| Security & credentials | 2 |
| Snapshots & one-off evidence | 4 |
| Not in this directory（指向 `docs/releases/`、`docs/architecture.md` 等） | — |

每行 = 文件名 + 日期 + 一句话主题。另加 `landing-YYYY-MM-DD-*.md` 命名约定
（原 README 漏了这一类，但 `landing-2026-09-07-items-1-9.md` 实际存在）。

### 22.3 防腐烂门禁 + 三个变异

`tests/test_audit_index_completeness.py`（**新增 4 条**）：

1. `test_extractors_are_not_empty` —— 两个提取器都必须 >15 条，防空集恒真
2. `test_every_audit_file_is_listed_in_the_index` —— 磁盘文件 ⊆ 索引
3. `test_index_never_references_a_file_that_does_not_exist` —— 索引 ⊆ 磁盘
4. `test_index_is_a_real_table_not_a_prose_dump` —— 必须是表格行，不是散文里顺带提到

| 变异体 | 结果 |
|---|---|
| **M1** 从索引表删掉 `sentinel-account-health-migration.md` 那一行 | 第 1 次 **存活**（见下）→ 收紧后 ✅ 红 |
| **M2** 索引里加一个不存在的 `ghost-audit-2026-01-01.md` | ✅ 红 1 条 |
| **M3** 把提取正则改坏（`\.md` → `\.mdx`） | ✅ 红 2 条，**含非空断言** |

### 22.4 🔴 M1 第一次存活 —— 又一条假绿模式

初版 `filenames_mentioned_in_index()` 扫的是 **README 全文**。删掉索引表格里那一行后
4 条测试**全绿**，因为同一个文件名在「Naming conventions」段也被提到了：

```python
- `sentinel-account-health-migration.md` — one-time migration record.
```

**"被提到"不等于"被索引"**。修正：提取范围收窄到 `## Contents` 之后；dangling 检查
仍用全文（任何地方指向不存在的文件都应报错）。两个提取器拆成
`filenames_mentioned_in_index()`（索引区）和 `filenames_mentioned_anywhere()`（全文）。

这是本轮**第二次**靠变异抓出门禁自身的设计缺陷（第一次是 P2-13 的 M2 正则误报）。
结论写进 `audit-playbook.md`：扫描门禁的**提取范围本身**也是变异目标。

### 22.5 验证与未做

- 新门禁 4 条全绿；三个变异全部被抓后已恢复（README 与备份 `diff` 字节级一致）。
- README 里 23 个裸文件名引用**逐个 `test -f` 校验**，23/23 存在。
- **未做**：不为"索引内容是否与文件实际内容一致"设门禁 —— 摘要是我读 H1 + 首段
  人工整理的，机器无法判断摘要准不准。这层只能靠人。

---

## 23. P2-16 `runtime/` retention：审计点错了主体，且"按年龄删"会删掉账号主库（2026-09-09）

### 23.1 审计原话 vs 实测

| | 审计原话 | 实测（2026-09-09 11:18） |
|---|---|---|
| 量级 | 「3879 批次文件 + 4MB jsonl」 | **9618 文件 / 99.5 MiB** —— 审计点到的确实是主要目录，但**漏了最大的一块** |
| 磁盘压力 | 隐含"占空间" | **99.5 MiB，不是空间问题**。真正的问题是**文件数** |
| 主体 | `payment_batches/` | **`payment_batch_locks/gates/` 3949 个空 `slot-0.lock`** = 全部文件数的 41% |

`runtime/` 实测构成（文件数降序）：

| 目录 | 文件 | 大小 | 最老 |
|---|---|---|---|
| `payment_batch_locks/` | **3949** | 0 B | 08-19（21 天） |
| `payment_batches/` | 3879 | 12.4 MiB | 08-02（38 天） |
| `logs/` | 2 | 5.5 MiB | 09-08 |
| `mailbox_imports/` | 71 | 2.5 MiB | 08-05（35 天） |
| `payment_operations/` | 1612 | 466 KiB | 09-03 |
| （根目录散落） | 63 | **77.6 MiB** | 05-22 |
| `browser_profiles/` | **0** | 0 B | —— |

两个必须记住的点：

1. **`payment_batch_locks/gates/<batch>/slot-0.lock` 是 3949 个 0 字节文件**。
   每跑一个支付批次建一个 gate 目录 + 一个 slot 锁，**批次结束后从不释放**。
   文件数只增不减。这是 P2-16 真正的主体，审计没提到。
2. **根目录散落的 63 个文件占 77.6 MiB**，其中 `accounts.sqlite3` 一个就 **67 MiB**。

### 23.2 为什么不能"按年龄删"

`accounts.sqlite3`（67 MiB，09-09 仍在写）是**账号主库**。同目录还有
`payment_link_runs.jsonl`（支付对账主线）、`paypal_proxy_state.json` /
`registration_proxy_health.json`（P1-2 刚接上的代理健康状态）。
一个朴素的 `find -mtime +30 -delete` 会直接毁掉生产数据。

→ retention 必须是**规则驱动 + 显式永不删除名单**，不是年龄扫描。

### 23.3 已落地：`scripts/runtime_retention.py`（独立 CLI，未接入任何热路径）

设计三原则：

1. **规则列表，不是年龄扫描**。6 条规则，每条带 `min_age_days` 和一句理由
   （写进 `Rule.note`，让下一个人知道为什么是这个天数）。
   `is_protected()` 是**硬否决**，在规则之前执行，CLI 无法覆盖。
2. **relocate 而不是 delete**。`--apply` 把候选同盘 `os.rename` 到
   `runtime/_retention/<时间戳>/`。同盘 rename 是元数据操作（本机实测：
   20 GiB / 11 万文件 **0.041 秒**），且**天然可回滚**。
   真删除要显式 `--purge`，且**单线程顺序**——并发会把火绒实时扫描队列压爆（实测 100% 失败）。
3. **默认 dry-run**。不带 `--apply` 只打印计划，一个字节都不动。
   门禁 `test_cli_defaults_to_dry_run` 锁死这条。

覆盖 P0-1：`browser_profiles/` 作为一条规则（3 天），运行中的 profile mtime 是新的，
年龄下限天然不会误删。当前目录是空的（09-09 已清），但 `external_sessions/profiles.py:57-59`
仍无条件写 `user_data_dir`，**会反弹** —— 这条规则是止血，不是根治。

### 23.4 试跑结果

```
candidates   : 3136 file(s), 2,352,333 bytes
by rule:
    3103  empty batch gate locks
      20  mailbox imports
      11  per-batch payment artifacts
       2  one-off scan snapshots
```

（未执行 `--apply`。`payment_batch_locks` 总数 3949，其中 846 个在 7 天年龄下限内被保留。）

### 23.5 门禁 + 三个变异

`tests/test_runtime_retention.py`（**新增 23 条 + 27 subtests**），权重全在**否决逻辑**上。
另有三条安全护栏：生产代码不得 import 本模块、CLI 默认 dry-run、脚本可独立执行。

| 变异体 | 结果 |
|---|---|
| **M1** `is_protected` 里 `.lock` 改回无条件保护（= 回到工具刚修好的那个 bug） | ✅ 红 2 条 |
| **M2** 从 `NEVER_DELETE` 移除 `accounts.sqlite3` | **存活 —— 等价变异** |
| **M2'** 改移除 `payment_link_runs.jsonl`（无后缀兜底） | **第 1 次仍存活 → 假绿 → 收紧后** ✅ 红 1 条 |
| **M3** `if not args.apply` 反转（默认就 relocate） | ✅ 红 1 条 |

### 23.6 🔴 两次变异存活，两个不同原因

**M2（等价变异）**：`accounts.sqlite3` 有**双重保护** —— 名单条目 + `.sqlite3` 后缀规则。
移除名单条目不改变任何行为。已在源码注释写明"冗余是刻意的纵深防御"，
**不假装测试抓到了它**。

**M2'（真缺陷，假绿）**：移除 `payment_link_runs.jsonl` 后 23 条**全绿**。
根因是 `test_never_delete_survives_extreme_age` 把文件放在 **runtime 根目录**，
而那里**没有任何规则会匹配** —— 测试通过是因为"没规则命中"，不是因为"veto 生效"。
**veto 根本没被测到。**

修正：文件必须放在**规则确实覆盖的目录**下（`payment_batches/`），并加一个
**哨兵断言**：同目录放一个普通文件，断言它**确实被规则收走** —— 否则
"veto 生效"的断言就是空的。

```python
self.assertIn(
    "ordinary.json",
    planned,
    "sentinel: the rule must fire here, or the veto check below is vacuous",
)
```

**这是本轮第三次靠变异抓出门禁自身缺陷**（前两次：P2-13 提取正则误报、
P2-17 提取范围过宽）。三次的模式都一样：**测试通过的原因不是我以为的那个原因**。

### 23.7 未做 / 需要拍板

- **未执行 `--apply`**：本次只做工具 + 门禁，不动真实数据。等老板确认后再跑。
- **未修源头**：`payment_batch_locks` 的锁为什么不释放，是独立问题（可能批次异常
  退出没走清理路径），本轮只做了回收工具，没查泄漏原因。
- **`logs/build_wpf.log` / `logs/roxynet/`**（见 §21.3）不在 `runtime/` 下，
  本工具的规则管不到，仍等确认。
- `runtime/_retention/` 自身**没有上限**：反复 `--apply` 会让它涨回原样。
  真正的清理还是要靠定期 `--purge`。

---

## 24. P2-12 热路径 print：数量对不上，且「全转 logger」是行为变更不是重构（2026-09-09）

### 24.1 复核：495 处而不是 378 处，且不能一概而论

审计原话「热路径 378 处 print；logger 覆盖率 12%；根治后可删 C# `BackendLogPresenter.cs:38-55`
黑名单」。复核结果：

| | 审计 | 实测（09-09） |
|---|---|---|
| print 数量 | 378 | **495**（`grep -rn "^\s*print(" sms_tool/`） |
| 分布 | 未给 | 分散在 100+ 文件；TOP 5：`paypal_reverse.py` 35、`phone_reuse.py` 31、`nodriver_paypal.py` 31、`commands/payment_links.py` 30、`sentinel_tokens.py` 27 |

**但 print 不能一概而论**，至少两类：

- **命令层**（`commands/*.py`、`cli.py`）：用户直接在终端看的输出，**必须保留 print**。
- **库/引擎层**：诊断进度，理论上可转 logger。

### 24.2 🔴 为什么「全转 logger」不是重构，是行为变更

`cli.py:418-424` 的注释把这件事写死了：

> "`to_console=False` is deliberate -- stdout is the WPF host's **IPC channel**
> (the `@@SMSWORKBENCH_V2@@` envelope plus the "Saved session:" marker), and a
> StreamHandler would inject formatter-decorated lines into that contract."

推论链：

1. logger **不写 stdout**（`to_console=False`），只写 `runtime/logs/sms_tool.log`。
2. 所以 `print → logger` 会让这些行**从 stdout 完全消失**。
3. C# 面板本来就把它们过滤掉（`NoiseLinePrefixes`）→ 面板**无变化**。
4. **但 CLI 用户会失去进度输出** —— 这是真实的用户可见变化，不是内部重构。

**另一个反直觉的点**：C# 是按**行前缀**匹配的。如果 logger 也写 stdout，行首会多出
formatter 装饰（时间戳/级别），前缀匹配全部失效 → 这些行会**重新出现在面板上**，
黑名单失效但噪声回来。所以「改 print + 保留黑名单」比不改更糟。

**本轮因此没有改任何 print。**

### 24.3 意外发现：`sms_tool/` 里已经有正确的范式

`sms_tool/auth_flow.py:160-180` 的 `_print_protocol_diagnostic()` **已经实现了审计想要的形态**：

```python
if 200 <= status < 400:
    logger.debug(line.strip())   # 健康阶段 → 只进日志
    return
print(line)                      # 异常阶段 → 让运维看见
```

注释明写理由："Keep healthy stages on the debug log; print only when the stage did not
behave, which is when someone actually needs to read it."

**这说明「logger 覆盖率 12%」低估了实际质量** —— 覆盖率是按调用数算的，
而这里的设计是**按严重程度分流**，热的走 logger、异常的走 print。
真要做 #12，应该**推广这个模式**，不是无差别替换。

### 24.4 已落地：跨语言噪声前缀契约门禁

既然不能改 print，就退而求其次：**防止 C# 黑名单腐烂成死代码**。

`NoiseLinePrefixes`（13 条）是**Python 侧知识的副本** —— 每条存在都是因为某个 `print()`
会发出那个行首。当 Python 侧不再发它，C# 条目**静默变成死权重**：
一个永远匹配不到的过滤器，看起来和一个正常工作的过滤器一模一样。

`tests/test_backend_log_noise_contract.py`（**新增 3 条**）：
从 C# 提取 13 个前缀，从 `sms_tool/**.py` 提取所有 print 的字面行首，断言
**每个 C# 前缀都有 Python 发射点**。契约是**单向**的（反向不成立：不在黑名单里的
print 就是运维想看的行）。

**实测结果：13 个前缀全部有发射点，黑名单没有腐烂。** 负面结果，但证明了现状健康。

### 24.5 变异：一次抓到、两次我选错目标、一次抓到

| 变异体 | 结果 |
|---|---|
| **M1** C# 加虚构前缀 `"Ghost prefix["` | ✅ 红 1 条 |
| **M2** 改掉 `auth_flow.py:602` 的 `Existing account login:` | **存活 —— 变异目标选错** |
| **M2'** 移除 `auth_flow.py` 全部 `Existing account ` | **仍存活 —— 还是选错** |
| **M3** 提取正则 `\bprint` → `\bprinter`（提取器变空） | ✅ 红 2 条，**含非空断言** |

**M2 / M2' 连续两次存活，都是我的错，不是门禁的错**：
`Existing account ` 的发射点分布在**两个文件** —— `auth_flow.py`（566/602/636）
**和 `registration_handlers.py`（850/873）**。我只改了前者，后者仍在 →
门禁**正确地**判定前缀有效。

教训：**做变异前先 grep 清楚目标字符串的全部出现位置**。
「前缀有一条发射链就够」这个语义是对的，要打掉它必须打掉**所有**发射点。

### 24.6 未做 / 需要拍板

- **没有改任何 print**（理由见 24.2）。要不要推广 `_print_protocol_diagnostic`
  的「按严重程度分流」模式，是**产品决策**（影响 CLI 用户看到的输出）。
- 真要根治 #12，需要同时：Python 改 logger + C# 删黑名单 + 接受 CLI 失去进度输出。
  三件事一起做，且 **C# 改动无法编译验证**。
- 门禁只覆盖 `sms_tool/`；`scripts/`、`services/` 的 print 不在 C# 黑名单的语义范围内。

---

## 25. P2-19 批次锁泄漏根因：不是「异常路径没释放」，是「release 了也不删」（2026-09-09）

### 25.1 结论先行

P2-16 交付 retention CLI 时留下一个没查的根因：`runtime/payment_batch_locks/gates/`
下 3,949 个 0 字节 `slot-0.lock`。**根因不是异常路径漏了 release，而是 `release()`
从来就只解锁、不删文件**；两个调用方又用**无界唯一 ID** 当 gate name，于是每跑一次
就永久新增一个空文件 + 一个空目录。

对照组直接证明这是**调用方**的问题而不是组件的问题：`registration_concurrency`
的 gate name 是有限阶段名，`runtime/gates/` **只有 3 个目录**，十几年也不会涨。

### 25.2 证据链

| 事实 | 位置 / 数据 |
|---|---|
| `release()` 只 unlock + close，不删任何东西 | `sms_tool/cross_process_gate.py:107-115` |
| 每个 name 建一个专属目录 | `cross_process_gate.py:55-56`：`Path(base_dir)/"gates"/_safe_name(name)` + `mkdir` |
| 批次 gate name 无界 | `sms_tool/payment_batch.py:148` `f"payment-batch-{batch_id}"` |
| 幂等 gate name 无界 | `sms_tool/payment_operation.py:126` `f"payment-operation-{key_hash}"`（sha256） |
| 全仓**零处**清理 `gates/` | grep `payment_batch_locks` 的生产引用只有 `payment_batch.py:150` 一处 |
| 存量 | `payment_batch_locks/gates` **3,949** 目录 · `payment_operations/gates` **806** 目录 |
| 对照组 | `runtime/gates` 只有 **3** 个目录（`registration_<stage>`，name 有界） |

目录名自带日期，时间分布显示是**持续累积**不是一次性爆发：08-19 起每天几十到几百，
09-03 后停止（对应批次不再跑）。

### 25.3 为什么不能把 name 改成有界

第一反应是「把 `payment-batch-{batch_id}` 换成 `payment-batch-{method}`」—— **不能**。

`--payment-batch-id` 可由外部指定：`cli.py:306` 定义该参数，
`SmsWorkbench/PaymentBatchService.cs:300` 启动后端时传 `request.BatchId`，
`services/protocol-payment/README.md:56` 明写「reusing the same `--payment-batch-id`
resumes the atomic checkpoint」。即**同一个 batch_id 会跨进程复用**，gate 的语义正是
「同 id 并发互斥」。改成 method 级会让两个**不同**批次互相阻塞 —— 那是行为变更，不是修复。

### 25.4 落地：`discard()` + 目录重建 + 调用点白名单

1. `CrossProcessSemaphore.discard()`（`cross_process_gate.py:117-150`）—— release 之后
   调用，best-effort `unlink` slot 文件 + `rmdir` 目录。失败静默：Windows 上若还有
   open handle，OS 会拒绝 unlink；残留空文件由 retention CLI 兜底。
2. `_try_acquire_any_slot()`（`:92`）—— **必须配套**。目录可能被并发 discard 掉，
   而 `__init__` 才是建目录的地方；一个在 discard 之前就构造好的等待者会**永远轮询**。
   现在 open 失败时先重建目录再重试。
3. 调用点三处：`payment_batch.py:558`（`finally`，只在 acquire 成功后可达 —— 超时分支
   直接 raise，不会走到）、`payment_operation.py:108`（`close()`）、`:178`（`begin()`
   的异常分支；这条路径不返回 `PaymentOperation`，`close()` 不会跑）。

**为什么不敢只给通用组件加个自毁方法就完事**：`discard()` 用在**仍被持有**的 gate 或
**复用型** gate 上会破坏互斥 —— 目录被删后新来者创建全新 inode 并上锁，而旧持有者还握着
旧 inode 的锁，两个进程同时持有同一 slot。所以正确性依赖「只在 release 之后调用，
且 name 是一次性的」这条**调用点契约**，必须用测试钉住。

### 25.5 门禁：把调用点契约变成测试

`tests/test_cross_process_gate.py`（**新增 4 条**）：

- discard 删掉 slot 文件与目录（配「删除前必须存在」的**哨兵断言**）
- 幂等，且未 acquire 也能调
- **目录被 discard 掉之后等待者仍能 acquire**，且重建出来的 slot 仍排斥第三方
- **AST 白名单**：`sms_tool/` 下所有 `X.discard()` 调用点必须恰等于
  `{payment_batch.py:process_gate, payment_operation.py:gate}`，另豁免
  `proxy_pool.py` 的 `set.discard()`。给复用型 gate（`registration_<stage>`）
  加 discard 会当场红。

retention 侧同步补两条（`tests/test_runtime_retention.py`）：新规则的样例
（`test_every_rule_matches_something` 要求**每条规则都能命中点什么**，否则是死规则），
以及 `test_operation_gate_rule_spares_the_idempotency_record` —— 断言规则**只匹配
`*.lock`**，绝不碰 `payment_operations/<hash>.json`。那条 `.json` 是决定「重试能否重放」
的幂等记录，按年龄回收它会让重试静默变得不安全。

### 25.6 变异：5 个全部被抓

| 变异体 | 结果 |
|---|---|
| **M1** `_try_acquire_any_slot` 的重建 `mkdir` 换成无副作用表达式 | ✅ 红 2 条 |
| **M2** `discard()` 去掉 `rmdir` | ✅ 红 2 条 |
| **M3** `discard()` 去掉 `unlink` 循环 | ✅ 红 2 条 |
| **M4** `payment_batch.py` 去掉 `discard()` | ✅ 红 1 条（白名单「缺失」方向） |
| **M5** `registration_concurrency.py` 给复用型 gate 加 `discard()` | ✅ 红 1 条（白名单「多余」方向） |
| **M6** retention 新规则的 `match_name` 从 `*.lock` 改成 `None` | ✅ 红 1 条（幂等记录会被误回收） |

M4 / M5 两个方向都验过，白名单才是真防住误用 —— 只验一个方向，另一个方向的漏洞
和「门禁正常工作」长得一模一样。

### 25.7 一个真实的门禁冲突（值得记）

`discard()` 的 docstring 原本写「残留由 `scripts/runtime_retention.py` 回收」，结果
`tests/test_runtime_retention.py::SafetyRails::test_no_production_module_imports_it`
当场红 —— 该门禁扫描 `sms_tool/` 全文**禁止出现 retention 工具名**，防止它溜进热路径。
**门禁是对的，是我的注释踩线**。改成「operator-facing retention CLI」，并在注释里写明
「别在这里点名那个脚本」。

### 25.8 未做 / 存量

- **存量 3,949 + 806 个目录没清**。修复只保证**不再增长**；清理要跑
  `scripts/runtime_retention.py --apply`（待拍板，见 §23）。
- `runtime/payment_operations/gates/` 已补进 retention 规则（`match_dir=
  "payment_operations"` + `match_name="*.lock"`，绝不碰旁边的 `<hash>.json`
  幂等记录）。**但当前 0 候选**：这 806 个锁最老只有 5.9 天（中位 3.3 天），
  还没到 7 天门槛 —— 它们仍在活跃创建，不是死文件。对比批次锁最老 21 天
  （09-03 之后批次没再跑）。要做负向断言把这个边界钉住，见 25.5。
- 「补了规则却 0 候选」很容易被误判成规则写错。判据是**年龄分布**，不是候选数。
- 4 个 `payment-batch-resume` / `-retry` / `-same-id` / `-terminal` 目录（mtime
  2026-08-19）在代码里**已 grep 不到**，是旧测试用真实 runtime 跑 gate 留下的残留。
  现在 `conftest.isolated_runtime` 把整棵 runtime 重定向到沙箱，不会再产生。
