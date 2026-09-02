# 第六轮深度审计 — 可观测性 / 契约一致性 / 死代码清理

> 日期：2026-09-02 · 规模：Python 200 文件 / 80,258 行，C# 170 文件
> 方法：AST 全量扫描（`F:\tmp\audit6\py_scan.py`、`dead_only.py`、`dup_file.py`）+ 两路子代理取证 + 主 agent 逐条复核
>
> **落地记录见文末「八、第一批落地记录」**（P0 日志接线 + 4 项 P1，其中 1 项被实测推翻后改为加守卫）。

## 与前五轮的关系（本轮是补集）

前五轮已覆盖：**性能**、**正确性**、**解耦/import 环**、**Python 代码质量**（克隆函数/超大函数/硬编码 URL）、
**C#/WPF**、**测试安全**、**文档**、**脱敏策略**。本轮**不重复**这些。

本轮新开的 6 个轴：

| 轴 | 结论 |
|---|---|
| 可观测性（日志体系） | 🔴 **整个日志模块是死代码** |
| 契约一致性（Python↔C# 非 IPC 面） | 🟠 3 处易碎文本契约 + 2 份等价实现 |
| 死代码（模块级符号 / C# 成员） | 🟢 82 + 28 个可清 |
| 配置契约双向对账 | 🟠 51 个死键 / 34 个永远走默认 |
| 数据层事务与迁移 | 🟠 无 schema 版本、1 处跨事务写入 |
| 重复文件 / 线程池 | ✅ 均健康（含 2 条必须更正的旧判） |

---

## 零、最关键 6 条

### 1. 🔴 P0 `sms_tool/logging_setup.py` 全仓零引用 —— 日志基础设施从没接线

`sms_tool/logging_setup.py` 能力完整：`configure_logging()`（:33）、`RotatingFileHandler`
（5 MiB × 5 备份，:61）、写 `runtime/logs/sms_tool.log`（:28）、幂等（:50-52）。

**但它没有任何调用者。** 全仓 grep `configure_logging|logging_setup` 只有该文件自身 2 处命中。

后果（生产代码实测，排除 dist/runtime/tests）：

| 指标 | 数值 |
|---|---|
| `print(` | **745 处** |
| `logging` 引用 | 40 处 |
| `logger.exception` | **0 处** |
| 带 rotating handler 的持久化日志 | **0 个** |

**这不是历史包袱，是模块建好了没接。** 直接后果：异常**永远没有堆栈**——
第五轮报的「82 处 `str(e)` 进返回值伪装成功」之所以难查，根因就在这里：没有地方记录原始异常。

> 二选一：在 `cli.py` / `chatgpt_phone_reg.py` / `desktop serve` 入口调 `configure_logging()`；
> 或把文件删掉。**留着会让人误以为项目有日志。**

### 2. 🟠 P1 邮箱参数判定有两份等价实现 —— 测试保护的是没被用的那份

| 实现 | 生产调用 | 测试 |
|---|---|---|
| `SmsWorkbench/MainWindow.Register.cs:727` `private string MailboxArgForLine` | **7 处**（Register.cs:56,667,671,711,769 + Inbox.cs:142,146） | **0** |
| `SmsWorkbench.Contracts/BackendCommandPlanner.cs:532` `public static string MailboxArgumentForLine` | **0** | **6 个**（`tests/SmsWorkbench.Tests/BackendCommandPlannerTests.cs:433-466`） |

两份逻辑逐行等价（仅 `'\ufeff'` vs 字面 BOM、`new[]{"----"}` vs `"----"` 之差）。
现已可各自漂移，而 6 个测试守护的是**没人调用**的那份。

修复方向：生产改调 `BackendCommandPlanner.MailboxArgumentForLine`，删掉 private 那份。

### 3. 🟠 P1 UI 刷新靠匹配 Python 的自由文本（易碎契约）

```
SmsWorkbench/MainWindow.Tasks.cs:274   !line.Contains("Saved session:", StringComparison.OrdinalIgnoreCase)
sms_tool/commands/registration.py:409  print(f"[*] Saved session: {out_path}")
```

任何人改动这条日志文案，**热持久化刷新静默失效**，无任何报错、无版本校验。

> 注：另一条 IPC 通道（`@@SMSWORKBENCH_V2@@`）设计是**对的**——有 version + schema 双重校验
> 且拒绝降级（`BackendJsonProtocol.cs:36-42,56-64`）。问题只出在这条**游离于 IPC 之外**的文本匹配。

同类：C# `AccountStatusInterpreter.cs:265-288` 硬匹配 11 个英文错误子串，
Python `store/normalize.py:315-349` 有 17 个平行副本，**两侧无共享常量、无一致性测试**。

### 4. 🟠 P1 配置双向对账：51 个死键 / 34 个永远走默认

**定义了从不读（51）**，成规模的：
- `paypal.*` **24 个**：`auto_generate`、`link_mode`、`confirm_style`、`skip_route_load`、
  `redirect_poll_interval_seconds`、`max_regenerate_workers`、`fallback_to_hosted_checkout_on_blocked` …
- `omakse.default_*` 6 个、`paypal_nocard.*` 7 个、`protocol_payments.proxy_pools.*` 5 个、`upi.*` 5 个
- `runtime.python_path`（C# 用自己的 Python 路径设置，此项从未被读）

**读了从不定义（34）**：`sentinel_mode`、`post_registration_enabled`、`pool_size`、
`payment_method_country`、`max_approve_attempts`、`api_retries` …（永远走代码内默认值）

**校验死代码**：`sms_tool/config.py:476-494` 整段校验 `registration.stage_timeouts`
（13 个合法 stage 名 + 正数校验），但该键在 `config.json`/`proxy.json`/`runtime.json`/`payment.json`
中**都不存在**。同类 `config.py:438-441` 校验的 `registration.retry_attempts`/`retry_delay_seconds`
也只在 `config.example.json` 里有。

**example 漂移**：example 有而配置无 121 项、配置有而 example 无 108 项；
`kakao`/`momo`/`omakse`/`paypal_nocard` 四个整段在 example 中**完全缺失**。

### 5. 🟠 P1 数据层：无 schema 版本 + 1 处跨事务写入

- **无 `user_version`、无 migration 目录、无迁移记录。** 迁移 = 3 个 `CREATE TABLE IF NOT EXISTS`
  + `store/constants.py:6` 的 `EXTRA_COLUMNS` 27 项 ALTER TABLE 循环（`connection.py:231-235`）
  + 4 条全表回填 UPDATE（`:236-268`）。加字段可用，但**无法回滚、无法判断库是哪个版本**。
- **`upsert_account` 跨两个独立事务**（P1）：
  `store/accounts.py:136` 先调 `_resolve_account_email()` → 内部 `store/normalize.py:101`
  执行 `UPDATE accounts SET email=?` 并隐式提交 → 回到 `accounts.py:137-138` 才 INSERT + commit。
  若 137 抛异常，**email 改名已落库但账号没写入**，且不可逆。
  触发路径是 `@+` 别名修复（`existing != canonical` 时），概率低但静默。
  修法：用显式 `BEGIN IMMEDIATE` 把两步包进同一事务。
- **P2** `scripts/cleanup_invalid_accounts.py:83-90` 先 commit 删 DB 行再 `shutil.move` 挪文件，
  两步之间崩溃 → DB 已删、session 残留。

### 6. 🟠 P1 C# 侧 logText：O(n²) 拼接 + 无界增长

`SmsWorkbench/MainWindow.Helpers.cs:430` `logText += line;`、`:433` `LogText += line;`

`:428` 的注释说已优化 O(n²)，但那只解决了**重渲染**（改用 `LogTextBox.AppendText`）；
`logText += line` 本身的整串复制仍在，且 `logText` 全仓只有 `ClearLog_Click`（:405）一处清空，
**永无截断**。长批次跑下来内存与卡顿线性累积。

调用链：`Progress<T>` 回调 → `UiLog`（:436）→ `LogPresanitized`，`Progress<T>` 捕获 Dispatcher
同步上下文，所以每行输出都在 **UI 线程**上做一次整串复制。

定 P1 而非 P0：不至于卡死，是长批次累积卡顿。

---

## 一、死代码清理清单（本轮可直接删的部分）

### 1.1 Python 模块级零引用符号：82 个

剔除假阳性后约 **50 个真死**。假阳性两类，**不要删**：

- **HTMLParser 子类回调**（`handle_starttag`/`handle_endtag`/`handle_data`）：由基类调用。
  涉及 `providers/cfworker_mailbox.py:481-503`、`mailbox_icloud_url.py:210-264`、
  `paypal_link/reconciliation.py:189-207`。
- **驱动入口**（`run_camoufox_registration` / `run_cloak_registration` / `run_roxy_registration`）：
  经 `registration_drivers/base.py` 的 `DRIVERS` 注册表分发，非死代码。

真死清单（按文件聚合，数字为行号）：

| 文件 | 符号 |
|---|---|
| `sms_tool/diagnostics.py` | `:23 safe_exception`、`:27 safe_command_display` |
| `sms_tool/error_advice.py` | `:43 advice_for`、`:48 format_advice` |
| `sms_tool/error_classification.py` | `:112 is_account_failure`、`:116 is_network_failure` |
| `sms_tool/sanitizer.py` | `:107 sanitize_json`（脱敏模块唯一未接线的 API） |
| `sms_tool/smsbower.py` | `:224 get_balance`、`:231 get_services`、`:251 get_countries`、`:303 acquire_and_wait_code` |
| `sms_tool/cli.py` | `:827 _importable_account_rows`、`:900 _resolve_cli_payment_route` |
| `sms_tool/k12_client.py` | `:115 _delete_workspace_user`、`:230 _post_workspace_invite` |
| `sms_tool/k12_identity.py` | `:27 _extract_refresh_token`、`:52 _extract_id_token` |
| `sms_tool/mailbox.py` | `:480 _pick_mailbox`、`:496 _record_key` |
| `sms_tool/mailbox_smailr.py` | `:153 _smailr_enabled`、`:301 _latest_smailr_otp_candidate` |
| `providers/smailr_mailbox.py` | `:186 SmailrClient.mailbox_detail`、`:238 _mailbox_id` |
| `services/.../momo/ac_paylink_core.py` | `:317 extract_client_secret_fragment`、`:631 create_checkout_from_session_json`、`:746 parse_checkout_response` |
| `services/.../blik/blik_qr_extract.py` | `:2764 fetch_redirect_page`、`:3644 run_legacy_two_pool_mode` |
| `services/.../kakao/kakao_extract.py` | `:1328 terminal_checkout_shape_error`、`:1332 checkout_retry_error` |
| 单发 | `account_2fa.py:358 totp_now`、`account_health_queue.py:139 list_account_health_jobs`、`agent_identity.py:502 _agent_runtime_deleted`、`auth_headers.py:480 auth_api_headers`、`commands/helpers.py:118 one_click_sms_max_reuse`、`cpa_import.py:540 _normalize_domain_filter`、`env_loader.py:111 env_str`、`fingerprint_pool.py:72 apply_to`、`import_targets.py:142 fetch_target_auth_files`、`mailbox_chongzhi.py:105 parse_chongzhi_file`、`omakse_client.py:602 normalize_us_payment_result`、`payment_country_catalog.py:40 paypal_country_requires_validation`、`paypal_protocol.py:80 _session_cookies`、`paypal_proxy.py:89 retarget_proxy_country`、`sentinel_quickjs.py:257 get_sentinel_token_via_quickjs`、`session_converter.py:23 _is_obj`、`sms_provider.py:11 SmsProviderResult`、`pix_extract.py:209 ensure_pix_offered`、`mailbox_parsers.py:315 _parse_mailbox_password_file`、`auth_state.py:78 fetch_client_auth_session_dump`、`registration.py:188 run_phone` |

> `error_advice.py` / `error_classification.py` / `diagnostics.py` 三个模块整体接近未接线，
> **建议整模块评估**而不是逐函数删。

### 1.2 C# 未使用成员：28 个

- **生产零引用的 public/internal（8）**：`MainWindow.xaml.cs:114 SelectedTabIndex`、
  `:303 PoolRow.Phone`、`StageMatrixViewModel.cs:46 DomainLabel`、`:145 ClearHistory()`、
  `AccountStatusInterpreter.cs:56 GetQuotaStatus()`、`PaymentBatchModels.cs:58 IsValid()`、
  `ProxyInputNormalizer.cs:118 NormalizeListText()`
- **仅被测试引用、产品代码从不调用的契约成员（6）**：`BackendCommandPlanner.cs:321 CreateQuotaUsageProbe`、
  `:338 CreateDeleteAccount`、`:420 CreateSingleAccountImport`、`:463 CreateRefreshSession`、
  `:477 CreateViewInbox`、`:532 MailboxArgumentForLine`
- **死私有方法（14）**：`MainWindow.Payment.cs:13,16,17,32,48`（该文件 6 个方法死 5 个）、
  `MainWindow.Tasks.cs:88 RerunFailed_Click`（同时是 `async void`）、`:120 RebuildSqlite_Click`、
  `MainWindow.Register.cs:13 RegisterFromPool_Click`、`:182 AddRegistrationAtOnlyArgs`、
  `:364 ShowPaymentMethodDialog`、`MainWindow.Detail.cs:326 AddDetailRow`、
  `MainWindow.Helpers.cs:192 JsonValueToObject`、`SettingsViewModel.cs:34 OpenConfig`、
  `PaymentBatchViewModel.cs:164 OpenReport`
- **死参数**：`MainWindow.Helpers.cs:14 RunUiTaskAsync(Func<Task>, CancellationToken ct = default)`
  的 `ct` 方法体内从未使用，且全仓无调用方传值 → 未来传 token 会静默无效。
- **死字符串**：`AccountStatusInterpreter.cs:285` 与 `BackendResultInterpreter.cs:73`
  都匹配拼写错误的 `"account_deatived"`，而 Python 侧只发正确拼写 `account_deactivated`
  （`sms_tool/account_health.py:101` 等）→ **容错分支永不命中**。

### 1.3 主题样式双份真相

- `SmsWorkbench/App.xaml` 声明 **67 个 `x:Key`**；`MainWindow.Theme.cs` 用 **85 处 `SetBrush`**
  把同样的键、同样的十六进制值在 C# 里再写一遍（浅色分支 `:81-108`）。
- `MainWindow.xaml.cs:220` 在 `InitializeComponent()` 之后立刻调 `ApplyCustomThemeColors` →
  **改 App.xaml 的颜色会被当场覆盖回去，且无任何提示。**
- **同名 `FieldLabel` 样式两份且继承链不同**（全仓唯一重复 `x:Key`）：
  `App.xaml:53` 带 `BasedOn="{StaticResource {x:Type TextBlock}}"`，
  `ProtocolPaymentWindow.xaml:8` **缺少 BasedOn** → 该窗口字段标签不继承全局 TextBlock 基础样式，
  与 `PaymentBatchWindow` 渲染不一致（**已存在的视觉 bug**）。
- App.xaml 内同值重复画刷 8 组，其中 `#58595C` 被 8 个键共用（`TextMain`/`TextSub`/`TextMuted`
  + 5 个 `Badge*Fg`）—— 5 个 Badge 前景色语义已与文本色脱钩，宜拆。

### 1.4 `registration.py` 门面缺 `__all__`（**不能删，要补**）

`sms_tool/registration.py`（278 行，7 个 def，**89 个 re-export**）docstring 自述
"Public registration facade and compatibility exports"，把 8 个模块的符号（含大量**私有符号**
`_xxx`）搬过来再导出。

**它是活的，不能删**：3 个生产消费者（`account_recovery.py:290`、`cli.py:64`、`:646`）
+ 3 个测试文件。其中 `tests/test_phone_reuse_smsbower.py:801` 直接调
`registration._registration_requires_phone_verification(...)` —— 门面同时是测试调用面。

问题在于**没有 `__all__`**，工具无法区分「公共 API」与「内部转发」，
这是全仓「未使用导入 325 处」误报的主要来源（占比 89/325）。

> 修法：**只加 `__all__`**，不改导入。低风险、立刻让静态分析可用。

---

## 二、安全卫生（P2）

- **OTP 明文 print 4 处**：`mailbox.py:887`、`:908`、`mailbox_poll.py:76`、`mailbox_remail.py:865`
  （均为 `print(f" code:{otp_code}!")` 形态）。
  缓解：C# 侧展示/回传前会过 `SensitiveDataSanitizer.Redact`
  （`BackendResultInterpreter.cs:245`、`BackendTaskCoordinator.cs:71`、`MainWindow.Inbox.cs:201`），
  且该 sanitizer 从 `sensitive_policy.json` 的 `text_patterns` 动态加载，与 Python 共用同一份策略。
  **残留风险**：Python 侧 print 是明文落盘到 WPF 捕获的 `runtime/app_*.log`，
  C# 的 Redact 只覆盖 UI/回传路径，**不覆盖日志文件本身**。
- **凭据文件安全已复核**：`runtime.json` 里确有明文凭据（roxy api_token、remail api_key、
  smailr api_key、cfworker admin/api token、gmail 应用专用密码），
  但 `config.json`/`proxy.json`/`runtime.json`/`payment.json`/`sms_tool/config.json`
  **全部被 `.gitignore` 覆盖，`git ls-files` 均不跟踪** —— 未泄漏。

---

## 三、本轮必须更正的两条判断（更正自己，不是更正前五轮）

### ① 4 处「裸创建线程池」不是泄漏 —— 都有 `try/finally shutdown`

初判 `blik_qr_extract.py:3352`、`:3707`、`ideal_qr_extract.py:2966`、`twint_extract.py:2953`
4 处 `executor = ThreadPoolExecutor(...)` 不带 `with`，疑似线程泄漏。
**逐处核查后更正**：每处都有 `finally: executor.shutdown(wait=True, cancel_futures=...)`。
`batch_runner.py:131` 的 `prewarm_executor` 也有 3 处 `shutdown(wait=True)`（:240、:262、:274）。

全仓 22 处线程池创建 **100% 正确关闭**。

### ② `storage.sqlite_path` 的 Python/C# 分歧是**条件性**的，当前不触发

| 侧 | 空值回退 |
|---|---|
| Python `store/connection.py:79` → `paths.py:20` | `project_path(cfg["runtime"]["directory"], "runtime")` |
| C# `MainWindow.Helpers.cs:160` | `Path.Combine(rootDir, "runtime", "accounts.sqlite3")` 硬编码 |

分歧只在 **`sqlite_path` 为空** 且 **`runtime.directory` 被改成非 `runtime`** 时才成立。
实测 `runtime.json:176` 与 `config.json:495` 均显式配置
`"sqlite_path": "runtime/accounts.sqlite3"`，且 `runtime.directory = "runtime"`
——**当前两条路径解析结果一致**。

定 **P2 潜伏**：哪天有人清空 `sqlite_path` 就会 Python 与 WPF 操作两个库。
修法：C# 侧改读 `runtime.directory`（默认 `"runtime"`），补一个跨语言路径解析一致性测试。

---

## 四、验证过**没问题**的项（下一轮请直接跳过）

### 并发 / 资源
- 全仓 22 处 `ThreadPoolExecutor` **全部正确关闭**（见上文更正 ①）
- Python `MUTABLE_DEFAULT_ARGS = 0`
- 69 处锁定义分布合理，无裸 `Lock` 未保护状态的情况
- **零** `Dispatcher.Invoke`（同步版）；唯一 `MainWindow.Tasks.cs:289` 是 `BeginInvoke`
- **零** `Thread.Sleep`、零 `lock` 内 `await`、零 `.Result`/`.Wait()`
- MainWindow 19 个 partial 全部零 `ConfigureAwait(false)`，异步恢复都在 UI 线程

### WPF 内存泄漏面（全数通过）
- 41 处 `+=` 中 38 处是**短生命周期局部对象**（模态对话框按钮 `Click`、`dialog.Closed`）或自订阅
- `PasswordBoxBinding.cs:27-29` 先 `-=` 再 `+=`，处理器是**静态方法**，无捕获即无根
- `MainWindow.Sidebar.cs:41-45,64-75` 对静态 `CompositionTarget.Rendering` 的解绑逻辑完整（含 `ReferenceEquals` 幂等）
- **全仓零** `DispatcherTimer`、`SystemEvents`、`WeakEventManager`、`static event`
- 4 个 ViewModel **均不引用 View 控件**；C# 侧**无任何 SQLite 访问**（全走 Python）

### 数据层
- **SQL 注入面 = 0**：6 处拼接全是标识符/占位符，值拼接 0 处；
  唯一处理外部输入的 `_email_fuzzy_pattern`（`account_lifecycle.py:103-108`）同时做了转义 + 参数绑定
- `services/protocol-payment/**` 无任何 SQL
- 高频 `WHERE lower(email)=lower(?)` 全部命中 `idx_accounts_email_lower`；
  `ORDER BY updated_at DESC` 无 TEMP B-TREE
- `busy_timeout` 未显式设置 —— CPython 默认 5s 已生效（第四轮已论证，**不再重提**）

### IPC
- `@@SMSWORKBENCH_V2@@` 协议有 **version + schema 双重校验**且**拒绝降级**
  （`BackendJsonProtocol.cs:36-42,56-64`）
- 9 个 op 两侧一一对应；错误码双向映射完整；超时分层（看门狗 150s > 请求 120s > 握手 15s > 心跳 10s）合理
- `desktop_ipc.py:7` 事件路径经过 `sanitizer`
- Round-5 报的 `MainWindow.Register.cs:756` 硬等 120s **已修复**
  → 现为 `Register.cs:793-795` `await ... .ConfigureAwait(true)`，并留有说明注释

### 测试有效性（四项全部健康）
| 项 | 实测 |
|---|---|
| 断言密度 | 138 文件 / 1682 测试 / 4187 断言 = **2.49/测试**；密度 <1.0 仅 3 个文件，密度 0 的 0 个 |
| sleep 依赖 | 真实 `time.sleep` 仅 3 处，**合计 ≤ 0.52s** |
| 跳过测试 | 全仓**仅 1 处** `pytest.skip`（条件跳过，理由明确）；无 `@xfail`、无僵尸 skip |
| mock 过度 | 断言全落在 mock 上的测试函数仅 11 个，**全部断言 `call_args`**（契约验证），无裸 `assert_called_once` |

### 重复
- **近重复文件只有 1 组**：`ideal_qr_extract.py` ↔ `twint_qr_extract.py`（行集合 Jaccard **0.789**，
  1675 共同行）—— 即第五轮已知的那一组，本轮用独立方法复现
- **C# 近重复文件 0 组**（Jaccard ≥ 0.60 阈值下无命中）
- 自定义 `IValueConverter` 仅 2 个、附加属性仅 1 个，**无重复**

---

## 五、落地清单

### 第一批：纯机械、低风险

| # | 动作 | 收益 |
|---|---|---|
| 1 | **给 `configure_logging()` 接线**（cli.py / chatgpt_phone_reg.py / desktop serve 三处入口） | 拿到持久化日志与异常堆栈，这是后续所有排障的前提 |
| 2 | 生产改调 `BackendCommandPlanner.MailboxArgumentForLine`，删 `MainWindow.Register.cs:727` 私有版 | 消一份等价实现，6 个测试转为守护生产路径 |
| 3 | `"Saved session:"` 提为**共享常量**并加守护测试（或改走 IPC v2 事件） | 消一处静默失效 |
| 4 | C# 删 14 个死私有方法 + 8 个零引用 public + `RunUiTaskAsync` 死参数 + `account_deatived` 死分支 | 减噪声 |
| 5 | Python 删约 50 个真死模块级符号（优先整模块评估 `error_advice`/`error_classification`/`diagnostics`） | 减约 400 行 |
| 6 | `registration.py` **只加 `__all__`**（不改导入） | 让 325 处未使用导入误报可分辨 |
| 7 | `upsert_account` 用 `BEGIN IMMEDIATE` 包住改名+INSERT | 消除部分写入 |
| 8 | OTP 明文 print 4 处改脱敏输出 | 日志文件不再落明文 OTP |

### 第二批：需设计决策

1. **删 `config.py:476-494` 的 `stage_timeouts` 校验**，或把该段补进配置 + example（二选一）
2. **清理 51 个死配置键**，尤其 `paypal.*` 24 个 —— 需要业务判断「是废弃还是待接线」
3. **建 `tests/conftest.py`**，加 autouse fixture 调 `reset_database_init_cache()`，
   并给该函数补第一个测试（它目前 0 调用者，从未被验证能用）
4. **schema 版本化**：加 `PRAGMA user_version` + migration 记录（现在无法判断库是哪个版本）
5. **主题单一真相**：决定保留 App.xaml 还是 Theme.cs，另一份删掉；顺手修 `FieldLabel` 重复 `x:Key`
6. **错误 marker 子串跨语言共享常量** + 一致性测试

### 明确不值得做

- **`registration.py` 门面不能删**（3 生产 + 3 测试消费者，且是测试调用面）
- HTMLParser 回调、驱动注册表入口 —— 是**假阳性死代码**，删了会崩
- 线程池 —— 已全部正确，不要动
- `print` 总量 745 处 **不要批量改 logging**：先解决第 1 条（接线），
  再按模块逐步迁移；批量替换会让 C# 的 stdout 解析一起错
- 类型注解当前 78.4%，最差 25 个文件 0% —— **不建议为补注解而补**，
  优先补 `store/`、`registration_drivers/` 等核心路径

---

## 六、本轮方法学坑

1. **`difflib.SequenceMatcher.ratio()` 对 3000+ 行文件是 O(n²)，200 文件两两比对会跑 120s+ 超时被 SIGTERM。**
   改用**行集合 Jaccard**（`set` 交并比）后 3 秒出结果，且阈值 0.70 能稳定复现已知重复组。
   一次性脚本务必先跑小样本估时。
2. **`grep` 只看 `ThreadPoolExecutor(` 会漏掉 `try/finally shutdown`** —— 判"资源泄漏"必须读到
   函数末尾。本轮因此错判 4 处（已更正）。
3. **判"配置键死活"要双向**：定义了不读 ≠ 废弃（可能是文档键），读了不定义 ≠ bug（可能走默认值）。
   两边都要出清单，交给业务判断。
4. **子代理的 P0 定级要复核触发条件**：本轮子代理把 `sqlite_path` 分歧定为 P0，
   实测当前配置下**两条路径解析结果一致**，属潜伏问题而非活跃 bug，已降为 P2。

---

## 七、第一批落地记录（2026-09-02 当晚）

**验证：pytest `1876 passed / 0 failed / 6 skipped / 81 subtests`（基线 1858，净增 18 = 新增的
9+4+5 个用例）；`dotnet test` = `253 passed`；`dotnet build -c Release` = 0 错误 / 76 警告（与基线一致）。**

> 中途曾出现 2 红：一是日志测试的全局状态串扰，二是 `test_desktop_remail_registration.py`
> 的源码文本断言绑死了实现位置。两者均已修，见「落地中新发现的 3 个坑」。

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | **P0 `configure_logging()` 接线** | ✅ | `cli.py:main()` 三入口全覆盖；`to_console=False` |
| 2 | 合并 `MailboxArgForLine` 双份实现 | ✅ | 删私有版，7 处调用切到 `BackendCommandPlanner` |
| 3 | `"Saved session:"` 常量化 + 跨语言守卫 | ✅ | 新增 `BackendTextMarkers`；5 用例，变异验证通过 |
| 4 | OTP 明文 print 脱敏 | ✅ | 新增 `sanitizer.mask_otp()`；实为 **5 处**（报告写 4 处） |
| 5 | `registration.py` 加 `__all__` | ✅ | 43 项公共契约；生成时剔除 3 个假阳性 |
| 6 | `upsert_account` 事务 | ⛔ **实测推翻** | 改为加守卫，见下 |
| 7 | C# `logText` O(n²) + 无界 | ✅ | 改 `StringBuilder` + 1M 字符上限 |

### 改动明细

| 文件 | 内容 |
|---|---|
| `sms_tool/cli.py` | `main()` 中 `install_safe_stdio()` 后调 `configure_logging(to_console=False)` |
| `sms_tool/logging_setup.py` | 修 `_default_log_path()` 的 `runtime_file(cfg, filename)` 误用；`except OSError`→`except Exception` |
| `sms_tool/sanitizer.py` | 新增 `mask_otp(value, keep_tail=2)` |
| `sms_tool/mailbox.py` / `mailbox_poll.py` / `mailbox_remail.py` | 5 处 OTP print 改用 `mask_otp`（**返回值仍为明文**，仅输出遮蔽） |
| `sms_tool/registration.py` | 新增 43 项 `__all__` + 说明注释 |
| `sms_tool/commands/registration.py` | 新增 `SAVED_SESSION_MARKER` 常量，print 改用它 |
| `SmsWorkbench.Contracts/BackendTextMarkers.cs` | **新增**：自由文本 stdout 契约常量 |
| `SmsWorkbench.Contracts/BackendCommandPlanner.cs` | 未改动（已成为唯一实现） |
| `SmsWorkbench/MainWindow.Register.cs` | 删私有 `MailboxArgForLine`，5 处调用改共享版 |
| `SmsWorkbench/MainWindow.Inbox.cs` | 2 处调用改共享版 |
| `SmsWorkbench/MainWindow.Tasks.cs` | `Contains(BackendTextMarkers.SavedSession)` |
| `SmsWorkbench/MainWindow.xaml.cs` | `logText` → `StringBuilder _logBuffer` + `AppendLogLine()` + 1M 上限 |
| `SmsWorkbench/MainWindow.Helpers.cs` | `logText += line` → `AppendLogLine(line)` |
| `tests/test_logging_setup.py` | **新增** 9 用例 |
| `tests/test_store_transaction.py` | **新增** 4 用例 |
| `tests/test_backend_text_markers.py` | **新增** 5 用例 + 4 subtests |
| `tests/test_desktop_remail_registration.py` | 断言改指 `BackendCommandPlanner.cs`（原断言绑死实现位置） |

---

## 🔴 接线后立刻暴露的 bug：`logging_setup` 把 config 参数当目录传

`sms_tool/logging_setup.py:28` 原文：

```python
return runtime_file("logs", "sms_tool.log")
```

而 `paths.runtime_file(cfg, filename)` 的**第一个参数是 config，不是目录**。传 `"logs"` 进去 →
`cfg.get(...)` 抛 `AttributeError` → 被 `except Exception` 吞掉 → 静默降级到仓库根的 `logs/`。

**它从未暴露，正因为 `configure_logging()` 从未被调用。** 接线后实测立刻看到
`logs/sms_tool.log` 生成在仓库根（而非 `runtime/logs/`）。

已修：改用 `runtime_dir(current_config_data()) / "logs"`。
（`logs/` 虽被 `.gitignore:79` 覆盖、无泄漏风险，但位置是错的。）

> 教训：**"死代码"掩盖了它自身的 bug。判定一个模块"没接线"时，要预期它里面的错误也从未被执行过。**

---

## ⛔ 实测推翻：`upsert_account` 并不跨事务

报告零节第 5 条称 `upsert_account` 的改名 UPDATE 与 INSERT 分属两个事务，
"中途失败会产生改名成功但没写入"。**实测证伪，该项不改代码，改为加守卫。**

证据（`F:\tmp\audit6\tx_probe.py`，复现自真实 `_connect()`）：

```
isolation_level = ''                       # 默认隐式事务模式
after failed rename+insert, rows = [{'email': 'old+tag@example.com', ...}]
>>> RESULT: RENAME ROLLED BACK -> single transaction (report is WRONG)
autocommit before DML: False
autocommit after  DML: True                # DML 自动开启事务
```

机理：`store/connection.py:93` 用 `sqlite3.connect(str(db_path))`，**未传 `isolation_level`**，
即默认的 `''`（隐式 BEGIN）。第一条 DML 开启事务，`commit()` 才结束。
`_resolve_account_email`（`store/normalize.py:101`）的 UPDATE 与 `accounts.py:137` 的 INSERT
共用**同一连接、同一事务**，中间没有 `commit()`。

**真正的风险不是"现在有 bug"，而是"有人把连接改成 autocommit"。** 一旦有人给
`sqlite3.connect` 加上 `isolation_level=None`，这两条语句就会变成两个事务，
报告描述的故障立刻成真。所以加了 `tests/test_store_transaction.py`（4 用例）锁住这个语义，
变异测试（`isolation_level=None`）确认守卫能抓到。

---

## 落地中新发现的 3 个坑

1. **新增全局状态会跨测试串扰**。`configure_logging` 用模块级 `_CONFIGURED` 做幂等，
   接线后任何经过 `cli.main()` 的测试都会把它置 `True`，导致后跑的日志测试被短路
   ——**单独跑绿、全量跑红**。修法：在 `setUp` 里显式重置 `_CONFIGURED = False`，
   且断言 handler 时只数**本次新增**（用 `id` 差集），不要数绝对值
   （root logger 上还挂着 pytest 自己的 handler）。
2. **源码文本断言会绑死实现位置**。`tests/test_desktop_remail_registration.py:78`
   断言 `MainWindow.Register.cs` 含 `value.StartsWith("remail://"` —— 实现搬到
   `BackendCommandPlanner.cs` 后这条必然红。已改为断言新家 + 断言"旧处无私有副本"。
3. **AST 生成的 `__all__` 会混入函数内延迟 import**。`RegistrationEmailWorkflow` /
   `run_batch_impl` / `run_browser_registration` 三个名字在模块级并不存在
   （是函数体内的 `import`），脚本按 `ast.walk` 全量收集时误判。
   已用 `hasattr(module, name)` 逐项校验后剔除。

---

## 九、第二批落地记录：死代码清理 + 第二批 6 项（2026-09-02 深夜）

**验证：pytest `1891 passed / 0 failed / 6 skipped / 83 subtests`（上一批 1876，净增 15 = 新增的
6+4+5 个用例）；`dotnet test` = `253 passed`；`dotnet build -c Release` = 0 错误 / 76 警告（与基线一致）。**

### 9.1 C# 死代码：删 24 个（21 个扫描命中 + 3 个级联）

自己的 `cs_dead.py` 重扫（**不是照抄子代理**），按「生产引用 0 ∩ XAML 引用 0」取候选，再逐个人工甄别：

| 文件 | 删除 |
|---|---|
| `AccountStatusInterpreter.cs` | `GetQuotaStatus` → 级联 `ExtractWhamUsage`、`FormatWhamUsageLabel`、`FmtTokenCount`（共 4） |
| `MainWindow.Helpers.cs` | `JsonTextToObject`、`JsonDocumentToObject`、`JsonValueToObject`（3） |
| `MainWindow.Payment.cs` | `OpenSessions_Click`、`OpenDatabase_Click`、`OpenMailboxPool_Click`、`AtExtractBaLink_Click`、`ShowProtocolPaymentDialog`（5） |
| `MainWindow.Register.cs` | `RegisterFromPool_Click`、`AddRegistrationAtOnlyArgs`、`ShowPaymentMethodDialog`（3） |
| `MainWindow.Tasks.cs` | `RerunFailed_Click`（**同时是全文件唯一 `async void`**）、`RebuildSqlite_Click`（2） |
| `MainWindow.Detail.cs` | `AddDetailRow`（1） |
| `MainWindow.xaml.cs` | `SelectedTabIndex`（1） |
| `PaymentBatchModels.cs` | `IsValid`（1） |
| `ProxyInputNormalizer.cs` | `NormalizeListText`（1） |
| `StageMatrixViewModel.cs` | `DomainLabel`、`ClearHistory`（2） |

**7 个 `*_Click` 处理器经 grep 确认 XAML 无订阅者** —— 菜单项早删了、handler 留下了。

#### 🔴 三类绝不能删的假阳性（本轮甄别出的模式）

1. **`[RelayCommand]` 方法**：XAML 绑定的是生成名 `OpenConfigCommand` / `OpenReportCommand`，
   grep 方法名 `OpenConfig` 命中 0。**我一度把 `PaymentBatchViewModel.OpenReport` 误判为死代码**，
   看到它上方的 `[RelayCommand(CanExecute = nameof(CanOpenReport))]` 才停下。
2. **MVVM Toolkit 的 `partial void On<X>Changed` 钩子**：`PaymentBatchViewModel` 6 个
   + `SettingsModels.OnValueChanged`，由源码生成器调用。
3. **附加属性访问器**：`PasswordBoxBinding.Get/SetBoundPassword`，XAML 写的是 `BoundPassword`。

**保留**：10 个 TESTONLY 的契约成员（BackendCommandPlanner 6 个等）、`PoolRow.Phone`（数据模型属性）。
`DragMove` 是正则误匹配（那是调用 `Window.DragMove()`，不是定义）。

### 9.2 Python 死代码：严格验证后删 38 个

`py_dead_verify.py` 三重判定（AST 引用 / 全仓文本 / `__all__` 导出 / 动态分发形状），
82 个候选 → **38 SAFE / 13 RISKY / 31 非候选**。`py_delete.py` 用 `ast.end_lineno` 精确定位删除。

已删：5 处 OTP 之外的死符号集中在 `diagnostics`、`error_advice`、`error_classification`、
`smsbower`、`k12_*`、`mailbox_smailr`、`paypal_protocol`、`paypal_proxy`、`sentinel_quickjs`、
`session_converter`、`sms_provider`、`import_targets`、`cpa_import`、`mailbox_chongzhi`、
`omakse_client`、`payment_country_catalog`、`auth_headers`、`account_2fa`、`agent_identity`、
`fingerprint_pool`、`cli`（2）+ 渠道脚本（4）。

**13 个 RISKY 未动**，因为它们是「导入了但没用」而非「没人引用」——
`_parse_mailbox_password_file`、`one_click_sms_max_reuse`、`fetch_client_auth_session_dump`、
`run_phone` 等在 `cli.py` / `registration.py` 的门面里被 import。这属于**门面冗余导入**，
与死代码是不同问题，需要单独一轮。

### 9.3 第二批 6 项

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | `stage_timeouts` 校验 | ✅ **改判断** | 见下 |
| 2 | 51 个死配置键 | ⏸ **列清单，交老板拍板** | 见 9.4 |
| 3 | `conftest.py` + `reset_database_init_cache` | ✅ | 见下 |
| 4 | schema 版本化 | ✅ | `PRAGMA user_version` |
| 5 | 主题单一真相 | ✅ **改为防漂移** | 一致化 + 修 `FieldLabel` |
| 6 | 错误 marker 跨语言共享 | ✅ | 扩展到 `BackendTextMarkers` |

**① `stage_timeouts` 不是死代码 —— 我改了判断。** 报告说"校验是死代码"，但读代码发现：
`registration.get("stage_timeouts", {})` 返回 `{}` → 走 `elif isinstance({}, Mapping)` → **分支确实执行**，
只是集合为空不产出 error。它是**受支持但未文档化**的能力。
所以正确处置是**补文档而非删代码**：`config.example.json` 加空 `stage_timeouts` 对象，
`config.py:476` 加注释说明这不是死分支。
（中途我一度想在 JSON 里加 `_stage_timeouts_note` 字段说明，随即撤回 —— 那会制造第 52 个死键。）

**② conftest.py 刻意不用 autouse 重置 DB 缓存。** `init_database()` 记忆化是 294× 的性能win
（97.4ms→0.33ms），每个测试重置等于把收益还回去。而 memo 已按**路径**为 key，
测试把 `database_path` 指向临时文件时本就会新建条目、不会串扰。
所以：`isolated_database` 做成**按需** fixture；autouse 只留两件廉价的事 —— 恢复 cwd、
清 `logging_setup._CONFIGURED`（修上一批发现的串扰根因）。
另外给 `reset_database_init_cache` 补了**它的头 4 个测试**（此前 0 调用者、从未被验证）。

**③ schema 版本化**：`store/constants.py` 新增 `SCHEMA_VERSION = 1`，
`init_database` 在 `commit()` 前写 `PRAGMA user_version`（写在事务内 → 失败的 init 不会留下版本号）。
它**不做迁移**，只让"这个库是哪个版本"可回答 —— 这是写真正 migration 的前提。

**④ 主题不删任何一份，改为防漂移。** App.xaml 是首帧必需（Theme.cs 运行时注入前要有值），
所以两份都得留。新增 `tests/test_theme_palette_consistency.py`（5 用例）断言：
重叠键在明暗任一调色板中必须一致、Theme.cs 不能设置 App.xaml 未声明的键、App.xaml 内无重复 key。
**顺带修了一个已存在的视觉 bug**：`FieldLabel` 在 App.xaml（有 `BasedOn`）与
`ProtocolPaymentWindow.xaml`（无）各定义一次，导致该窗口字段标签丢失全局 TextBlock 基础样式，
与 `PaymentBatchWindow` 渲染不一致。已删本地副本。

**⑤ 错误 marker**：Python 抽 `ACCOUNT_DEACTIVATED_MARKERS`(5) / `AT_INVALID_MARKERS`(12)，
C# 在 `BackendTextMarkers` 镜像 `AccountDeactivated`(5) / `AtInvalid`(7)，
`AccountStatusInterpreter.LooksAtInvalidError/LooksAccountDeactivatedError` 改用常量
（12 个 marker 的 OR，内容与顺序无关，行为等价）。测试断言双边相等，
并**锁定 `account_deatived` 这个 typo 必须保留** —— 旧版本写过它，磁盘上的 session 里还有。

### 9.4 配置死键：需要老板拍板（我没动）

`config_dead.py` 的证据很干脆：**51 个键里 50 个，键名的叶子部分在代码中出现 0 次**
（唯一例外 `upi.link_mode`，`link_mode` 命中 5 处 —— 那是 `paypal.link_mode` 的同类实现）。

这说明它们不是"读了走默认值"，而是**代码侧从未实现过**。分成两组：

| 组 | 键数 | 判断 |
|---|---|---|
| `paypal.*` 24 / `omakse.default_*` 6 / `paypal_nocard.*` 7 | 37 | PayPal 资金路径的开关，多数带 `fallback_*`、`skip_*`、`*_seconds` 形态 —— 像**规划中的降级策略**，代码没跟上 |
| `protocol_payments.proxy_pools.*` 5 / `upi.*` 5 | 10 | 与 `paypal.*` 高度同构（`auto_generate`/`link_mode`/`redirect_url_format`/`use_elements_session`/`approve_missing_redirect` 五个键名在 paypal 与 upi 下**完全重复**）→ 像是**按渠道复制出来的模板配置** |
| `runtime.python_path` / `paypal_browser.email_mode` / `phone_reuse.smsbower.*` | 4 | 单发，`runtime.python_path` 明确被 C# 自己的设置取代 |

**为什么我不删**：这些键存在于你正在用的 `payment.json` / `runtime.json` 里。
删掉会让你已配的值无声失效；而"我设了这个但没生效"正是当前的实际状态 ——
**真正有用的不是删，是让它可见**。

建议（二选一，等你定）：
1. **`doctor` 命令新增一节**：列出"配置里写了但代码不读的键"，让你一眼看到哪些设置是空转的；
2. **把 51 个键固化成清单 + 测试**：一旦某个键被代码读取，测试就红，提醒把它移出清单。

### 9.5 本批新踩的 2 个坑

1. **我写的自动删除脚本有行号漂移**：`cs_delete.py` 用正则 `^\s{4,8}` 匹配成员，`\s` **会跨换行符**，
   导致匹配起点落在上一行、行号算错；且表达式体方法的结束判定只看当前行有无 `=>`
   （body 在下一行时会一路扫到文件尾）。dry-run 输出暴露了它（某方法 span 被算成 25 行）。
   → **放弃自动删除，改用精确文本 Edit**。教训：dry-run 必须逐条核对 span 行数，不能只看方法名。
2. **Windows 上 SQLite 句柄 + `TemporaryDirectory` 的顺序陷阱**又出现了两次：
   `with TemporaryDirectory()` 的 `__exit__` 先于 `addCleanup(conn.close)` 执行 → `PermissionError`。
   我的 `_SqliteCase` helper 用 `mkdtemp` + addCleanup（rmtree 先注册→最后跑）规避，
   **新写的测试必须复用这个 helper，不要自己写 `with TemporaryDirectory`**。

---

## 十、51 个死配置键的处置（2026-09-02 收尾）

### 结论：不删运行配置，改为「可复算的检测器 + doctor 可见 + 测试锁定」

| 动作 | 对象 | 数量 |
|---|---|---|
| **删** | `config.example.json` 里的死键（文档在宣传无效配置） | **35**（30 分片同源 + 5 example 独有） |
| **不动** | `config.json` / `proxy.json` / `runtime.json` / `payment.json` | **61**（老板的运行配置） |
| **新增** | `sms_tool/config_usage.py` 检测器 | 可复算，不靠快照 |
| **新增** | doctor 的 `config_unread_keys` 检查（warn，可注入） | 一行告警 + JSON 全量 |
| **新增** | `tests/test_config_usage.py`（7 用例，双向变异验证通过） | 清单漂移即红 |

### 为什么删 example 而不删分片

分片里是**你正在用的配置**，自动删除等于替你决定哪些设置不要了。
example 是**文档**，里面写着 35 个代码根本不读的键 —— 任何人照抄模板就会继承一批无效设置。
这两件事的风险完全不对等。

### 检测器的判定规则（关键：只看字符串字面量）

早期版本按「标识符出现次数」判定，`paypal.link_mode` / `upi.link_mode` 被误判为**已读**——
因为 `services/protocol-payment/*/run_single_link_mode()` 这个**函数名**里含 `link_mode`。
改为只认 **AST 里的字符串字面量**后，两者立刻显形为真死键。
另：`iter_leaf_paths` 会跳过键名本身含 `.` 的映射
（`email_registration.smailr.domain_ids."smailr.com"` 是**数据**不是配置键）。

### 实测口径修正

| 来源 | 数字 | 说明 |
|---|---|---|
| 子代理报告 | 51 个死键 | 只扫了部分分片 |
| 检测器实测 | **61 个**（分片）+ 5 个（example 独有） | 全量扫 `config.json`/`proxy.json`/`runtime.json`/`payment.json` |
| 子代理报告 | 「example 有而配置无 121 项」 | 实测 example 独有 **107** 项，其中真死仅 **5** 项 |

抽查确认真死（避免误伤有效配置）：
`chat_web_client_id`、`use_as_username` 代码里 0 命中；
`smailr.domains` 只有 `providers/smailr_mailbox.py:25` 的**硬编码默认值**，不读配置。

### 变异验证（双向）

| 变异 | 结果 |
|---|---|
| 在 `omakse_client.py` 加 `"default_concurrency"` 字面量（假装代码读了它） | ✅ 红：`NO LONGER dead (delete from EXPECTED_UNREAD)` |
| 往 `config.example.json` 加一个死键 | ✅ 红：`NEW dead keys` + example 断言双红 |

**第二轮变异一开始没被测出** —— 检测器只看分片、不看 example 独有键。
补上 `documented_only` 分支后才覆盖（这就是上面 5 个新发现的来源）。

### doctor 集成

`config_unread_keys` 做成**可注入 probe**（与其他检查一致），不是硬编码追加。
因为 doctor 的既有用例断言 `warned == 0`，硬编码会让它们全红且无法 stub。
已同步给 4 处 doctor 测试的 probes 字典加上该键。

```
[WARN] config_unread_keys: 61 key(s) set but never read: chatgpt.chat_web_client_id,
       email_registration.smailr.domains, email_registration.use_as_username, ...
         -> remove them or wire them up; run with --json for the full list
```

`--json` 输出 `unread_config_keys` 全量（含 path / shards / in_example）。

### 一个刻意的取舍：JSON 重写 vs 行级删除

先写了行级删除（想保住最小 diff），但 30 个键里 **16 个在 example 里是多行值**，
括号栈跟踪解析不出来。改用 `json.dump` 重写后 diff 变大（含原文件手工缩进的规范化，
实测 212 增 / 71 删，其中真实删除只有 30 行）。
**选了可靠性**：缩进规范化是一次性的附带改进，而解析失败的删除是功能性的错。
