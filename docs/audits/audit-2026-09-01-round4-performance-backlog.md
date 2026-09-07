# 第四轮深度审计 — 性能 / 前端层 / 数据层 / IPC / 依赖

日期：2026-09-01
范围：`sms_tool/`（181 py，2208 函数）、`SmsWorkbench/`（73 cs + 5 xaml）、`SmsWorkbench.Contracts/`、`services/`
排除：`dist/`、`runtime/`（源码副本）、`.dotnet/`（本地 SDK）、`.venv/`、`tests/`、`sessions/`、`__pycache__/`、`.agents/`、`.claude/`

前三轮定位：一轮=死代码与重复实现、二轮=并发与架构、三轮=发布出口与数据正确性。
**本轮是补集**：性能与资源效率、WPF 前端层与 MVVM、SQLite 数据层与事务、IPC 协议健壮性、依赖治理、仓库卫生。

---

## 零、最关键的 6 条（按「会不会真出事」排序）

### 1. 🔴 P0：`get_account_record()` 每次调用重放全库 DDL + 3 条全表 UPDATE ✅ 实测

`store/accounts.py:214` 的 `get_account_record()` 第一件事是调 `init_database()`，
后者重放全部 `CREATE TABLE/INDEX` + `PRAGMA table_info` + **3 条无 WHERE 的全表 UPDATE**
（`connection.py:117-140`，`paypal_status`/`refresh_token_status`/`plan_type`）。

本机实测（`runtime/accounts.sqlite3`，795 行 / 42.3 MB）：

| 指标 | 实测值 |
|---|---|
| `init_database()` | min 96.7 / **med 99.9** / max 112.5 ms |
| `get_account_record()`（n=10） | min 99.4 / **med 104.0** / max 120.2 ms |
| 795 账号全量刷新 | **≈ 82.7 秒纯 DB 开销** |

`init_database()` 全仓 **16 个调用点**，`store/accounts.py` 独占 9 处 —— 不是某一处写错，
是整个 `store/` 层的默认姿势是「每次调用重建 schema」。叠加调用方：
- `account_promotion.py:236` `for email in requested: get_account_record(email)` —— **DB 读取在并发区之外**，
  `:245` 的 `ThreadPoolExecutor` 只并发了后面的网络探测
- `account_health_queue.py:253`、`:300` 健康检查队列内逐账号调用

**增长形态是 O(N²)**：账号翻倍 → 单次 `init_database` 变慢（全表 UPDATE）× 调用次数翻倍。

### 2. 🟠 P1：SQLite 是 `delete` 日志模式（但**没有** busy_timeout 缺口）✅ 实测 + ⚠️ 已自我证伪一条

> **⚠️ 修正（2026-09-01 落地时实测推翻初判）**：
> 原文称「全仓 0 处 `busy_timeout`」—— 这是 **grep 的假阴性**。
> CPython 在 C 层就把它设好了，源码里当然搜不到：
> ```
> sqlite3.connect(p)                -> PRAGMA busy_timeout = 5000   ← 默认就是 5 秒
> sqlite3.connect(p, timeout=30.0)  -> PRAGMA busy_timeout = 30000
> ```
> 所以「补 busy_timeout」这个动作**本身就是个 no-op**，已撤销。
> **教训：grep 扫不到 ≠ 没有生效。凡是「框架/运行时可能已默认设置」的项，必须读运行时值而不是搜源码文本。**
> 真正成立的部分只剩下 `journal_mode=delete`。

| 项 | 实测 |
|---|---|
| `PRAGMA journal_mode` | **`delete`**（非 WAL）—— 这项成立 |
| `busy_timeout` | **5000 ms（CPython `timeout` 参数默认值，早已生效）** —— 无缺口 |
| 连接策略 | `connection.py:35` `_connect()` 每次操作 `sqlite3.connect()` 并在 `finally` 关闭，**无池化** —— 这项成立 |
| 并发写方 | 12+ 处 `ThreadPoolExecutor`（`batch_runner.py:32,265`、`account_scan.py:67`、`account_health_queue.py:188`、`account_lifecycle.py:60`…）+ WPF 常驻进程 + 一次性任务进程 —— 这项成立 |

**WAL 已实测、并被明确推迟**（不是没做，是测出来现在开了会更慢）：用 `init_database()` 那 3 条
全表 UPDATE（每次改动约 32 MB，远超 1000 页的自动 checkpoint 阈值，导致每次提交都重写整库）做压测：

| 模式 | 写事务数 / 6 秒 | 读者 p95 延迟 |
|---|---|---|
| `delete`（当前，保留） | **41** | **1.68 ms** |
| WAL | 26 | 4.43 ms |
| WAL + `wal_autocheckpoint=0` | 17 | 121.36 ms |

**WAL 在当前代码状态下慢 1.8 倍。** 但换成小事务（单行 UPDATE）后结论完全反转：
WAL 57 次读 vs `delete` 7 次读，且 `delete` 模式的读者撞到了 `database is locked`。
**=> WAL 必须与第 8 节第 6 项（`init_database()` 摘出热路径）一起落地，不能先于它。**

### 3. 🔴 P0：`lower(email)=lower(?)` 让唯一索引彻底失效 ✅ EXPLAIN 实测

```
EXPLAIN QUERY PLAN SELECT * FROM accounts WHERE lower(email)=lower(?)
  → (2, 0, 0, 'SCAN accounts')                    ← 全表扫

EXPLAIN QUERY PLAN SELECT * FROM accounts WHERE email=?
  → (3, 0, 0, 'SEARCH accounts USING INDEX sqlite_autoindex_accounts_1 (email=?)')
```

同一张表，去掉 `lower()` 就从 SCAN 变 SEARCH。该模式全仓 **37 处**
（`accounts.py:219,297,301`、`markers.py:25,92,138,167,198,227,258`…）。
795 行 × `raw_json` 均值 41,200 B ⇒ 每次查找扫描 **~31 MiB**。

同类：`accounts.py:418-421` 的 4 条 `lower(raw_json) LIKE '%account_deactivated%'`
（前置通配 + 函数包裹，索引必然失效，且 `raw_json` 是 41 KB 的 TEXT blob）。

### 4. 🔴 P0（用户可见的 bug）：任务列表「参数」列永不刷新 ✅ 已定位到唯一改写点

`MainWindow.xaml.cs:319-328`：

```csharp
public sealed partial class TaskRow : ObservableObject
{
    [ObservableProperty] private string status = "";   // ← 有通知
    [ObservableProperty] private string cost = "";     // ← 有通知
    [ObservableProperty] private string doneAt = "";   // ← 有通知
    public string Name { get; set; } = "";             // ← 无通知
    public string Task { get; set; } = "";             // ← 无通知
    public string Info { get; set; } = "";             // ← 无通知 ★
    public string Retry { get; set; } = "0";           // ← 无通知
}
```

- `MainWindow.xaml:911` `<DataGridTextColumn Header="参数" Binding="{Binding Info}" Width="520"/>`
- `MainWindow.Tasks.cs:185` `task.Info = progressEvent.Detail.Length > 0 ? ... : ...` —— 运行时确实改写

同排的 `Status`/`Cost`/`DoneAt` 是 `[ObservableProperty]` 所以正常刷新，**只有「参数」列是死的**。
用户看到的现象会是：任务在跑，时间/状态在变，参数列一直空着或停在初始值。

### 5. 🟠 P1：`.gitignore` 的一次性脚本防护漏了 3 个目录 —— 与 08-31 凭据泄漏同一条路径 ✅ 实测复现

`.gitignore:35-36` 现在只有 `/_*.py` 和 `scripts/_*.py`。实测：

```
services/_diag_x.py      !! 未忽略（会被 git add -A 收录）
tests/_diag_x.py         !! 未忽略（会被 git add -A 收录）
sms_tool/_diag_x.py      !! 未忽略（会被 git add -A 收录）
scripts/_diag_x.py       已忽略
_diag_x.py               已忽略
```

08-31 那次事故（`ee02fab` 把带 3 个真实凭据前缀的 `scripts/pick_final_replacements.py` 推上公开仓库
并打进 Release）事后只补了出事的那一条路径。诊断脚本放在 `services/` 或 `sms_tool/` 下重演一次即可复现。

### 6. 🟠 P1：`nodriver` 是未声明依赖，当前环境里根本不存在 ✅ 实测

- `nodriver_captcha.py:31`、`nodriver_paypal.py:57` 共 2 处 `import nodriver as uc`
- `requirements.txt` / `constraints.txt` 中 **0 处**
- 实测 `.venv`：`ModuleNotFoundError: No module named 'nodriver'`

两处都在 `try/except` 里静默降级，所以 CAPTCHA 的 nodriver 路径**一直没跑起来而无人知晓**。

---

## 一、P0 / P1：性能与资源效率

- **[高]** `store/accounts.py:214` — `get_account_record()` 每次重放全库 DDL — 见零的第 1 条，实测 104ms/次、795 账号 ≈ 82.7 s
- **[高]** `account_promotion.py:236` / `account_health_queue.py:253,300` — 逐账号 `get_account_record()`，DB 读取留在并发区之外 — 承接上条，全量刷新走串行
- **[高]** `store/accounts.py:418-421` — 4 条 `lower(raw_json) LIKE '%account_deactivated%'` — EXPLAIN 实测 `SCAN accounts`；`account_deactivated` 本就是已知状态值，应走 `status IN (...)`
- **[中]** `store/accounts.py:246` — `SELECT * FROM accounts ORDER BY updated_at DESC` **无 LIMIT** — 795 行 × 41 KB `raw_json` ≈ 31 MiB 一次拉进 Python，调用方通常只取前 N 条
- **[中]** `account_lifecycle.py:129-131` — **删 1 个账号要全量读+解析 797 个 session 文件（32.5 MB）** — 实测 `sessions/session_*.json` = 797 文件 / 32.5 MB，单文件 ~41 KB；逐个 `json.loads(read_text())` 比 email。没有反查索引
- **[中]** `store/accounts.py:397` `rebuild_from_session_dir()` — 同样的 797 文件全量读（维护操作，非热路径），且 `:399` 失败只 `print` 跳过
- ~~**[中]** `account_2fa.py:54,69,77,90,106,114,132,158,180` — **9 处 curl_cffi 请求全部无 `timeout`** — 全仓 102 处 HTTP 调用中 29 处缺 timeout，~~ **⚠️ 已证伪（2026-09-01）**：curl_cffi 的 `Session` 默认 `timeout=30`，用本地挂起 server 实测确认这 9 处**原本就有 30 秒超时**。全仓按「客户端家族」重扫后**真实缺口为 0**（详见第十一节第 9 条）。改为加 `tests/test_http_timeout_guard.py` 常驻守卫
- **[中]** `pay_link/adapters.py:96` — 协议抽取子进程默认 `timeout = ... or 900`（**15 分钟**）— `payment.json` 的 `protocol_payments.timeout_seconds=900`，8 个 method 全部无 override。对比 `sentinel/runner.py:161` 做了 `max(10, min(timeout, 120))` 夹紧
- **[中]** `nodriver_captcha.py:71,103,148,164,193` — 5 处固定 `asyncio.sleep(5)` 盲等，不受 `deadline` 约束 — 单次 solve 至少 25 秒固定开销
- **[中]** `paypal/form_steps.py`（9 处）+ `paypal/flow_steps.py`（10 处）— 固定 `time.sleep(1~2s)` 紧跟在点击/填表后代替 `wait_for_load_state` — 每次 PayPal 流程约 14 秒固定等待
- **[中]** `smsbower.py:89`、`phone_proxy.py:263`、`nodriver_paypal.py:432,452`、`paypal_reverse.py:457`、`sms_utils.py:51,76` — **8 处在轮询循环里用模块级 `requests.get`/`curl_requests.get`** — 每次重做 TCP + TLS 握手。全仓 35 处模块级 HTTP，只有这 8 处在循环内

## 二、P0 / P1：WPF 前端层

- **[高]** `MainWindow.xaml.cs:319-328` + `MainWindow.xaml:911` + `MainWindow.Tasks.cs:185` — `TaskRow.Info` 无通知却被绑定且运行时改写 — 见零的第 4 条
- **[高]** `StageMatrixStore.cs:38-51` — `Append()` **每个进度事件**同步执行 `File.AppendAllText` + `File.ReadLines(_path).TakeLast(2001)` **全文件回读**，超限时再 `WriteAllLines` + `File.Move` —**O(N²) 同步磁盘 IO 全压在 UI 线程**。一次长批次 = 上千次全文件回读。唯一调用点 `StageMatrixViewModel.cs:129`（`persist: true`）
- **[中]** `MainWindow.Pools.cs:360-372` — `AddBackendAccountRow` 对**每一行**做 **3 次 `raw_json` 全量解析**：`GetImportedStatus(rawJson)`(:366)、`GetPaypalAmount(rawJson)`(:368)、`GetPaypalAmount(rawJson)` 再一次（:372，在 `DisplayPromotionStatus` 实参里）。N 行 = 3N 次，且在 `await ReadPoolsAsync()` 之后的 UI 线程续体上
- **[中]** `MainWindow.xaml.cs:267-302` — `PoolRow` 里 **31 个** `public string X { get; set; }` 全无通知，被 `MainWindow.xaml:819-831` 的 13 个 DataGrid 列绑定 — 实测运行时改写为 0 处（`Pools.cs:353` 每次重建整行），属**潜伏**风险
- **[中]** `MainWindow.xaml` — `Click=` **39** 处 vs `Command=` **0** 处；而三个次级窗口（`PaymentBatchWindow`/`ProtocolPaymentWindow`/`SettingsWindow`）是 **0 Click / 14 Command** — 同一解决方案内两代架构，**主窗口完全停留在事件处理器时代**
- **[中]** `MainWindow.xaml` — **18 个**逐字相同的侧边栏按钮（`Style="{StaticResource SidebarNavButtonStyle}"`），仅 `Click`/`Path.Data`/`Text` 不同 — 约 130 行可抽成一个 `IconNavButton` 控件
- **[中]** `App.xaml` — **12 个无 `x:Key` 的全局隐式样式**（`TextBlock:41`、`Button:60`、`TextBox:154`、`DataGrid:518`…）— 任一第三方控件或新 Window 都会被静默套用，`:41` 无条件改所有 `TextBlock.Foreground`
- **[中]** 171 个 `{Binding}` 中：`ValidationRules` **0**、`IDataErrorInfo` **0**、`INotifyDataErrorInfo` **0**、`FallbackValue` **0**、`TargetNullValue` **0**、`PresentationTraceSources` **0** — **绑定失败 100% 静默**，无兜底也无诊断开关
- **[中]** ` MainWindow.*.cs` — **200 处命令式 `new <WPF控件>`**（`RowDefinition 46`、`TextBlock 34`、`Button 28`、`StackPanel 24`、`Window 12`…），即 **12 个对话框完全用 C# 搭 UI**（`ShowDialog()` = 12 处），与 `DialogFactory.cs:16` 并存两套构造方式
- **[中]** `MainWindow.Tasks.cs:262-273` — `RefreshPoolsAfterHotPersistence` 在进度回调内触发整表重建（750 ms 节流仍在 UI 线程）
- **[中]** `MainWindow.Register.cs:16,76,99,113,126,138,151,172,213` — 注册/接码主流程走 `RunBackend`（无 `progressDomain`）→ **无进度弹窗、无任务内取消**；只有 `RunAccountBatchBackend` 才有进度对话框。最长等待 12 小时（`MainWindow.Tasks.cs:17` `BackendTaskTimeoutMs`）
- **[中]** `App.xaml.cs:44-49` — `DispatcherUnhandledException` 只 `MessageBox.Show(e.Exception.Message)` 后 `e.Handled = true` — 无堆栈、无操作上下文、不提示重启，**应用带着未知状态继续跑**
- **[低]** 96 个 `catch` 中：空体/仅注释 **23** 处、只写日志不触达 UI **61** 处、有 UI 呈现仅 **20** 处

## 三、P0 / P1：SQLite 数据层

- **[高]** `journal_mode=delete` + **0 处 `busy_timeout`** + 每次新建连接 — 见零的第 2 条
- **[高]** `lower(email)=lower(?)` → `SCAN accounts`（37 处）— 见零的第 3 条
- **[高]** `store/connection.py:44-115` — 3 张表 **0 外键**；`registration_audit` 除自增 id 外**无唯一约束**，`(batch_id,email)` 可重复插入
- **[中]** `accounts` 58 列中 **55 列无索引**；高频过滤列 `status`/`mailbox_provider`/`plan_type`/`paypal_status`/`quota_status`/`batch_id`/`registration_state` 全裸
- **[中]** 事务：`commit()` 14 处 / `execute` 70 处 = 0.20，**`executemany` 全仓 0 处**
  - `accounts.py:392-403` `rebuild_from_session_dir` 遍历 797 文件，每个 `upsert_account` 内部 1 次 commit（AST 扫不出来，commit 藏在被调函数里）
  - `registration_handlers.py:337-339` 每次状态迁移 2 次 commit
  - `account_lifecycle.py:103-108` 逐 rowid `DELETE`，未用 `executemany`
- **[中]** `omakse_client.py:550` — 硬编码 `os.path.join(PROJECT_ROOT,"runtime","accounts.sqlite3")`，绕过 `database_path()` 配置 — 用户改 `storage.sqlite_path` 后**此处读到空库，静默出错**
- **[中]** `account_lifecycle.py:79` `with sqlite3.connect(db) as conn:` — 上下文管理器只 commit/rollback，**不关闭连接**，连接泄漏
- **[中]** 实测 `VACUUM|retention|max_age|prune|purge` **0 处**命中，`auto_vacuum=0`，无任何清理/归档策略
- **[中]** `registration_audit` 实测 `{'active': 817, 'failed': 650, 'pending': 817}` — **pending 与 active 数量完全相等**，疑似每账号成对写入（未追踪写入点验证）
- **[中]** `registration_checkpoints` 183 行**无过期机制** — 崩溃残留的断点永久驻留
- **[中]** `raw_json` 实测占库 **31.2 MiB / 42.3 MiB = 74%**（均值 41,200 B、最大 87,399 B）— 库体积几乎全由冗余 JSON 大字段驱动
- **[低]** 参数化：仅 `connection.py:123` 等 3 处 f-string 拼 SQL，插值全部来自模块内常量（`store/constants.py:EXTRA_COLUMNS`），**无注入面**

## 四、P1：IPC 协议健壮性

**核心结论：仓库里有两套线协议，受版本校验的是次要那条。**

| | 一次性通道 | 常驻 JSONL 通道（主用） |
|---|---|---|
| 信封/版本 | ✅ `smsworkbench.ipc.v2` v2（`desktop_ipc.py:56-68`） | ❌ 仅 `id/ok/payload\|error`（`desktop_serve.py:80-86`） |
| 版本校验 | ✅ `BackendJsonProtocol.cs:28-33` | ❌ 无 |
| 脱敏 | ✅ `desktop_ipc.py:71-73` | ❌ 刻意绕过（`desktop_serve.py:89-100`） |
| 读超时 | — | ❌ Python `reader.readline()` 无超时 |

- **[高]** `DesktopReadClient.cs:352` 读超时 **120 s** vs `desktop_serve.py:108` Python 侧 `reader.readline()` **无任何超时** — 单边。Python 是单线程串行服务，一次卡顿阻塞后续全部请求，而 C# 已在 120 s 放弃
- **[高]** 心跳/keepalive/watchdog 实测 **0 处** — 死亡检测完全被动：`IsAlive => !_closed && !_process.HasExited`。**进程「活着但卡死」永不被告知**，发现延迟最长 120 s 且只在发生读请求时暴露
- **[高]** `desktop_serve.py:85` 错误是 `f"{type(exc).__name__}: {exc}"` — **无机器可读错误码**。C# `BackendResultInterpreter.cs:62-77` 对 5 个键匹配 6 条子串判定「是否掉号」，其中含**拼写错误 `account_deatived`（`:69`）**。改任一文案即断链
- **[中]** `DesktopReadClient.cs:359-366` — 超时/取消分支**未从 `_pending` 移除 id**（仅失败分支 `:348` 调了 `Complete(id, default)`）— 每次超时泄漏一个 `TaskCompletionSource`，`_pending` 无界增长
- **[中]** `desktop_serve.py:78` 畸形请求回 `{"id":0,...}`，而 C# `++_nextId`（`:336`）从 **1** 起 → id=0 的响应**永无认领方**，调用方空等到 120 s 超时。（`request_id = 0` 在 `:76` 已预初始化，不会 NameError —— 只会静默错配）
- **[中]** `BackendResultInterpreter.cs:24-43` `TryExtractScanSummary` — 从 stdout 文本**倒序暴力截取** `{...}` 试探解析，找同时含 `results`+`total` 的块，**而非消费 V2 信封** — 任意含花括号的进度输出都可能改变结果
- **[中]** `desktop_serve.py:85` 错误信息截断至 500 字符，深层堆栈丢失
- **[中]** `BackendResultInterpreter.cs:148` — `"phone_verification_required"` 映射到 `"支付完成"`，与 `:147` 的 `"手机验证"` 语义不一致（是否笔误需与需求侧核对）
- **[低]** `ExtractPayloadFromResponse`（`:245-256`）对 `ok=false` 抛 `InvalidOperationException` 而非 `ResidentChannelException` — 后端业务错误不会触发一次性回退

## 五、P1：依赖治理与 vendored 库

- **[高]** `nodriver` 未声明且实测不存在 — 见零的第 6 条
- **[高]** `services/protocol-payment/LICENSE:3` — MIT 但版权行为 **`Copyright (c) 2026`**，**无权利人姓名/组织**，MIT 要求的署名主体缺失
- **[高]** `services/protocol-payment/README.md:6-9` — 上游来源仅写作本地绝对路径 `F:\epsoft\pix` 与 `ideal-link-extractor-open-source-20260712`，**无 URL、无 tag、无 commit** — 上游不可追溯，升级路径实际断死
- **[高]** 该目录**无任何依赖声明**（`requirements.txt`/`pyproject.toml`/`setup.py` 全 absent），规模 14 个 .py / **17,951 行**，最大 `blik/blik_qr_extract.py` **3,792 行 / 167 KB**
- **[中]** **依赖方向被倒置**：`kakao/kakao_extract.py:42` `from sms_tool.account_liveness import probe_account_liveness` — 它不是独立第三方库，而是与宿主双向纠缠。**即便改成 submodule 也无法独立存在**，「vendor vs submodule」的取舍已不成立
- **[中]** 下限跨越主版本且 `constraints.txt` 无人消费 → 实际安装版本完全漂移：`browserforge>=0.1.0`→1.2.4、`cryptography>=41.0.0`→50.0.1、`qrcode[pil]>=7.4.2`→8.2、`playwright>=1.40.0`→1.60.0、`selenium>=4.20.0`→4.48.0
- **[中]** `constraints.txt` 是孤儿：`.github/workflows/ci.yml:42` 实测为 `pip install -r requirements.txt`，**无 `-c constraints.txt`**；`pytest-cov` 进了 `requirements.txt:24` 却未进 constraints
- **[中]** `playwright-stealth==2.0.3` — 上游为 fork 链（原项目 → AtuboDad → Mattwmaster58 2.x），PyPI 自述 "proof-of-concept starting point"、"Don't expect this to bypass anything"。仅 `registration_drivers/stealth.py` 1 处使用，却承担反检测职责
- **[低]** `httpx[http2,socks]`（`requirements.txt:5`）与 `selenium`（`:19`）在 `sms_tool/`/`services/`/`scripts/`/`tests/` 中 **0 处 import**
- **[低]** `scripts/installer/GPTRegisterToolSetup.csproj` **未列入 `GPTRegisterTool.slnx`** → 解决方案级构建跳过安装器；同时它 0 个 `PackageReference` 却继承 CPM 与 `RestorePackagesWithLockFile=true` 并已生成 lock 文件
- **[低]** NuGet 治理到位：`Directory.Packages.props` 10 个 `PackageVersion` **全部被消费，无孤儿包**；4 个项目 lock 文件齐备；`Directory.Build.props:9` 把 NU1901-NU1904（漏洞包）设为错误

## 六、P1/P2：类型化与可维护性（Python）

- **[高]** `payment_contracts.py:19` 定义了 `PaymentResultDict(TypedDict, total=False)`（16 字段），但**全仓仅 2 处引用，都在其自身文件内** — 全仓**唯一**的 TypedDict，定义了却没人用，支付结果的形状契约事实上不存在
- **[高]** **229 个函数**声明返回 `dict[str, Any]`，`dict[str, Any]` 出现 **520 次**；全仓只有 **17 个 dataclass**、**0 个 NamedTuple** — 核心领域对象（账号记录、支付结果、代理、会话）层间一律裸 dict。
  已有正确范式但未推广：`account_models.py:19/32/43` 的 frozen dataclass + `field(repr=False)` 防凭据 repr 泄漏，质量很高，而 `store/accounts.py` 完全没用它
- **[高]** **13 个文件（函数数均 ≥15）注解覆盖字面 0.0%** — `account_scan.py`、`agent_identity.py`、`auth_flow.py`、`codex_export.py`、`cpa_import.py`、`mail_otp.py`、`mailbox_gmail.py`、`mailbox_parsers.py`、`providers/cfworker_mailbox.py`、`session_refresh.py`、`store/normalize.py`、`sub2api_import.py`、`workspace_scan.py`，合计 **314 个函数 / 724 个形参零注解**
- **[中]** 全仓形参注解覆盖 **64.6%**（3004/4652）、返回注解 **65.4%**（1444/2208）；
  `mailbox.py` 9.2%（返回注解 2.0%）、`cli.py` 7.4%、`mailbox_remail.py` 3.8%；
  `Any` 出现 **1525 次**，最密集的 5 个文件正是支付与注册主流程
- **[中]** `impersonate="chrome124"` 硬编码在 **12 个文件** — 浏览器指纹是全局一致性要求（同一账号 checkout→approve 必须同指纹），升级指纹要同步改 12 处，漏一处即 TLS 指纹不一致
- **[中]** `timeout=30` 出现在 **27 个文件**、`timeout=10` 在 **11 个文件** — 无法统一调参
- **[中]** `registration_drivers/external_sessions.py:564` — Roxy 默认地址硬编码 `http://127.0.0.1:50000`，该值同时存在于 `config.json:230`/`runtime.json:22`/`config.example.json:46`；源码 `:562` 注释自陈 "50100 was wrong"，即**这个兜底值已经错过一次**
- **[中]** `registration_drivers/playwright.py:545` — 生产代码里 `is_mock_page = type(page).__module__.startswith("unittest.mock")` 并据此分支 — 测试关注点泄漏进生产路径
- **[中]** `desktop_read.py:289` → `mailbox_parsers.py:260,267` — IPC 服务路径上 `print(f"[!] Skip malformed chatai line ...")` 写 **sys.stdout**，而 `desktop_serve.py` 的契约是「一行一个 JSON」→ `mailbox-pool`/`pools` 两个 op 遇畸形行即向 stdout 注入非 JSON 行（`mailbox_tokens.txt` 有 336 条非注释行）
- **[中]** `sms_tool/` 根目录平铺 **130 个 .py**，而 `commands/` 10 个、`store/` 7 个、`pay_link/` 7 个 — 分层只完成一小部分；`paypal/`(8) / `paypal_link/`(3) / `pay_link/`(7) 三个支付相关目录并存，职责边界从名字无法判断
- **[低]** `services/mail-otp-web/` 是**运维孤岛**：与 `sms_tool/` **零耦合**（无启动脚本、无 CI 作业、无 C# 调用、无端口 8791 引用），只能按 README 人手 `python app.py`。（背景更正：它**不是** Flask，用的是 stdlib `http.server.ThreadingHTTPServer`）
- **[低]** `gen_pp_link.py:6-7`、`paypal_reconciliation.py:6-7` — 纯转发壳 `from .paypal_link import *`

## 七、P2：仓库卫生与磁盘（**只报告，未删除任何内容**）

- **[低]** `git status --porcelain -unormal` = **0 行**；`git ls-files -i -c --exclude-standard` = **0 行**；`git ls-files --others --exclude-standard` = **0 行** — 工作区干净，**无游离文件**

总计 **2.7 GiB**，其中被 git 跟踪的内容仅 **6.1 MiB / 534 文件**，忽略文件 41,567 个。
可安全回收（按体积排序）：

| 路径 | 体积 | 说明 |
|---|---|---|
| `.dotnet/` | **760.9 MiB** / 5,607 文件 | 本地 SDK 副本（sdk 390 / shared 199 / packs 161） |
| `runtime/browser_profiles/` | 491.0 MiB / 3,420 文件 | ⚠️ 未确认是否承载已登录会话状态，**确认前勿删** |
| `runtime/camoufox_dl/camoufox_win.zip` | **470.3 MiB**（单文件） | 08-30 的下载产物，可重下 |
| `dist/` | 255.3 MiB / 476 文件 | `release/` 176.6 MiB 旧发布包 + `installer/` 68.0 MiB |
| `runtime/relogin_backups/` | 83.5 MiB / 695 文件 | 无保留期 |
| `runtime/deletion_backups/` + `accounts.sqlite3.pre_cleanup_20260823` | 25.6 + 31.6 MiB | 已滞留 8 天的快照 |
| `runtime/_filter_repo_work/` | 15.2 MiB / 677 文件 | 历史重写遗留工作目录 |
| `sessions/` | 36.1 MiB / 1,114 文件 | 无保留期，全部 08 月生成 |

---

## 八、建议执行顺序

### 第一批：✅ 已于 2026-09-01 落地（详见文末《第一批落地记录》）

| # | 项 | 结果 |
|---|---|---|
| 1 | 补 `.gitignore` | ✅ 改成全局规则 `_*.py` + `!__init__.py`，实测 7 个探针全中、0 误伤 |
| 2 | WAL + busy_timeout | ⚠️ **拆成两半**：busy_timeout 经实测是 no-op（CPython 默认已设 5000ms）→ 撤销；WAL 经实测会劣化 1.8 倍 → 推迟到第 6 项一起做 |
| 3 | 修 `TaskRow.Info` | ✅ 改 `[ObservableProperty]`，反射实测 PropertyChanged 已触发 |
| 4 | `nodriver` 二选一 | ✅ 选择「声明」而非「删除」（两个调用方都是真实生产路径）：进 `requirements.txt` + 降级点改为显式告警 |
| 5 | 消除 `lower()` 全表扫 | ✅ 加 `lower(email)` 表达式索引（零行为变更）：实测 2.647 ms → 0.099 ms，**快 26.7 倍** |

---

### ~~第一批（原始计划，已被上表取代）~~
1. **补 `.gitignore`**：把 `/_*.py` + `scripts/_*.py` 换成能覆盖全部顶层源码目录的规则（`services/_*.py`、`tests/_*.py`、`sms_tool/_*.py`）。实测已复现漏洞，这是 08-31 事故的同类路径
2. **开 WAL + busy_timeout**：`connection.py:35` `_connect()` 里加两行 `PRAGMA journal_mode=WAL` / `PRAGMA busy_timeout=5000`。当前 `delete` 模式 + 12 处并发写方 = 必然冲突
3. **修 `TaskRow.Info`**：改成 `[ObservableProperty]`。一行改动，修掉一个用户可见 bug
4. **`nodriver` 二选一**：要么加进 `requirements.txt`，要么删掉两处 import 和对应的降级分支 —— **不要让一条永不执行的代码路径继续伪装成可用能力**
5. **删 `lower()` 包裹**：`accounts.py:219,297,301` + `markers.py` 共 37 处，改存规范化小写 email 或加 `lower(email)` 表达式索引。EXPLAIN 已证明 SCAN→SEARCH

### 第二批：✅ 已于 2026-09-01 落地（详见文末《第二批落地记录》）

| # | 项 | 结果 |
|---|---|---|
| 6 | `init_database()` 摘出热路径 | ✅ 一次性初始化标志（按解析后的库路径记忆化）：**97.4 ms → 0.33 ms，294 倍**；`get_account_record` 端到端 **99.4 ms → 1.95 ms，51 倍** |
| 7 | `StageMatrixStore.Append` | ✅ 内存缓冲 + 环形裁剪：2000 次追加 **21,961 ms → 652 ms，33.7 倍**；满仓后单次追加 **24.2 ms → 0.38 ms，63.7 倍** |
| 8 | 合并 `raw_json` 解析 | ✅ 每行 3 次 JSON 解析 → 1 次，另外 `DisplayPayPalStatus` 也被重复算了 1 次（原报告只发现了前者） |
| 9 | `account_2fa.py` 补 timeout | ⚠️ **证伪，0 处真缺口**。curl_cffi 的 `Session` 默认就是 `timeout=30`。改为加 `tests/test_http_timeout_guard.py` 常驻守卫 |
| 10 | `omakse_client.py:550` | ✅ 改用 `database_path()`；顺带修掉一个真 bug：查询是大小写**敏感**的 `email=?` |
| 11 | `account_lifecycle.py:79` | ✅ `with sqlite3.connect()` 是事务上下文、**不关连接**，改用 `closing()`；全仓仅此一处该模式 |

### 第三批：2–4 周（降本，可并行）
12. ✅ `get_account_record` 批处理化：`store/accounts.py` 新增 `get_account_records(emails)`（`WHERE lower(email) IN (...)` 一次查询替代 N 次）
13. ✅ `account_lifecycle.py` 删号全量扫 797 文件 → 按 `session_{email去+}_{ts}.json` 文件名前缀 glob 收窄，只打开命中几个文件（已加 200 文件规模化回归测试）
14. ✅ `MainWindow.xaml` 侧边栏按钮抽 `IconNavButton`：`SmsWorkbench/IconNavButton.cs`（`Button` 子类 + `IconGeometry`/`Text` 两 `DependencyProperty`）+ `IconNavButtonStyle` 控件模板（逐字复刻原内联 `Grid/Border/Path/TextBlock`）；16 个按钮（实测 16 非 18）全部改为 `<local:IconNavButton Command="{Binding NavCommand}" CommandParameter=.../>`；`MainWindow.Navigation.cs` 加 `RelayCommand<string> NavCommand` + `OnNavigate` 按参数路由到原 16 个 `Click` handler（同 partial 类可调用，未改 handler 签名）。`dotnet test` 253 passed / 0 failed。
15. ✅ `raw_json` 状态字段提升为独立列：`EXTRA_COLUMNS` 加 `terminal_state TEXT DEFAULT ''` + `connection._ensure_extra_columns` 幂等回填（仅 `terminal_state=''` 行才扫，`lower(status)/lower(error)/lower(raw_json)` 三路 `account_deactivated` 判定，首次后 0 行命中，无复发扫描）+ `idx_accounts_terminal_state` 索引；`upsert_account`/`markers.py` 写入点同步维护；`list_terminal_remail_accounts` 重写为 `terminal_state='account_deactivated'`。**副本演练等价性验证通过**（OLD `SCAN accounts` 51ms vs NEW `SEARCH ... USING INDEX` 1ms；注入 3 合成正例 old=3 new=3 missing=0 extra=0），**生产库已迁移**：列+索引就位，分布 `{active:795}`，新查询返回 0 行（与生产真实数据一致）。回滚：`DROP INDEX idx_accounts_terminal_state; ALTER TABLE accounts DROP COLUMN terminal_state;` + 撤销 4 处源码。
16. ✅ IPC：常驻通道补版本协商（`hello`）/ 结构化错误码 / Python 侧看门狗 / 心跳探测；`DesktopReadClient.cs:359-366` 补 `_pending` 泄漏清理
17. ✅ `services/protocol-payment/`：LICENSE 补权利人 + 上游子目录声明；README 补 Provenance 表 + 反向依赖边界说明；`kakao_extract.py:42` 反向 import 改为可注入 + 惰性回退
18. ✅ 类型化：`account_scan.py` 顶层契约函数已标注；`sub2api_import.py` 37 个函数（含 1 个嵌套 `_run`）全注解覆盖（`from __future__ import annotations` + `Any/Iterable`/`list[...]|None` 等），`tests/test_sub2api_import.py` 14 项测试通过。`PaymentResultDict` 已在 `payment_contracts.py` 定义并被 `to_dict` 使用。

### 第四批：可选
19. 磁盘回收：`.dotnet/` 761 MB + `camoufox_win.zip` 470 MB + 旧 release 176 MB ≈ 1.4 GB
   ⚠️ `runtime/browser_profiles/`（491 MB）**先确认是否承载会话状态再动**
20. `constraints.txt` 接进 CI 或删掉（孤儿锁文件比没有锁文件更误导）

---

## 九、复核说明（本轮推翻的初判）

按上一轮沉淀的方法论，三路 agent 的「推翻」段比结论段更值得看。本轮共推翻 **12 条**初判：

1. **`sms_tool` import `services`** —— 推翻。零 import；关系是**配置化路径 + 子进程 spawn**（`pay_link/adapters.py:157` 读 `protocol_payments.reference_root` 后 `subprocess`）。但换来了更隐蔽的耦合：三个约定（配置项 / 脚本相对路径 / CWD）任一变动，`registry.py:143` **只把 `available` 置 False，静默降级不报错**，7 个支付方式从 UI 消失而无告警
2. **`paypal_extract.py` 是 `services/protocol-payment/*_extract.py` 的副本** —— 推翻。AST 函数名重叠：blik 0/158、ideal 0/138、twint 0/138、kakao 0/67、momo 1/49、pix 1/23。业务域不同，无复制关系
3. **`services/mail-otp-web/` 是 Flask** —— 推翻。用 stdlib `http.server.ThreadingHTTPServer`
4. **`desktop_serve.py` 畸形请求会 NameError** —— **推翻**（agent 报的是错的）。`request_id = 0` 在 `:76` 已预初始化，实测 `handle_request(None)` 正常返回 `{'id':0,'ok':False,...}`。真正的后果是 id=0 无人认领（C# `++_nextId` 从 1 起），不是崩溃
5. **配置在热路径被反复 open+parse** —— 推翻。`current_runtime_config()` 走 ContextVar，`default_runtime_config` 有 `lru_cache(maxsize=1)`，实测 1.2–2.2 ms
6. **循环内 `in list` 大面积 O(n²)** —— 推翻。162 处中只有 9 处是对 list，且都是小规模去重累加器
7. **无界缓存** —— 推翻。0 处无 `maxsize` 的 `lru_cache`，4 处全 `maxsize=1`
8. **可变默认参数** —— 推翻，**0 处**（AST 扫 `defaults` + `kw_defaults`）
9. **`subprocess.run` 在循环里反复起进程** —— 推翻。仅 4 处 subprocess，无一在循环内
10. **`PoolRow` 31 个无通知属性是线上 bug** —— 降级为潜伏风险。实测运行时改写为 0 处
11. **`ProtocolPaymentViewModel.RunAsync` 无 catch 会静默吞异常** —— 降级。`ProtocolPaymentService.cs:196-211` 已把异常全转返回值，当前不抛出
12. **50 个 `+=` vs 3 个 `-=` 是大规模泄漏** —— 推翻。47 处都在短生命周期对话框上随窗口 GC；唯一静态事件 `CompositionTarget.Rendering` 已正确退订

**C# 侧阴性结论（查了但没问题，比问题清单更能说明真实水平）：**
字符串式 `OnPropertyChanged("Foo")` **0**（42 处全 `nameof`）、`static event` **0**、静态可变字段 **0**、
后台线程改 `ObservableCollection` **0**、`AddTransient`/`AddScoped` **0**（故 captive dependency 0）、
组合根之外 `GetRequiredService` **0**、`async void` **0**、主题字典外硬编码颜色仅 **5** 处、
接口抽象率 **14/16 ≈ 88%**、`PaymentBatchService`/`SettingsService`/`ProtocolPaymentService` 构造依赖 **100% 是接口**。

**我自己的实测复核项（未采信 agent 数字，重新跑过）：**
`init_database` 99.9 ms / `get_account_record` 104.0 ms / 795 账号 82.7 s（agent 报 136.5 ms / 108.5 s，同量级）；
`journal_mode=delete`；`EXPLAIN` 三条；`.gitignore` 5 个探针路径；`TaskRow.Info` 定义+绑定+改写三处全中；
`BackendResultInterpreter.cs:69` 拼写；`nodriver` 实测 `ModuleNotFoundError`；`omakse_client.py:550`;
`kakao_extract.py:42` 反向 import；侧边栏按钮实测 **18** 个（agent 报 16）；`init_database` 调用点 **16** 处（agent 报 17）。

---

## 十、第一批落地记录（2026-09-01，14:35–15:05）

改动 5 个文件（`+70 / -7`），两套测试全绿：
**Python `1355 passed + 60 subtests` / 0 失败（4m02s）**、**.NET `235 passed` / 0 失败（4s）**。

### 1. `.gitignore` —— 从「按目录枚举」改成「全局规则 + 单点例外」

枚举是补不完的（08-31 补了 `scripts/`，09-01 实测仍漏 `services/`、`tests/`、`sms_tool/`）。改成：

```gitignore
_*.py
!__init__.py
!/sms_tool/__main__.py   # 已跟踪的合法模块，不能被吃掉
```

**⚠️ 差点引入的回归**：第一版只写了前两行，`git ls-files -i -c --exclude-standard` 立刻报出
`sms_tool/__main__.py` —— 它是已跟踪的 `python -m sms_tool` 入口，被新规则吃掉后**对它的修改会静默停止入库**。
补了第三条例外。
**这是本次唯一一个「改动本身会制造问题」的点，靠 `git ls-files -i -c` 兜住了 —— 改 .gitignore 后必跑这一条。**

实测（两向探针）：
```
_diag_x.py / scripts/_diag_x.py / services/_diag_x.py / tests/_diag_x.py
sms_tool/_diag_x.py / sms_tool/store/_diag_x.py / services/protocol-payment/_diag_x.py
    -> 全部「已忽略 OK」
sms_tool/__init__.py / sms_tool/store/__init__.py / tests/__init__.py
services/protocol-payment/common/__init__.py / scripts/install_git_hooks.py / sms_tool/config.py
    -> 全部「可入库 OK」
git ls-files -i -c --exclude-standard  -> 空
```

### 2. WAL / busy_timeout —— 一半撤销、一半推迟

见第零节第 2 条的修正框。结论：
- `busy_timeout` **撤销**（实测是 no-op，CPython 默认 5000ms；抬高它只会把响亮的失败变成更久的静默卡住）
- WAL **推迟**，必须与第 8 节第 6 项一起落地。判断依据写在 `connection.py` 的注释里（含三组实测数字）

### 3. `TaskRow.Info` —— 反射实测确认修好了

`MainWindow.xaml.cs:326` 从 `public string Info { get; set; }` 改为 `[ObservableProperty] private string info = "";`。
临时反射探针（跑在 `%TEMP%`，未污染仓库）实测：

```
  Info     -> 写入'x_Info'  PropertyChanged=触发 ✅
  Status   -> 写入'x_Status' PropertyChanged=触发 ✅   （既有对照组）
  Name     -> 写入'x_Name'  PropertyChanged=未触发 ❌
  Task     -> 写入'x_Task'  PropertyChanged=未触发 ❌
  Retry    -> 写入'x_Retry' PropertyChanged=未触发 ❌
```

`Name`/`Task`/`Retry` 不触发**是刻意的** —— 它们只在行对象进入 `ObservableCollection` 之前赋值一次，
运行期从不被改写（`PaymentBatchViewModel.cs:474` 的 `row.Name` 是另一个 row 类，不是 TaskRow）。
已在代码里写了注释，防止后人「顺手修好」它们。

### 4. `nodriver` —— 选「声明」不选「删除」

两条路径都是**真实生产调用**，删掉等于砍功能：
- `paypal/orchestrator.py:225` → `run_nodriver_pay`（策略选择的一环）
- `paypal_reverse.py:994` → `solve_captcha_with_nodriver`（CAPTCHA 兜底）

落地：
- `requirements.txt` 加 `nodriver>=0.45`（带注释说明它此前为何一直静默失效）
  实测 `pip install --dry-run -r requirements.txt` 现在能解析出 `nodriver-0.50.3`
- 两处 `except ImportError` 从静默返回改为**显式告警**（纯 ASCII，实测在 `PYTHONIOENCODING=cp1252` 下不炸 ——
  CI 的 stdout 就是 cp1252，中文 print 会 `UnicodeEncodeError`）

⚠️ **未安装进 `.venv`** —— 那是环境变更，留给老板决定。执行 `.\.venv\Scripts\python.exe -m pip install nodriver` 即可启用。

### 5. `lower(email)` 表达式索引 —— 零行为变更，26.7 倍

选了加索引而不是改 19 处查询或改存小写 email：**不动任何业务代码，也不动已有数据**。
`init_database()` 里加一行 `CREATE INDEX IF NOT EXISTS idx_accounts_email_lower ON accounts(lower(email))`
（SQLite 3.43.1，表达式索引自 3.9 起支持）。

同库前后对照（n=120）：

| | EXPLAIN QUERY PLAN | 纯查询中位耗时 |
|---|---|---|
| 无索引（改动前） | `SCAN accounts` | **2.647 ms** |
| 有索引（改动后） | `SEARCH accounts USING INDEX idx_accounts_email_lower (<expr>=?)` | **0.099 ms** |

四种查询形态（`accounts.py:219/297/301`、`markers.py:25/92/167/227`、`normalize.py:80`）全部由 SCAN 转 SEARCH。

⚠️ **别被这个数字误导**：`get_account_record()` 端到端仍是 **105 ms**，因为 99% 的时间花在
`init_database()` 的重放上（第零节第 1 条）。**这个索引的价值要等第 6 项落地后才真正兑现。**

---

**未验证 / 存疑：**
- `registration_audit` 的 `pending 817 == active 817` 是否成对写入 —— 未追踪写入点
- `BackendResultInterpreter.cs:148` 的 `"phone_verification_required" → "支付完成"` 是否笔误 —— 需与需求侧核对
- 82.7 秒是线性外推，未在真实批量任务上端到端计时；多 worker 时全表 UPDATE 互相阻塞，真实开销可能更高
- `runtime/browser_profiles/` 是纯缓存还是承载已登录会话 —— **未确认前不应删除**
- NuGet 包是否过期（Serilog 4.4.0 / WPF-UI 4.3.0 / xunit 2.9.3）未与最新版逐一比对

---

## 十一、第二批落地记录（2026-09-01）

测试对账：**Python 1357 passed + 60 subtests / 0 failed（3m00s）**、**dotnet 237 passed / 0 failed（4s）**。

### 6. `init_database()` 摘出热路径 —— 294 倍

没用 `functools.lru_cache`：**`runtime_config` 通常是 dict，不可哈希**，lru_cache 直接 `TypeError`。
改成按**解析后的库路径**记忆化的手工标志（`_INITIALIZED: set[str]`）：

- 键走 `sms_tool.storage.database_path`（跟 `_connect()` 同一个理由：测试大量 patch 这个符号，
  绑定本地 `connection.database_path` 会让键落在真实库上）
- 双重检查 + `_INIT_LOCK`，失败**不入缓存**（否则半套 schema 会被永久记住）
- 留了 `force=True` 和 `reset_database_init_cache()` 两个逃生口

同进程同库对照（生产库副本 42.3 MB / 795 行，n=12）：

| | 平均 | p95 |
|---|---|---|
| `init_database(force=True)`（旧行为） | **97.434 ms** | 99.501 ms |
| `init_database()`（记忆化后） | **0.333 ms** | 0.426 ms |
| `get_account_record()`（旧，每次重放） | **99.379 ms** | 101.881 ms |
| `get_account_record()`（新） | **1.954 ms** | 2.353 ms |

**第一批那个表达式索引现在才真正兑现** —— 端到端从 105 ms 掉到 1.95 ms。

**语义边界（重要）**：保证是「每进程每库路径一次」。schema 迁移随新代码发布，而新代码意味着新进程，
所以记忆不可能活得比让它失效的那次迁移更久。全仓 17 个调用点，无一需要 `force=True`。

### 7. `StageMatrixStore.Append` —— 33.7 倍

原实现每次 `Append` 都 `File.ReadLines(_path).TakeLast(MaxRecords+1)` 读全文件，超限再整体重写。
`Append` 直接从 `StageMatrixViewModel.Apply` 调用（**UI 线程**），满仓后每个进度事件卡 24 ms。

改成内存缓冲（`List<string>` 存已序列化行）+ 环形裁剪：
- 惰性装载，只在首次 `Append`/`Load` 读一次盘
- 追加只写新增那一行
- 超限才重写，且**裁剪到半仓**（`MaxRecords/2`）而不是刚好 `MaxRecords` ——
  裁到满仓会让「到达上限后的每次追加」都重写一遍；裁到半仓是每 `MaxRecords/2` 次追加才重写一次，**摊还 O(1)**
- 重写前**重新读盘**（不是直接信缓冲）：本壳无单实例保护，另一个进程可能追加过缓冲里没有的行，
  直接按缓冲重写会**静默丢掉别的进程写的数据**

临时基准（已删除，不入库）：

| | 2000 次追加 | 满仓后 200 次追加 | 单次追加（满仓） |
|---|---|---|---|
| 改前 | **21,961 ms** | 4,843 ms | **24.2 ms** |
| 改后 | **652 ms** | 76 ms | **0.38 ms** |

文件大小完全一致（1013.5 KiB），截断语义未变。保留了 `File.AppendAllText` 的同步写（不引入丢数据风险）。
新增 2 个回归测试锁住「不超上限」「重开实例能读到」「上限内不丢行」。

### 8. 合并 `raw_json` 解析 —— 顺手多发现一处

原报告只说了 `:372` 的 `GetPaypalAmount(rawJson)` 是 `:368` 的重复。实测还有一个：
**`:367` 和 `:371` 的 `DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod)` 参数完全相同**，
是同一个纯函数的两次求值。所以每行 3 次 `BackendJson.TextToObject` → 1 次，
外加 1 次 `DisplayPayPalStatus` 冗余。三行局部变量提出即可。

### 9. `account_2fa.py` 补 timeout —— 证伪，0 处真缺口

**这是本轮第二次踩到同一类假阴性**（第一次是 `busy_timeout`）。原文称「全仓 102 处 HTTP 调用中 29 处缺 timeout，
`account_2fa.py` 是唯一真正裸奔的」。实测：

```
curl_cffi 0.16.0  Session()            -> self.timeout = 30
Session.request(... timeout=NOT_SET)   -> timeout = self.timeout if timeout is NOT_SET else timeout
```

用本地「accept 后永不响应」的 TCP server 实测确认（不是读源码猜的）：

```
Session(timeout=2.0).get(url, impersonate="chrome146")  -> 2.01 s 后抛 Timeout (curl 28)
Session(timeout=2.0).get(url)                           -> 2.01 s 后抛 Timeout (curl 28)
Session(timeout=2.0).get(url, timeout=1.0, ...)         -> 1.01 s 后抛 Timeout（逐调用覆盖优先）
Session(timeout=None)                                   -> 无限等待（唯一能真正挂死的方式）
```

于是重扫全仓（AST，排除 `dist/ runtime/ .venv/ tests/`），按**客户端家族**分类 —— 关键不是调用点有没有
`timeout=` 字面量，而是这个库有没有默认超时：

| 家族 | 库默认 | 无 `timeout=` 的调用点 | 通过 `**kwargs` 传入 |
|---|---|---|---|
| curl_cffi | **30 s** | 0 | 7 |
| requests（原生） | **无** | **0** | 5 |
| httpx | 5 s | 0 | 0 |
| urllib | 无 | 0 | 0 |

5 处 `**kwargs` 逐一核对（`momo_qr_extract.py:509/511/1279`、`mailbox_remail.py:269/271`）—— **全部带 timeout**。
`Session(timeout=None)` 全仓 0 处。

**结论：全仓没有任何一处 HTTP 调用会无限挂起。** 那条审计结论作废。

没有去加 9 个 `timeout=30` 字面量（纯 no-op，还会把「库默认值」变成「散落各处的魔法数字」）。
改成加 `tests/test_http_timeout_guard.py`：**按家族判定**，只对「无库默认 + 无 timeout + 无 `**kwargs`」和
「`Session(timeout=None)`」报警，另加一条棘轮断言（`**kwargs` 站点数 ≤ 6，当前 5）。
已用探针文件验证**它真的会失败**（故意造 `requests.get()` 无 timeout 和 `Session(timeout=None)`，
两处都精准命中并给出 `file:line`），探针已删。

### 10. `omakse_client.py:550` —— 顺带修掉一个真 bug

硬编码 `os.path.join(PROJECT_ROOT, "runtime", "accounts.sqlite3")` → 改用 `database_path()`。
默认配置下两者指向同一个文件（已比对确认），但现在配置分片里的 `storage.sqlite_path` 才真正生效。

**额外发现**：那句查询是 `WHERE email=?` —— **大小写敏感**，而全仓其他所有 email 查询都是
`lower(email)=lower(?)`。用不同大小写的 email 调 `extract_links_for_account()` 会拿不到 token、
报「No access token found」。已一并改成大小写不敏感。

验证脚本 5 项全过（精确大小写 / 全小写 / 全大写都命中；未知 email 干净报错；
改 `database_path` 后查找确实跟着走 —— 最后这条才是原 bug 的判据）。

### 11. `account_lifecycle.py:79` —— 连接泄漏

`with sqlite3.connect(db) as conn:` 里的 `with` 是**事务**上下文管理器，不是资源管理器：
它负责 commit/rollback，**从不关闭连接**。每次删号泄漏一个句柄 + 一个文件锁，直到 GC。
改成 `with closing(sqlite3.connect(db)) as conn:` 外层管关闭、内层 `with conn:` 保留原来的
commit-on-success / rollback-on-error。全仓 `with sqlite3.connect(` 仅此一处（已扫）。
顺带把 `conn.close()` 挪出成功路径 —— 原来异常时也漏。

---

**第二批的坑清单**
1. **`functools.lru_cache` 用不了** —— `runtime_config` 是 dict，不可哈希。手工记忆化 + 路径做键。
2. **记忆化的键必须走被 patch 的那个符号**（`sms_tool.storage.database_path`），否则测试直接失联。
3. **AST 扫描器自己的 bug 制造了 5 个假阳性**：`**kwargs` 在 `keywords` 里（ `kw.arg is None`），
   不在 `args` 里。第一版只查了 `args`，把 5 个合规调用报成了缺 timeout。
4. **AST 扫描还会把 `session.get(...)` 当成 HTTP 调用** —— 很多 `session` 其实是 dict
   （`session_refresh.py` / `k12_identity.py` / `desktop_read.py` 等 6 个文件全是 dict.get）。
   按「变量名导入来源」判定家族能过滤掉大部分，但这类误报要靠人工看一眼。
5. **裁剪到半仓而不是满仓** —— 差一个常数，复杂度从「满仓后每次 O(N)」变成「摊还 O(1)」。
6. **重写前重新读盘** —— 无单实例保护时，直接按内存缓冲重写会吃掉别的进程的数据。
