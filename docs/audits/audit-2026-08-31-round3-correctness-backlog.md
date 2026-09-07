# 第三轮深度审计 — 正确性 / 安全出口 / 资源生命周期 / 交付工程

- 日期：2026-08-31
- 范围：`sms_tool/`（181 py）、`SmsWorkbench/`（63 cs + 5 xaml）、`SmsWorkbench.Contracts/`、`services/`、`scripts/`、`tests/`、`docs/`
- 排除：`dist/`、`runtime/`、`sessions/` 内容、`__pycache__/`、`*.pyc`、`.venv/`、`**/bin|obj/`（源码副本，不排除则结论全错）
- 定位：前两轮的**补集**。一轮（`cleanup-backlog`）查死代码/重复实现/契约漂移/文档路径；二轮（`round2-architecture`）查并发/配置耦合/幂等/测试有效性/CI。本轮查**发布出口、数据正确性、资源生命周期、交付工程**。
- 方法：三路并行 agent（Python / C# / 工程化）+ 主 agent 逐条 grep 复核。下文标 ✅ 的均为我亲自复核过的条目。

---

## 零、最关键的 3 条

### 1. 🔴 P0：已公开发布的 v2026.08.31 资产里带着被清理的凭据前缀 ✅

08-31 那轮 filter-repo 只清了 git 历史，**没清发布资产**。

- `dist/release/GPT-Register-Tool-win-x64-v2026.08.31.zip`（408 条目）内含 `scripts/pick_final_replacements.py`
- `dist/release/GPT-Register-Tool-Setup-v2026.08.31.exe`（54.2 MB）内嵌同一文件，字节偏移 13956444
- 同目录另含 5 个 `scripts/_diag_*.py`（camoufox_launch / camoufox_layers / camoufox_profiledir / remail_order / roxy_egress）

**已上线证据**（`gh api repos/2951461586/GPT-Register-Tool/releases`）：

| 资产 | 下载次数 |
|---|---|
| `Setup-v2026.08.31.exe` | 7 |
| `win-x64-v2026.08.31.zip` | 4 |
| `v2026.08.31.sha256.txt` | 1 |

泄漏内容：脚本第 10 行自述的 **roxy token / 旧 smailr key / 代理账号口令**三组，各 6 位前缀（本文件不写出值）。

**泄漏面边界（已实测，好消息）**：zip 与 exe 内 `kyl_state` / `mailbox_tokens` / 四个分片配置 / `sessions/` / `accounts.sqlite` / 真实 `.env` **全部为 0 条**。PII 面干净，只有这 3 个凭据前缀。

**时间线**：release 构建于 08-31 00:52–00:55；清理提交 `6fc56c0`（`.gitignore` 补 `scripts/_*.py`）在 **08-31 14:07** —— 清理晚 13 小时，**发布包从未重建**。

**根因不是"忘了"，是流程上没有闸门**：见第 2、3 条。

### 2. 🔴 P0：发布流程零凭据扫描 ✅

`scripts/build_installer.ps1` 全文 310 行，`scan_` / `secret` / `sensitive` / `guard` **命中 0 次**。
发布是本项目唯一不设防的出口 —— 而它偏偏是唯一会把内容送到几百个 fork 之外的出口。

### 3. 🔴 P0：CI 上两条凭据扫描是假阴性 ✅

⚠️ 修正一轮结论：一轮说"仓库自带 3 个扫描脚本一个都没接"——**不准**。CI（`.github/workflows/ci.yml:41-46`）接了 4 步。真问题是**其中两步永远不可能失败**：

- `scripts/scan_hardcoded_secrets.py:10` — `ROOT` 套了 **3 层** `os.path.dirname`，实测得 `F:\epsoft`（仓库根应是 2 层的 `F:\epsoft\GPT-Register-Tool`）。后果：`SCAN_DIRS` 里 `sms_tool`/`scripts`/`SmsWorkbench` 等全部 `os.path.isdir` 失败被跳过，只剩 `'.'` 去扫**同级无关目录**。且全文 `sys.exit`/`SystemExit`/`return 1` **命中 0 次** → **永远 exit 0**。
- `scripts/scan_sensitive_history.py` — `main()` 结尾 `return 0`，永不为门禁。且 `ci.yml:45` 传的 6 个路径里 `proxy/runtime/payment.json`、`mailbox_tokens.txt`、`kyl_state.json` 在 CI 上**从未存在**，只有 `config.json` 是 `Copy-Item config.example.json` 造出来的 → 实际只扫示例值。
- ✅ **唯一能当门禁用的是 `scripts/sensitive_field_scan.py`**（`if failures: return 1` + `raise SystemExit(main())`，97 行，覆盖 `sms_tool`/`services`/`SmsWorkbench` 源码 + `runtime/`、`logs/` 产物）。但它**不扫 `dist/installer/package/`**，接进发布流程前需先补一条产物路径。

---

## 一、P1：会静默出错

### 4. `config.json` 与分片并存时，对 `config.json` 的编辑静默失效 ✅

` sms_tool/config.py:127` `load_merged_config()`：

```python
if any(path.exists() for _, path in shard_paths):
    ...            # 只合并三个分片
    return merged  # ← 永不落到下面的 legacy 分支
legacy = _CONFIG_DIR / "config.json"
```

当前根目录 `config.json`（22 键 / 43,584 B）与三个分片（08-31 15:26）**同时存在**。用户改 `config.json` 永远不生效，且无任何提示。
**修法**：分片存在时也读 `config.json` 并告警「检测到遗留 config.json，已被分片覆盖」，或直接把迁移后的 `config.json` 改名 `config.json.migrated`。

### 5. 常驻进程并发启动无锁 → 孤儿 Python ✅

`SmsWorkbench/DesktopReadClient.cs:258` `GetOrStartResident()` 方法体 20 行**无 `lock`**：

```csharp
if (_resident != null && _resident.IsAlive) return _resident;
_resident?.Dispose();
_resident = ResidentChannel.Start(...);   // 两个并发调用各起一个
```

两个并发调用同时通过 `IsAlive` 检查 → 各 `Start()` 一个 → 后一个覆盖 `_resident` → **前一个 python 进程再无引用可 Kill，成为永久孤儿**（缺 JobObject，见第 6 条，进程回收完全靠 `App.OnExit` 级联）。
同文件的 `_gate`（:282）只服务 `ResidentChannel` 内部，管不到这里。

### 6. 常驻 Python 进程：无 JobObject、无心跳 ✅

`CreateJobObject` / `AssignProcessToJobObject` **全仓 0 命中**。回收链路是 `App.OnExit`（`App.xaml.cs:90`）→ `ResidentChannel.Dispose`（:458）→ `Kill(entireProcessTree: true)`（:465）。宿主进程被强杀/崩溃/`FailFast` 时**必然残留**。
健康检查只有 `IsAlive => !_closed && !_process.HasExited`（:302）——纯惰性，`HasExited` 发现不了"活着但卡死"。Python 死锁时只能等 `RequestAsync` 的 120s 超时（:352）才被动重建。

✅ 复核推翻：「端口冲突/僵尸端口」不成立 —— 全仓 `TcpClient`/`HttpListener` **0 命中**，`MainWindow.xaml.cs:7` 的 `127.0.0.1:7897` 是代理上游地址。两条 IPC 都是匿名管道，天然无端口占用。

### 7. 常驻进程 stdin 编码缺失，非 ASCII 输入必炸 ✅

`DesktopReadClient.cs:313-318` 五个字段里，Output/Error 都设了 UTF8，**唯独漏 `StandardInputEncoding`**：

```csharp
RedirectStandardInput = true,          // :313
StandardOutputEncoding = Encoding.UTF8, // :317
StandardErrorEncoding = Encoding.UTF8,  // :318
```

请求经 `_process.StandardInput.WriteLineAsync`（:343）写入，走系统默认 ANSI/OEM 代码页；而 Python 侧 `cli.py:400` 已 `reconfigure(encoding="utf-8")`。**账号邮箱/文件路径含中文或俄文时解码失败或直接 mojibake**。

### 8. 写盘失败仍推进 DB：磁盘与 SQLite 分叉 ✅

`sms_tool/account_scan.py:457-461`：

```python
try:
    Path(json_path).write_text(json.dumps(updated, ...), encoding="utf-8")
except Exception as exc:
    print(f"[!] Failed to update session JSON {json_path}: {exc}")
upsert_account(updated, json_path=json_path)   # ← 无条件执行
```

磁盘 session JSON 还是旧 refresh_token，SQLite 已写入新值，且 DB 里的 `json_path` **指向那个过期文件**。同构：`sms_tool/commands/one_click.py:182-187`。
这类「先写盘失败只 log」共 7 处，这两处危害最大 —— 不是丢更新，是**两个持久化层互相对不上且无任何标记**。

### 9. OTP 验证码明文打到 stdout，且脱敏策略根本不覆盖 ✅

- 明文 print 共 6 处：`sms_tool/mailbox.py:896,917`、`sms_tool/mailbox_poll.py:76`、`sms_tool/mailbox_remail.py:851,865`、`sms_tool/nodriver_paypal.py:294`、`sms_tool/paypal_reverse.py:164`
- `sensitive_policy.json` 的 `sensitive_keys`（42 项）**不含** `otp` / `sms_code` / `email` / `phone` / `proxy_password`；`sensitive_key_fragments` 仅 `token|secret|password|card_number|cardnumber|card_last4|authorization|license_key` —— **`otp`、`sms_code` 不匹配任何片段**
- `sanitizer.py:56` 只按 dict key + 7 条 text_patterns 工作，对已取出的标量无能为力

✅ 落盘日志侧是干净的：初判「`logs/roxynet/*.log` 含 refresh token 明文」经复核是 URL 路径误报（`/tunnel/report_proxy...`）。**推翻，落盘凭据泄漏不成立**。`logger.exception` 全库 0 处。

---

## 二、P2：架构债

### 10. 日志：名义统一、实际是空壳 ✅

- `sms_tool/logging_setup.py:33` `configure_logging()` **定义后 0 处调用**（全仓 grep 仅命中自身定义与 docstring）。`RotatingFileHandler`（`runtime/logs/sms_tool.log`，5 MiB×5）从未安装，`runtime/logs/` 从未生成。
- 16 个模块的 `logging.getLogger` 拿到的是**无 handler 的 root logger**，Python `lastResort` 只把 WARNING+ 裸打到 stderr → 全部 `logger.info/debug` **静默丢弃**。
- 量化：`print()` **732 处**（仅 38 处 `file=sys.stderr`），89/222 文件有 print；logger 调用 **63 处**。比值 **11.6 : 1**。
- `start_proxy_pool.py:67` 的 `logging.basicConfig` 是第二套独立配置。

✅ 复核降级：「print 污染 IPC 协议」从高危降为**中**。`desktop_read.py`/`desktop_serve.py` 自身 print 为 0；`logging.StreamHandler()` 无参**默认 `sys.stderr`**；C# 侧 `DesktopReadClient.cs:380` 对非 JSON 行 `continue`、`BackendJsonProtocol.cs:19` 按 `@@SMSWORKBENCH_V2@@` 前缀倒扫。防线有效。
但残留两个缺口：`desktop_ipc.py:73` 的 `emit_result` **缺 `flush=True`**（同文件 `emit_event` 有）→ 超时 `Kill(entireProcessTree)` 时未 flush 的信封随缓冲区丢失；`BackendJsonProtocol.cs:62-83` 的 `ExtractLegacyPayload` 会把 stdout 上的裸 JSON **误认作协议载荷**。

### 11. 每账号一次全量 DDL 对账 + 3 条全表 UPDATE ✅

`sms_tool/store/accounts.py:23` `upsert_account()` 首行即 `init_database()`。单次成本 = `PRAGMA table_info` + 最多 26× `ALTER TABLE` 判定 + **3 条无条件全表 UPDATE**（`connection.py:124-141`：`paypal_status='link_ready'`、`refresh_token_status='no_rt'`、`plan_type=lower(account_type)`）+ commit。
`init_database(` 共 **16 处**调用，几乎全在函数体内而非进程启动处。其中 `store/accounts.py:393` 的 `rebuild_from_session_dir` **遍历 797 个 session** → 约 2400 次全表 UPDATE 扫描。

### 12. 数据无版本号，变更只能靠 `.get()` 散弹兜底 ✅

- SQLite：`_ensure_extra_columns` **有增量迁移但无 `PRAGMA user_version`、无 schema version 列** → 只能加列，无法表达更名/删列/改类型，也检测不了"新代码开旧库"；且每次启动都跑那 3 条全表 UPDATE 当迁移
- `sessions/*.json`（顶层 797 个）：**0 个**含 `schema_version`；顶层键组合 **29 种**不同形态（最大同构组 220，其余散布在 168/159/43/38/33/30/19…）
- 配置：`load_merged_config()`（`config.py:121`）**不做 schema 校验**，而 `load_runtime_config(validate=True)` 有。4 个模块绕过校验直接调它：`omakse_client.py:47`、`paypal_protocol.py:44`、`upi_link.py:67`、`paypal_link/gen_link.py:251`
- 老文件直接下标读取（Load 语境 30 处/11 文件）：`agent_identity.py:134`、`sub2api_import.py:187,205`、`cpa_import.py:680`

✅ 复核修正：首版 AST 未区分 `ast.Store`/`ast.Load`，把赋值（`data["quota"] = ...`）误报成读取。真正有 KeyError 风险的收窄到上述 4 处。

### 13. 类型化模型几乎没被采用 ✅

`sms_tool/account_models.py`（214 行 `AccountSessionModel`）**只在 2 个文件出现**：自身（3 次）与 `store/accounts.py`（6 次）。全库只有 `upsert_account` 一个入口把账号 dict 收敛成模型，其余模块继续传裸 dict。
账号/配置 dict 的传播广度：`data` → **33 个模块**、`config` → 24、`cfg` → 14、`account` → 13。
注解覆盖率（3044 个 `def`）约 74–76%，数字体面，但裸 `dict` 注解 105 处、`dict[..., Any]` 202 处、含 `Any` 773 处 —— **指标好看、类型安全为零**。

### 14. 跨语言常量重复（契约漂移温床）✅

| 概念 | 份数 | 位置 |
|---|---|---|
| 注册驱动（6 值） | **4 份** | `config.py:448`（只有 5 个，**缺 `protocol`**）、`cli.py:248`、`registration_drivers/base.py:15,48`、`SettingsCatalog.cs:52` |
| 支付终态（5 值） | **5 处** | `payment_contracts.py:12,16` + `pay_link/base.py:193`；C# `BackendResultInterpreter.cs:207`、`ProtocolPaymentExecution.cs:195,310,316` |
| 国家码 | `payment_methods.json` 内 **3 份**（checkout 13 / stage 16 / billing 20）+ C# 硬编码 1 份 | `SettingsCatalog.cs:169` |

`SettingsCatalog.cs:169` 的选项串里 **`"DE"` 出现两次**（下拉框重复显示），且 8 个唯一值只覆盖 20 个 `billing_countries` 中的 8 个。

✅ 复核推翻：支付方式（15 个 id）**不是重复** —— `pay_link/base.py:178` 是从 `CATALOG_METHODS` 推导，`PaymentMethods.cs:103` 从内嵌资源加载，唯一来源是根 `payment_methods.json`。但 `SmsWorkbench.csproj:17` 把它 `EmbeddedResource` 编进程序集 → **磁盘与程序集两份物理拷贝，无同步校验**（`sensitive_policy.json` 同样双份，但那个有 `sensitive_field_scan.py:44` 守着）。

### 15. 测试工程：整体健康，一处致命 ✅

✅ 正面的：断言密度 **108.9 条/千行**（Python 2756 条 / 129 文件）、C# `Assert.*` 784 条；skip **1 条**（`test_sentinel_runner.py:70` 的 `skipif(not shutil.which("node"))`，合法环境守卫）、xfail 0；flaky 信号极轻（`sleep(` 2 处、真实网络调用 **0 处**、写系统绝对路径 0 处）。

- **[高]** `tests/test_precommit_guard.py:27` —— `target = ROOT / "runtime" / filename` 后 `write_text`，**12 处调用**（:80,90,99,108,117,126,136,155,161,170,180）。写的是**生产数据区**（`runtime/accounts.sqlite3` 42.3 MB + `browser_profiles/` 473 MB）。且 `sensitive_field_scan.py:76-84` 会 `rglob` 扫 `runtime/` 找凭据产物 —— 测试写入的伪造密钥串若与扫描并发，**直接把 CI 打红**。当前 `finally: unlink` 生效、无残留，但设计上不该落在这里。

### 16. 配置与代码的双向对账 ✅

- 幽灵配置（配置有、代码零引用）：**L1 = 0 条，L2 = 41 条**（`proxy.json` 1、`runtime.json` 2、`payment.json` 38 —— 集中在 `paypal.*` 的回退/轮询开关）。逐个复核：只出现在 JSON 自身与 `config.example.json`
- 反向（代码读、配置无）：C# 142 条配置路径字面量中 **11 条不在分片**，其中 2 条是有意清理，**9 条真实缺失**（`proxy.mailbox`、`proxy.liveness`、`email_registration.mailbox_proxy`、两个 `email_registration.*.domain`、`paypal.billing_country`、`paypal.billing_region`、`phone_reuse.smsbower.endpoint`、`phone_reuse.smsbower.pool_size`）
- Python 侧确认缺失 1 条：`registration.stage_timeouts`（`config.py:472`、`registration_handlers.py:280` 都读，分片里没有 → 静默走 `{}`）

✅ **正面基准**：`SmsWorkbench/SettingsCatalog.cs` 声明的 **111 条配置路径，111/111 全部命中**分片，零漂移。这说明「集中声明配置路径」有效，**建议把它扩到 Python 侧**。

---

## 三、P2：文档与交付

### 17. README 上手路径：7/10 成立，断的 3 条全在第一次上手 ✅

| 文档说法 | 位置 | 实测 | 判定 |
|---|---|---|---|
| `python -m pip install -r requirements.txt` | `README.md:90,105,115` | 真实后端是 `.venv/Scripts/python.exe`（`runtime.json` 的 `runtime.python_path`）；README/EN 中 `.venv` **0 次**；分片里**没有** `runtime.python_path` 键 → 全新安装回落裸 `python` | ❌ |
| `copy config.example.json config.json` | `README.md:91,106,116` | 示例 18 键 vs 实际 22 键，缺 `kakao` / `momo` / `omakse` / `paypal_nocard` 四整节 | ❌ |
| 安装包方式 | `README.md:79-92` | 全程未提启动方式；真实启动器 `Start-SmsWorkbench.cmd` 在两份 README 中 **0 次提及** | ❌ |
| `preflight_env.py` / `build_dotnet.ps1` / `SmsWorkbench.exe` / `chatgpt_phone_reg.py --help` / `pytest -q` | — | 逐条实测均成立 | ✅ |
| 徽章 "Python 3.10+" | `README.md:12` | `.venv` 3.11.8、CI 3.12，仓库内无 3.10 验证 | ⚠️ |

- `constraints.txt`（14 条 pin）**无任何消费者**：`ci.yml:27` 未加 `-c`，发布包里也没有（8-31 18:56 才新增，晚于 00:54 构建），`pytest-cov` 未锁 → 孤儿文件
- `README_EN.md` 只链到 `release-v2026.08.22.md`，最新是 `.08.31`

### 18. docs 索引只覆盖 12/32，且违反自身规则 ✅

`docs/README.md` 引用 docs/ 内 12 个文件，**指针全部有效（0 断链）**；反向「实际有、索引没列」= **20 个**（5 个专题文档 + 15 个发布说明）。
最新发布说明 `docs/release-v2026.08.31.md` 未进索引 —— 而该索引自己的规则写着 "Update this index and the root README to point at the newest release"。
✅ `docs/directory-map.md` 反向差集 31 个**不算缺陷**（它的定位是仓库目录地图，不是 docs 索引），它引用的 3 个 docs 外目标全部存在。

### 19. 发布包体积

用 `os.walk` 累加（`du` 会 SIGTERM）：

- `dist/` **196.0 MB / 466 文件**：`release/` 117.5 MB（4 个历史资产）+ `installer/setup-publish` 54.2 MB（exe 副本）+ `installer/package` 13.6 MB（408 文件）+ `net10` 9.0 MB
- `scripts/` **137.2 MB / 326 文件**：`installer/bin` 121.9 + `installer/obj` 15.1（.NET 中间产物，已 gitignore），**真实脚本只有 0.1 MB / 26 个**
- ✅ 打包收集规则本身干净（`build_installer.ps1:191-207` 基于 `git ls-files`）：zip 实测 `__pycache__` / `.log` / `.sqlite` / `sessions/` / `runtime/` / `*_tokens.txt` / 真实 `.env` **全部为 0**

### 20. 仓库卫生 —— 本项目最强的一项 ✅

- `git ls-files -i -c --exclude-standard` → **0 条**（无"被忽略却已入库"）
- `git status --porcelain` → **空**
- 已入库 530 文件：`.log` / `.sqlite` / `.pyc` / `>1MB` 文件 **全部 0 个**；二进制仅 4 个 / 0.26 MB，全是合法图标资源
- 一轮指出的 `_*.py` 漏 `scripts/` 已修（`.gitignore:36`）

---

## 四、C# 侧：整体比预期健康得多

以下经复核**均为阴性**（推翻了常见 WPF 项目的刻板印象）：

- ✅ **Timer / DispatcherTimer：全仓 0 个**（唯一近似物 `CompositionTarget.Rendering` 静态事件已正确退订，且带重复订阅清理）
- ✅ **`async void` 0 个**；`Dispatcher.Invoke`（同步阻塞版）**0 个** → 无 UI 线程自锁死
- ✅ **4 处 `lock` 全部在 `await` 之前释放**；`SemaphoreSlim` 0 个；`new FileStream` / `new StreamReader` 生产代码 0 处
- ✅ **全局异常三件套齐全**：`App.xaml.cs:24-26`（`DispatcherUnhandledException` / `AppDomain.UnhandledException` / `TaskScheduler.UnobservedTaskException`），均写 `runtime/ui_errors.log`
- ✅ **DI 是真实存在的**：`AppHost.cs:28-48` 用 `Microsoft.Extensions.Hosting`，**16 个 `AddSingleton`**，`MainWindow` 走**构造注入**（9 个依赖）
- ✅ **可变静态字段为 0**（277 处 `static` = 195 方法 + 28 readonly + 27 const + 27 其他）。20 个静态类几乎全是纯函数，真正的不可测接缝只有 4 处（`ConfigStore` / `PaymentMethods` / `SensitiveDataSanitizer` / `Application.Current` 服务定位 + `SmsBowerCatalogClient` 绕过容器直连）
- ✅ **XAML 颜色治理优秀**：55 处 hex 中 **50 处在 `App.xaml`**（即主题字典本体），业务窗口仅 5 处
- ✅ 空 catch 仅 5 处，4 处是 best-effort 清理

**仍待修的**：

- **[中]** `MainWindow.Tasks.cs:239-243` —— `catch (Exception)` 分支**未复位 `StatusText`**。:213（成功）、:230（取消）、:236（已占用）都有复位，**唯独异常分支只写 `task.Status = "启动失败"`** → 状态栏永久停在"运行中"。这是真实的"loading 态永不复原"实例（二轮初判"普遍不复原"已推翻，两处 VM 的 `IsRunning` 都在 `finally` 正确复位）
- **[中]** `App.xaml.cs:44-49` —— `OnDispatcherUnhandledException` 弹框后**无条件 `e.Handled = true`**，任何未处理异常都被吞掉继续运行，UI 停在未知中间态
- **[中]** **159 个 `{Binding}` 全部隐式路径**，显式 `Binding Path=` 0 处 → 重命名属性即静默断链，无编译期校验（未用 `x:Bind`）
- **[中]** **30 组 Setter 重复 ≥3 次**（`Foreground={DynamicResource TextMain}` ×16、`BorderBrush={DynamicResource Line}` ×14…）；67 个 Style / 369 个 Setter 只用了 **20 处 `BasedOn`**，基类样式抽取严重不足
- **[中]** `MainWindow.xaml` 硬编码**尺寸 171 处**、FontSize 31、Margin 34；13 处固定像素 Grid 行列（67 条行列定义中固定占 19%），窗口缩放时不回流
- **[中]** `ProtocolPaymentViewModel.cs:184-234` —— `try` 只有 `finally` 无 `catch`，异常时 `StatusText` 不复原，异常逃逸进 `AsyncRelayCommand` 成为 unobserved task exception
- **[低]** `MainWindow.xaml.cs:19` `_lifetimeCts` **从未 Dispose**（12 处引用只有 `Cancel()`）
- **[低]** 协议健壮性：常驻协议**缺 version 字段**（对比一次性路径有 `version==2 && schema=="smsworkbench.ipc.v2"`）；错误**全部是纯字符串无结构化码**；`desktop_serve.py:74,83` 请求无法解析时回 `id=0`，该 id 永远匹配不到 pending 项 → 请求方干等 120s 超时

---

## 五、建议执行顺序

### 第一批：今天（2 小时内）

1. **下架 v2026.08.31 的两个资产并重发**。`gh release delete-asset` 删 exe + zip（保留 tag 与 release note），清库后重新 `build_installer.ps1 -Version v2026.08.31.1` 发布。
2. **判断那 3 个凭据要不要轮换**。6 位前缀不足以直接利用，但会大幅缩小爆破面。roxy token 与代理口令建议直接轮换（成本极低）；smailr key 是旧 key，确认是否已停用。
3. `scan_hardcoded_secrets.py:10` 的 `ROOT` 去掉一层 `dirname`，并补 `sys.exit(main())` —— 当前 CI 这一步毫无意义。
4. `build_installer.ps1` 在打包后、签名前插入 `sensitive_field_scan.py`（先补 `dist/installer/package/` 路径）+ 一个「包内是否含 `scripts/_*.py` / `pick_final*`」的硬断言。

### 第二批：本周（正确性兜底）

5. `config.json` 被分片旁路 → 加告警或自动改名（#4）
6. `GetOrStartResident` 加锁（#5）；`ProcessStartInfo` 补 `StandardInputEncoding`（#7）—— 后者是一行改动
7. `account_scan.py:458` / `one_click.py:183` 写盘失败时**中止后续 `upsert_account`**（#8）
8. `sensitive_policy.json` 补 `otp` / `sms_code` / `email` / `phone` / `proxy_password`，并改掉那 6 处明文 print（#9）
9. `tests/test_precommit_guard.py:27` 改用 `tmp_path`，别写生产 `runtime/`（#15）

### 第三批：2–4 周（降本）

10. 接上 `configure_logging()`，把 732 处 print 里属于诊断的那部分迁到 logger（#10）
11. `init_database` 收敛到进程启动一次；3 条全表 UPDATE 加 `user_version` 门控（#11）
12. 给 sessions JSON 加 `schema_version`；SQLite 加 `PRAGMA user_version`（#12）
13. 把 `SettingsCatalog.cs` 的「集中声明配置路径」做法扩到 Python 侧，顺便清掉 41 条幽灵配置、补上 10 条缺失（#16）
14. 跨语言常量收敛到单一来源（#14）

### 第四批：可并行（工程化）

15. README 三处断链 + `config.example.json` 补 4 节；处置 `constraints.txt`（要么接进 CI 要么删）（#17）
16. docs 索引补 20 个条目，或把 15 份 release note 挪出 `docs/`（#18）
17. XAML：样式 `BasedOn` 抽取、MainWindow 硬编码尺寸收进资源字典（#19 节的 #4 项）
18. `MainWindow.Tasks.cs:239` 补 StatusText 复位；`App.xaml.cs:44` 的 `e.Handled = true` 改为按异常类型判断（#20 前两条）

---

## 六、本轮方法论沉淀

1. **「清理了 git 历史」≠ 「清理了发布物」。** filter-repo / 强推只能改 git 对象。Release 资产、Docker 镜像、npm/PyPI 包、CI 缓存是**独立的泄漏通道**，清理动作必须逐个通道重新执行一遍。本轮就是靠 `gh api .../releases` 对账才发现 —— 只查本地 `git log` 永远不会发现。
2. **验 CI 门禁要验「能不能失败」，不是「有没有这一步」。** 一个 `grep -c "sys.exit"` 就查出两步扫描永远 exit 0。同理适用于任何"接了但没用"的门禁：linter、覆盖率阈值、架构约束扫描。
3. **发布脚本要不要扫描，看它是不是唯一不设防的出口。** 本仓 commit 有 precommit_guard、CI 有 4 步扫描，唯独 `build_installer.ps1` 0 命中 —— 而发布内容会送到 187 个 fork 之外。
4. **agent 的「推翻初判」段比结论段更值得看。** 本轮三路 agent 共推翻 14 条初判（URL 路径误报成 token、logging 默认走 stderr 不是 stdout、`async void`/Timer 全仓 0 个、颜色 50/55 集中在主题字典、`du`/端口冲突/`O(n²)` 假设不成立…）。不复核直接采信会把报告写成狼来了。
5. **AST 统计必须区分 `ast.Store` / `ast.Load`**，否则 `data["x"] = 1`（赋值）会被报成「读取老文件可能 KeyError」。本轮首版误报 15 处。
6. **Windows 上 `.venv` 是唯一可信 python**；`du -sh` 在 2 万文件目录必 SIGTERM，体积一律用 `os.walk`；bash heredoc 写脚本会被拦，用 Write 工具落到 `%TEMP%`，路径写 `C:/...` 不写 `/tmp`（MSYS 解析成 `F:\tmp`）。

---

## 七、复核说明

主 agent 亲自 grep / 读源码复核的条目，上文已标 ✅。复核实锤的：#1（zip+exe 内文件清单 + `gh api` 下载数）、#2（grep -c = 0）、#3（ROOT 三层 dirname + 无 sys.exit + ci.yml 行号）、#4（`config.py:127` 分支）、#5（`DesktopReadClient.cs:258` 无 lock）、#7（:313-318 字段比对）、#8（`account_scan.py:457-461`）、#9（`sensitive_policy.json` 逐词 grep + 6 处 print 行号）、#10（`configure_logging` 仅 2 处命中，全在定义文件内）、#11（16 处 `init_database(` 调用 + 3 条无条件 UPDATE 全文）、#15（`test_precommit_guard.py:27` + 12 处调用）。

三路 agent 自行推翻的初判共 14 条，散见各节 ✅ 标记处。典型：`logs/roxynet` 的 "rt_" 命中是 URL 路径；`logging.StreamHandler()` 无参默认 stderr；`TcpClient`/`HttpListener` 0 命中故无端口冲突；`async void`/`Dispatcher.Invoke`/`Timer` 全仓 0 个；55 处 hex 中 50 处在主题字典；`dist/net10/runtime/*.log` 被 `build_installer.ps1:212` 显式删除故不进包；支付方式清单不是重复（推导式）；`test_cli_ba_link.py` 无落盘调用。
