# 项目深度审计报告 — 待清理 / 待优化清单

- 审计基线：`ec28985`（2026-08-31），519 个跟踪文件
- 审计范围：`sms_tool/`（179 模块）、`services/`、`SmsWorkbench/`、`SmsWorkbench.Contracts/`、`scripts/`、`docs/`、`.github/`、`tests/`
- 方法：AST 全量解析（2784 个定义）+ import 图环检测 + 交叉 grep 复核 + 逐文件阅读
- 已排除：`dist/`（466 文件）、`runtime/_filter_repo_work/`（677 文件）—— 两者是源码副本，会污染所有 grep

> 严重度说明：**P0** = 功能性 bug 或安全暴露，应立即处理；**P1** = 架构债，影响可维护性；**P2** = 清理项，成本可控。

---

## 执行状态（2026-08-31 下午）

第一批 5 项**已全部完成并验证**，工作区净减 299 行：

| # | 项 | 状态 | 验证 |
|---|---|---|---|
| 1 | 删凭据前缀脚本 + 修 `.gitignore` | ✅ | 文件已删；`git check-ignore` 对新文件 `scripts/_diag_newtest.py` 命中 `.gitignore:36` |
| 2 | 修 `--update-proxy-country` 传值 | ✅ | 新增 2 个防回归测试；2 个断言旧行为的测试已同步修正 |
| 3 | 修 `ConfigStore` 空桶不删文件 | ✅ | 新增 `ConfigStoreTests` 3 个用例（含"删光分片后不复活"） |
| 4 | `sensitive_policy.json` 补 `license_key` | ✅ | 实测 `license_key`/`licenseKey`/`cloak_license_key` 均脱敏，`license_type`/`license_status` 不受影响 |
| 5 | 删 C# 死代码 | ✅ | 删 1 个文件 + 13 个死方法 + 1 个转换器；残留引用 grep 为空 |

**测试结果**：`dotnet test -c Release` = **225 passed**（基线 220，+5 新增）。
**门禁**：`precommit_guard.py --all` = clean（517 个跟踪文件）。

### 两个值得记一笔的发现

**① 修 bug 时发现"传错值比不传更糟"。** Python 侧 `stage_country_overrides`（`commands/payment.py:247-267`）
是「配置值打底 + CLI 值覆盖」。配置里存着正确的 promotion 国家（C# `SaveProxyConfiguration` 写的
`stage_proxy_countries.promotion`），而 CLI 传下去的 approve 国家会**盖掉**它。
所以这个 bug 不是"传了个次优值"，是**主动破坏了一个本来正确的配置**。
修法也因此不能只改字段——update 国家的权威来源是**持久化配置**（`LoadProxyConfiguration` 已能解析），
UI 上没有对应输入项。已从配置读取并回退到 `PaymentMethods.DefaultUpdateCountry`。

**② 两个原有测试断言的正是 bug 行为。** `PaymentBatchServiceTests:65` 断言
`--update-proxy-country == "JP"`（approve 国家），`:97` 断言 `== "TR"`。
这类"测试锁定错误行为"的情况，修 bug 时往往比 bug 本身更难发现——建议以后修完 bug 顺手看一眼有没有测试在断言旧行为。

### 遗留（2026-08-31 下午已全部处置）

- **5 个 `scripts/_diag_*.py` 已 `git rm --cached`**：文件保留在磁盘（本地诊断仍可用），仅移出版本控制。
  顺带查出这 5 个**也全在远端 main**，所以 filter-repo 实际剔除的是 **6 个路径**（原先以为只有 1 个）。
- **历史已清**：filter-repo 剔除 6 个路径 + 强推，`main` → `6fc56c0`（173 → 171 提交）。
  两分支 + 26 tag 逐个核验 0 命中，7 个含凭据 blob 已从对象库彻底消失（`git cat-file -t` 逐个确认）。

**⚠️ 强推 ≠ 立即生效。** GitHub 的旧对象回收有延迟：强推 8 分钟后，旧 sha `ec28985`
在主仓和 fork 上**仍可取到**含凭据文件。同类先例 `fa70bd0`（8 天前剔除）现在主仓 + fork
全部 `No commit found`，说明最终会清掉。**需隔几天复查**：
```
gh api repos/2951461586/GPT-Register-Tool/commits/ec2898526556852fe09780c59c7afca044bd0cd1
```

**⚠️ 修正上午的一个判断。** 上午写「fork 快照 ≤08-22 不含此提交，只需清主仓」，隐含"fork 侧安全"。
实测 188 个 fork 最后 push 最晚 `2026-08-25`，但**凭主仓 commit sha 在 fork 上照样取得到文件**（5/5 命中）——
GitHub 的 fork 与主仓共享对象库。反过来，强推重写能清掉整个 network 的旧对象，所以**这轮清历史是有效的**。

执行中把仓库搞毁过一次并恢复，全过程与根因见 `.workbuddy-ai/memory/git-history-rewrite.md`。

---

## 一、P0：功能性 bug（会静默出错，不只是风格问题）

### 1.1 批量协议支付把 update 代理国家传成了 approve 国家

```
SmsWorkbench/PaymentBatchService.cs:307   AddCountryArgument(arguments, "--update-proxy-country", request.ApproveCountry);
SmsWorkbench/PaymentBatchService.cs:356   AddCountryArgument(arguments, "--update-proxy-country", approveCountry);
```

- Python 侧 `sms_tool/cli.py:319` 把 `--update-proxy-country` 映射到 `promotion_proxy_country`，
  由 `commands/payment.py:214/229/264` 用于 **promotion/update 代理轮换**，与 approve 是不同概念。
- 正确写法在同仓库已有对照：`ProtocolPaymentExecution.cs:76` 用的是 `request.UpdateCountry`。
- 根因：`PaymentBatchRequest`（`PaymentBatchModels.cs:100-117`）的字段列表里**根本没有 `UpdateCountry`**，
  只有 `CheckoutCountry` / `ApproveCountry`。UI 侧算出来的正确值（`PaymentBatchService.cs:254`
  `PromotionCountry = normalized == "gopay" ? "TH" : DefaultUpdateCountry(...)`）**从未进入 request**。
- 后果：gopay 场景代码明确要求 promotion 国家为 `TH` 而 approve 国家是 `ID`，
  实际传下去两个都是 `ID`。批量路径的代理轮换国家被静默替换，且无任何告警。

**修法**：给 `PaymentBatchRequest` 补 `UpdateCountry` 字段 → 构建处（`:254` 附近）传入 → `:307`/`:356` 改用它。

### 1.2 配置分片删不干净，已删除的键会复活

`SmsWorkbench/ConfigStore.cs:163`

```csharp
if (bucket.Value.Count == 0) continue;   // 空桶直接跳过，不删除盘上的旧分片文件
```

- `SettingsService.RemovePath`（`:114-118`）把某分片最后一个顶层键删掉后，该文件仍留在盘上。
- 下次 `ReadMerged`（`:96-102`）会把这个旧文件原样合并回来 → **幽灵配置复活**。
- 修法：空桶时删除对应分片文件（而非 `continue`）。

### 1.3 临时文件泄漏（4 条生产路径）

`BackendCommandPlan.TempFiles` 只在 2 处被清理：
`MainWindow.ContextMenu.cs:165`、`MainWindow.Tasks.cs:322`（仅 `DeleteSelectedAsync` 的 finally）。

泄漏路径：
- `MainWindow.Export.cs:36-39` `CreateAccountImport`
- `MainWindow.Register.cs:206-213` `CreateOneClickSms` —— **注释还写着 "Ensure temp files are cleaned up by the coordinator，但 `RunBackend` 只接收 `args`，根本拿不到 `plan.TempFiles`**
- `MainWindow.Register.cs:241-247` `CreateAccountScan`
- `MainWindow.Register.cs:263-267` `CreatePromotionCheck`

### 1.4 常驻进程每行泄漏一个 JsonDocument

`SmsWorkbench/DesktopReadClient.cs:370` `response = JsonDocument.Parse(line).RootElement;` —— 在 `ReadLoopAsync` 的每行循环里创建，**从不 Dispose**。对照同文件 `:129` 就有 `using JsonDocument document = ...`。常驻 IPC 进程下是持续泄漏。

---

## 二、P0：安全暴露

### 2.1 🔴 新增：3 个真实凭据前缀已推送到公开仓库

`scripts/pick_final_replacements.py:11` 硬编码了 3 个真实凭据的 6 位前缀（roxy token / 旧 smailr key / 代理账号口令）。
该文件随 `ee02fab`（2026-08-31）入库，**已推送到 GitHub 公开主仓**——实测：
`gh api .../contents/scripts/pick_final_replacements.py?ref=main` 返回 886 字节。

**这条推翻了 2026-08-31 上午的评估结论。** 当时的结论"8 个分片凭据从未上过 GitHub"成立，
依据是该批提交落在 08-22 → 08-30 的 push 空窗期；但 `ee02fab` 是 08-31 新提交且**已 push**，不在这个空窗里。

危害分级：**是"凭据指纹"而非凭据本体** —— 6 位前缀不足以还原，但等于在 408 stars / 187 forks 的公开仓库里
指明"这三个服务在用什么形态的凭据"。且该文件自身已不可运行（读的 `runtime/_filter_repo_work/` 已删除）。

**处理建议（按成本递增）**：
1. 立即删脚本 + 补 `.gitignore` 规则（见 4.1）—— 防扩散，零风险
2. 是否跑 filter-repo 剔除历史由你定：fork 快照均 ≤ 08-22，**不含**此提交，故只需清主仓；
   但 filter-repo 在本仓有毁仓前科（见 MEMORY.md），需先 `git bundle` 锚点 + `CODEBUDDY_SAFE_DELETE_ENABLED=0`

`docs/security-exposure-assessment-2026-08-31.md:126-129` 的"待办"段落里**自己也写了部分明文**
（复用口令片段、`kyl23333.xyz` 邮箱域名），同样已 push。

### 2.2 门禁存在假阴性

`scripts/precommit_guard.py --all` 对 519 个跟踪文件返回 **clean / exit 0**，
但放过了 2.1 的凭据前缀 —— 规则对"短前缀常量"这一形态无检测能力。
`docs/security-exposure-assessment-2026-08-31.md:131-134` 两天前已指出此问题并建议改环境变量，**未执行**。

### 2.3 `sensitive_policy.json` 漏了 `license_key`

`runtime.json` 有 `registration.drivers.cloak.license_key`，但 `sensitive_policy.json` 的
`sensitive_keys`（35 项）与 `sensitive_key_fragments`（token/secret/password/card_number/cardnumber/card_last4/authorization）
**均不覆盖 `*_key` 形态**。CloakBrowser License Key 是商业授权凭据，填上即不被门禁拦截。
（`*_api_token` 类键已被 `token` 片段兜底，覆盖度整体尚可。）

---

## 三、P1：架构债

### 3.1 Python 侧 17 个 import 环（3 个高危）

| 环 | 证据 | 危害 |
|---|---|---|
| `pay_link` 6 个子模块全部回指父壳 | `pay_link/adapters.py:4` `import sms_tool.payment_link_manager as _plm`，随后 `_plm.subprocess.run`（:48）、`_plm.current_config_data()`（base:34）、`_plm._protocol_cfg()`（base:92,98） | 拆分名存实亡，共享状态仍在壳里 |
| 7 跳环跨 3 个子包 | `payment_link_manager` → `pay_link` → `adapters` → `gcash_transport` → `gen_pp_link` → `paypal_link` → `gen_link` → 回 `payment_link_manager`；`paypal_link/gen_link.py:19` 还有 `_PlmProxy.__getattr__` 运行时 import | 静态分析失效，重构即踩雷 |
| 分层倒置 | `store/connection.py:31` `import sms_tool.storage as _storage`，注释自陈"为让 `patch.object(storage, ...)` 生效" | **底层为迁就测试打桩反向依赖上层壳** |
| 存储层反向依赖业务 | `store/accounts.py:126` `from ..mailbox_remail import record_dead_remail_account` | 存储层不该知道邮箱业务 |
| 配置层反向依赖驱动层 | `config.py:146` 函数内 `from .registration_drivers.external_sessions import _driver_config` | 最底层模块依赖最上层 |

### 3.2 "假拆分"：壳留下了，状态也留下了

- `sms_tool/paypal_link/gen_link.py` **1255 行**独立实现，通过 `_PlmProxy.__getattr__` 懒代理访问壳。
- `sms_tool/pay_link/` 6 个文件各自 `import sms_tool.payment_link_manager as _plm` 取共享状态。
- `sms_tool/paypal_auto.py`（28 行）、`gen_pp_link.py`（7 行）、`storage.py`（8 行）是纯 re-export 壳，
  **仍各有真实引用**（`commands/payment_links.py:127,164,246`；`gcash_transport.py:143-146`、`payment_capability.py:69,122`、
  `paypal/orchestrator.py:16`；storage 有 20+ 处），不能直接删，需先改调用点。
- `sms_tool/payment_link_manager.py` 第 8–44 行 **37 行 import 整段未使用**（文件只剩 `from .pay_link import *`），可立即删。
- `sms_tool/registration.py` 278 行里约 125 行是 import，**96 个未使用**，是遗留聚合壳。

### 3.3 重复实现（机械拆分的后遗症）

| 符号 | 份数 | 分布 |
|---|---|---|
| `normalize_proxy_url` | **9** | blik:397、ideal:310、twint:303、kakao:160、momo:338、ac_paylink_core:260、direct_card:311、paypal_proxy:58、phone_proxy:103 |
| `_as_int` | 8 | account_identity:248、account_liveness:497、codex_export:508、codex_oauth:1150、payment_auth:174、store/normalize:18、sub2api_import:919、token_telemetry:38 |
| `_as_bool` | 7 | account_health_queue:408、mailbox_gmail:513、pay_link/base:53、payment_contracts:103、paypal_authorization:231、store/normalize:13、sub2api_import:926 |
| `new_session` / `proxy_for_country` | 4 / 4 | 各 34–42 行近乎逐字相同 |
| 邮箱行分类（C#） | 3 | `BackendCommandPlanner.cs:532` / `MainWindow.Register.cs:702` / `MainWindow.Export.cs:887` |

- `services/protocol-payment/` 五个提取器各写一遍同一套工具函数（**303 个函数名在 ≥2 个文件重复定义**）。
  `common/protocol_core.py` 只被 blik/ideal/twint 部分复用，**kakao 与 momo 完全未接入**。
  文件体量：blik 3795 / ideal 3200 / twint 3187 / momo 2046 / kakao 1493 / direct_card 1174。
- HTTP 双栈混用：`curl_cffi`（59 处）与 `requests`（71 处）在**同一文件内并存** —— `paypal_extract.py`（L28/L97）、
  `paypal_protocol.py`（L13/L19）、`paypal_reverse.py`（L20/L24）、`pp_link_helpers.py`（L21/L51）、
  `paypal_proxy.py`（L14/L179）、`phone_proxy.py`（L22）。
- C# 侧：4 套独立的 `new JsonSerializerOptions { WriteIndented = true }`
  （`ConfigStore.cs:56`、`PaymentBatchService.cs:34`、`SettingsService.cs:20`、`MainWindow.Export.cs:125,178`）；
  `ListSeparators` 常量定义 4 次；`AddPoolArgument`/`AddCountryArgument` 逐字重复两份。

### 3.4 C# 契约层被生产代码绕过（静默 bug 温床）

`BackendCommandPlanner` 三个方法**只被单测引用**，生产路径走的是 View 里的副本：

| 契约方法 | 生产实际用的 | 单测 |
|---|---|---|
| `CreateViewInbox:477` | `MainWindow.Inbox.cs:133-158` 手工拼 `--desktop-ipc --view-inbox ...` | `BackendCommandPlannerTests.cs:385` |
| `MailboxArgumentForLine:532` | `MainWindow.Register.cs:702` `MailboxArgForLine` | 同上 |
| `AppendSessionFile:569` | `MainWindow.Navigation.cs:15` `AddSessionFileArg` | 同上 |

后果：**契约层与生产行为一旦漂移，单测仍然全绿。**

另有 3 处同类重复：`MainWindow.Export.cs:1003` `AddImportTargetArg` 重复 `NormalizeImportTarget`（:402）；
`ProxyInputNormalizer.LineSeparator`（`"\n"`，Helpers:43）与 `BackendCommandPlanner.cs:565`（`Environment.NewLine`，Windows 为 `"\r\n"`）
产出**字节不同**的同一份 `--proxy-pool` 参数。

### 3.5 巨型结构

- **`MainWindow` 部分类 19 个文件约 7000 行**：Export 1029 / xaml 949 / Register 773 / Helpers 454 / Pools 410 /
  Detail 403 / Tasks 388 / xaml.cs 365 / SmsBower 280 / Inbox 334 / ContextMenu 243。
  `MainWindow.xaml.cs:3` 有约 63 个成员；`PoolRow`（:266-301）32 个属性混装数据行+展示文本+后端原始行。
- Python 超长函数 Top：`cli.py:200 main`（543 行 / 97 分支）、`payment_batch.py:80 run_payment_batch`（492 行 / **136 分支**）、
  `registration_drivers/playwright.py:1559 run_browser_registration`（457 行 / **嵌套 9 层**）、
  `upi_link.py:365`（87 分支）、`payment_routing.py:246 plan`（**85 分支**）。
- **分支 >15 的函数共 230 个**，嵌套 ≥7 的 8 个。
- `SmsWorkbench/MainWindow.xaml.cs` 有 9 参数 internal 构造函数（`:220-244`），唯一调用方是测试，生产路径 `protocolPaymentDialogs` 传 null。

### 3.6 异常处理

- **219 处 `except Exception: pass/continue`**（完全吞异常）：`playwright.py` 20 处、`paypal/dom_fields.py` 17 处、
  `captcha_solver.py` 13 处、`nodriver_paypal.py` 12 处、`external_sessions.py` 11 处。
- **623 处宽泛 `except Exception:`**。
- 9 处硬编码 `time.sleep(5)` 无退避无上限（`nodriver_captcha.py:71,103,148,164,193` 等）。
- 已确认干净：**0 处裸 `except:`、0 处 `os.system`、0 处 `subprocess(shell=True)`**。

### 3.7 其他运行时风险

- `MainWindow.Register.cs:757` `.GetAwaiter().GetResult()` 同步阻塞 —— 调用链上溯到
  `ExportAccountsTxt:75`（UI 线程同步 `File.WriteAllText`），**导出 TXT 时每个账号都可能卡 UI**。
- `MainWindow.Tasks.cs:17` `BackendTaskTimeoutMs = 12 小时`，唯一取消来源是用户点取消按钮，批次卡死无自动兜底。
- 硬编码 `http://127.0.0.1:7897` 复制 4 份（`MainWindow.xaml.cs:7`、`SettingsService.cs:18`、`SettingsCatalog.cs:150,152`）。
- 超时数字遍地硬编码：`120000` 出现 8 次、`900000` 2 次、`12*60*60*1000` 2 次。
  项目里已有正确范式（`PaymentBatchService.GetMethodTimeoutMilliseconds:371` 读 `protocol_payments.timeout_seconds`），只是没推广。

---

## 四、P2：可立即执行的清理

### 4.1 `.gitignore` 规则写漏

`.gitignore:33` 只写 `/_*.py`（限根目录），实际诊断脚本在 `scripts/_diag_*.py`，**不被任何规则匹配**
（`git check-ignore` 返回 NOT IGNORED）。现有 5 个 `_diag_*.py` 只是"已跟踪所以没丢"，
**下次新建诊断脚本会直接入库**。这正是 2.1 事故的路径。
另 `.gitignore:34` `scripts/diag_*.py` 是死规则（全仓无此命名）。

（好消息：`git ls-files -i -c --exclude-standard` 与 `git status --porcelain -unormal` **均为空**，
无冲突项也无漏网项；`runtime/` `dist/` `sessions/` `logs/` `.venv/` `__pycache__/` 均已正确忽略。）

### 4.2 死代码

**C#**
- `SmsWorkbench/AccountScanResultInterpreter.cs` **整个文件 204 行零引用** —— 已 grep 确认全仓仅命中定义行，
  无对应测试，且其 3 个方法与 `BackendResultInterpreter.cs:24/139/123` 完全重复。**可直接删。**
- `CollapsedLabelConverter` 双向死代码：`MainWindow.xaml.cs:329-341` + `MainWindow.xaml:19` 资源，无任何引用。
- `MainWindow.Export.cs` 13 个私有方法零引用：`:616 ScanStatusLabel`、`:694 ScanResultError`、`:707 TryExtractScanSummary`
  （调用点 :599/:600/:419 全走限定名 `BackendResultInterpreter.xxx`）、`:631`、`:636`、`:643`、`:656`、`:675`
  （后两个互相调用但无外部入口，整簇死）、`:713 BoolValue`、`:819`、`:834`、`:1003`、`:1021`。
- `App.xaml` 11 个资源键零引用：`Primary`、`SuccessSoft`、`SuccessBorder`、`SectionLabel`、`SidebarIconButton`、
  `PagerButton`、`AccentBlue/Green/Orange/Purple/Red`、`DialogWindow`。
  （反向检查通过：不存在引用未定义键的情况。）

**Python**（22 项全项目零引用，已排除 dunder / HTMLParser 框架回调 / 装饰器注册 / 导入即注册的适配器）
- 高：`registration.py:188 run_phone`、`sms_provider.py:11 SmsProviderResult`、
  `sentinel_quickjs.py`（整个模块入度=0，`sentinel_tokens.py:399` 是另一份本地实现）、
  `diagnostics.py:23,27 safe_exception/safe_command_display`、
  `sanitizer.py:80,105,106`（注释自称"used by adapters and tests"，实际零引用，注释已过时）
- 中：`captcha_solver.py:78 solve_captcha`、`auth_headers.py:480 auth_api_headers`、`account_2fa.py:358 totp_now`、
  `error_classification.py:112,116`、`payment_country_catalog.py:40`、`import_targets.py:142`、
  `omakse_client.py:590`、`mailbox_chongzhi.py:105`、`session_converter.py:19,23`、
  `paypal_fingerprints.py:18,23,24`、`smsbower.py:224,231,251,303`、`registration_handlers.py:875`、
  `providers/smailr_mailbox.py:186,238`、`providers/cfworker_mailbox.py:503,523`、`paypal_protocol.py:80`

### 4.3 依赖声明与实际不符

`requirements.txt` 13 项中：
- **`httpx[http2,socks]>=0.28.0` —— 0 处 import**（全仓 grep 为空）
- **`selenium>=4.20.0` —— 0 处 import**
- `playwright-stealth` 仅 1 处（可选分支，建议挪 extras）
- 未发现"用了但没声明"的第三方库

另：`sms_tool/` 下 **451 处未使用 import**（已排除 `from __future__` 假阳性），
其中 `registration.py` 96 处、`paypal_link/gen_link.py` 50 处、`payment_link_manager.py` 43 处（整段可删）。

### 4.4 一次性脚本入库

7 个 filter-repo 事故脚本（`cmp_index_wt.py`、`extract_history_secrets.py`、`filter_replacements.py`、
`fix_hardcoded_token.py`、`pick_final_replacements.py`、`scan_hardcoded_secrets.py`、`scan_sensitive_history.py`）
随 `ee02fab` 入库，+395 行，**无任何代码/文档/CI 引用**，输入目录 `runtime/_filter_repo_work/` 已消失，不可再运行。
其中 `fix_hardcoded_token.py:8`、`filter_replacements.py:12-13`、`pick_final_replacements.py:7-8` 还硬编码了本机绝对路径
`F:\epsoft\GPT-Register-Tool\...`。

应保留的（有真实引用）：`precommit_guard.py`（测试直接 import）、`preflight_env.py`（README + ci.yml:23）、
`architecture_scan.py` / `sensitive_field_scan.py`（ci.yml:30-31）、`install_git_hooks.py`、
`cleanup_invalid_accounts.py` / `mailbox_pool_orphans.py`。

`scripts/installer/` 干净：仅 4 个源文件入库，**无任何 .dll/.exe/.pdb/.zip 入库**（`git ls-files` 已确认）。

### 4.5 磁盘占用（1.4 GB 可评估）

```
runtime/      20825 files   1200.9 MB   ← 已 gitignore
  ├ camoufox_dl/        1 file    470.3 MB  (39.2%)   下载器缓存，可重装
  ├ browser_profiles/  3420 files  491.0 MB  (40.9%)  浏览器 profile 残留
  ├ relogin_backups/    695 files   83.5 MB  ( 7.0%)
  ├ accounts.sqlite3                 42.3 MB
  ├ accounts.sqlite3.pre_cleanup_20260823_162318  31.6 MB  ← 8 天前的清理前备份
  ├ deletion_backups/     4 files   25.6 MB
  ├ _filter_repo_work/  677 files   15.2 MB  ← filter-repo 工作目录，已废弃
  └ reference-abai/     310 files    6.1 MB  ← vendored 第三方参考仓库
dist/          466 files    196.0 MB   ← 构建产物
scripts/       327 files    137.2 MB
sessions/     1114 files     36.1 MB
```

`_filter_repo_work/`（15.2 MB）与 `accounts.sqlite3.pre_cleanup_*`（31.6 MB）是明确的过期产物。

### 4.6 根目录脚本已静默失效

- `verify_proxy.py:35` `with open("config.json")` 无分片回退，`except: return {}` ——
  分片迁移后取到空 dict，仍打印"配置来源: config.json"且**不报任何错误**。
- `start_proxy_pool.py:8` usage 示例仍是 `--config config.json`。

（应保留：根目录 `chatgpt_phone_reg.py` 是 6 行兼容壳，被 README 10 处命令引用；
`payment_methods.json` 被 `payment_catalog.py:90` 与 `PaymentMethods.cs:103` 实际读取。）

### 4.7 CI 工程化

- `ci.yml:17` 只跑 `python-version: "3.12"` 单版本，而 README 声明"Python 3.10 或更高"—— **3.10/3.11 从未被验证**。
- **无任何 lint / 格式检查**（无 ruff / flake8 / black / editorconfig 校验），而仓库有 451 处未使用 import。
- `ci.yml:25` `pytest --collect-only -q` 与 `:26` `pytest -q` 串行重复，前者无增量价值。
- `ci.yml:29` 单独跑 3 个测试文件，已被 `:26` 的全量覆盖（`pytest.ini` 的 `testpaths = tests` 生效），属历史补丁残留。
- `ci.yml:24` `compileall` 只覆盖 `sms_tool services`，漏了根目录 3 个脚本与 `scripts/` —— 恰是失效风险最高处。
- `config.example.json` 只有 18 个顶层键，三分片实际 22 个，**缺 `kakao`/`momo`/`omakse`/`paypal_nocard`**，
  而 `ci.yml:21` 正是用它构造测试配置 → 这 4 个支付方式的测试只能靠代码默认值兜底。

---

## 五、文档债（31 份 md，分片改造后大面积过时）

分片改造是 2026-08-30 做的，**文档几乎没跟上**：

| 文件 | 问题 |
|---|---|
| `docs/architecture.md` | `:37` 称配置模板是"复制到 config.json"；`:220,347,404,511` 4 处仍描述"解析单一 config.json"；`:1101-1103` 的"必须排除出 Git"清单**未列 `proxy.json`/`runtime.json`/`payment.json`** —— 而这三片才是真正装着代理口令与 API Key 的文件 |
| `PROXY_GUIDE.md` | 全文建立在单一 config.json 之上；`:75` 的 `python -m sms_tool.gen_pp_link --dry-run` 是**死命令**（gen_pp_link.py 现为 7 行壳，无 `__main__`，`grep dry.run paypal_link/*.py` 为空） |
| `docs/directory-map.md` | 对 `registration_drivers`、`paypal_link`、`pay_link`、`sms_tool/store`、三个分片文件的 grep 计数**全为 0** —— 末次提交 08-29，早于这些结构 |
| `docs/registration-and-proxy-architecture.md` | `:155,156,163` 引用 `config.json:650 / :216 / :293` 行号，单文件已不存在 |
| `docs/headless-browser-registration-audit.md` | `:38` 的会话类行号全失效，且仍列出**已删除的** `BrowserUseSession@644`、`SkyvernBrowserSession@700`、`RoxySeleniumSession@1054` |
| `docs/scan_headless_browser_proxy_fingerprint_2026-08-29.md` | `:36-40` 的行号**与上一条互相矛盾**（Cloak 361 vs 208、Camoufox 442 vs 305、Roxy 429 vs 479）；实测值 208/305/479/689 |
| `docs/README.md` | `:8` 索引停在 `release-v2026.08.22.md`，而已有 `release-v2026.08.31.md`；**8 份非发布类文档一条未收录** |
| `README.md` | `:91,106,116` 安装步骤仍是 `copy config.example.json config.json`；`:150` 称 API Key 写入 config.json（实际在 payment.json / runtime.json）；`:618,630` 发布检查清单只提 config.json → **发布前检查会漏掉真正装着凭据的三个新文件**；`:564` 命令混入 PowerShell 反引号续行符（bash 下会被当命令替换）；`:66` 要求 `curl_cffi==0.16.0` 而 `environment_preflight.ps1:57` 接受 0.15.x 或 0.16.x，口径不一致 |
| `README_EN.md` | 仅 83 行 = 中文版 12.8%，**缺 5 个整章**（项目架构/核心配置/常用操作/测试构建发布/数据安全）；`:66` 索引落后 3 个版本 |

**文档重复**：`headless-browser-registration-audit.md` 与 `scan_..._2026-08-29.md` 同为 08-29 的浏览器注册盘点，
结构重叠且结论冲突（前者列 browser_use/skyvern，后者没有）；`browser-registration-risk-control-gap-2026-08-30.md:101-117`
是第三次重复。`architecture.md:888-892` 与 `directory-map.md:59` 的 paypal 七层说明逐字重复。

**未过时（正向标注）**：17 份 `release-*.md` 按策略是每 tag 一份不可变记录，不属腐化；
`README.md` 出现的 CLI flag 与 `cli.py` 的 181 个 flag **无一项失配**，命令质量优于架构文档。

---

## 六、建议执行顺序

**第一批（今天，风险极低，收益明确）**
1. 删 `scripts/pick_final_replacements.py` + 补 `.gitignore` 的 `scripts/_*.py` 规则 + 删死规则 `scripts/diag_*.py`
2. 修 `PaymentBatchService.cs:307,356` 的 update 国家传值（补 `UpdateCountry` 字段）
3. 修 `ConfigStore.cs:163` 空桶不删文件
4. `sensitive_policy.json` 补 `license_key`
5. 删 `AccountScanResultInterpreter.cs`（204 行）+ `CollapsedLabelConverter` + `MainWindow.Export.cs` 13 个死方法

**第二批（本周）**
6. 补齐文档的分片改造同步 —— 优先 `architecture.md:1101-1103`（安全清单漏三片）与 `README.md:618,630`（发布检查清单）
7. 清 `payment_link_manager.py` 的 37 行死 import + `requirements.txt` 删 httpx/selenium
8. 修 `DesktopReadClient.cs:370` JsonDocument 泄漏 + 临时文件泄漏的 4 条路径
9. 删除 7 个已失效的 filter-repo 脚本 + 5 个 `_diag_*.py`

**第三批（需要设计决策，不建议贸然动）**
10. `pay_link` / `paypal_link` 的真拆分（把共享状态从壳里搬走，打破 7 跳环）
11. `services/protocol-payment/` 五提取器抽公共层（kakao/momo 接入 `common/protocol_core.py`）
12. `MainWindow` 19 文件 7000 行拆分
13. curl_cffi / requests 双栈统一

**需要你拍板的**
- `pick_final_replacements.py` 的历史是否跑 filter-repo 剔除（fork 快照 ≤08-22 不含该提交，只需清主仓；但本仓有毁仓前科）
- `runtime/` 1.2 GB 中 `camoufox_dl`（470 MB）与 `browser_profiles`（491 MB）是否可重建
