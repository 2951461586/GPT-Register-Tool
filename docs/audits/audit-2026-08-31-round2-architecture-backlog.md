# 第二轮深度审计 — 架构 / 并发 / 文档 / 测试有效性

- 审计基线：`6fc56c0`（2026-08-31 强推后），519 个跟踪文件
- 审计范围：`sms_tool/`(179 py) + `services/protocol-payment/`(7 渠道 15,690 行) + `SmsWorkbench/`(56 cs) + `SmsWorkbench.Contracts/`(16 cs) + `docs/`(32 md) + `tests/`(120 py) + `.github/workflows/`
- 方法：三路并行取证（Python 架构与并发 / C# 异步与 IPC / 文档与测试工程化）+ **主 agent 对 10 条高危结论独立复核**
- 已排除：`dist/`、`runtime/`（源码副本，**未排除时 .py 从 216 涨到 1088，5 倍污染**）、`__pycache__`、`bin/`、`obj/`

> **与上一轮的关系**：本轮是 `docs/audit-2026-08-31-cleanup-backlog.md`（清理 backlog：死代码 / 重复实现 / import 环 / 磁盘 / 凭据）的**补集**，专注于**架构设计、并发与状态、文档体系、测试有效性、工程化**。上一轮条目不复述。

---

## 零、最关键的 7 条（按「会不会真出事」排序）

| # | 问题 | 位置 | 后果 | 复核 |
|---|---|---|---|---|
| 1 | 并发门用 `ContextVar` 记录持有者，但跑在线程池里 | `registration_concurrency.py:46,87-89` | **配额失效 + 跨账号释放信号量** | ✅ 实测确认（见下） |
| 2 | 常驻 Python 进程重定向了 stderr 却从不读取 | `DesktopReadClient.cs:307` | 缓冲区满 → 写阻塞 → 界面全空数据 | ✅ grep 确认 0 读取点 |
| 3 | 配置分片直接覆盖写，无 temp+replace、无备份 | `config.py:79-86` | 崩溃即留下截断 JSON，全站不可用 | ✅ 源码确认 |
| 4 | 并发上限只对一半路径生效 | `PaymentBatchService.cs:38` / `ProtocolPaymentService.cs:36` | 2 个 Python 进程同写 sessions/SQLite | ⚠️ 未独立复核 |
| 5 | `store/` 持久化层 1,439 行零测试 | `store/{accounts,connection,normalize,markers,checkpoints,constants}.py` | 数据损坏不可发现 | ✅ 6/6 模块测试命中 0 |
| 6 | 5 个渠道抽取器 12,990 行零测试 | `blik`(3795) `ideal`(3200) `twint`(3187) `pix`(1634) `direct_card`(1174) | 唯一花钱的路径裸奔 | ⚠️ 抽样确认 |
| 7 | README 的国家/支付方式清单与实际配置**双向**失配 | `README.md:214,224` vs `payment_methods.json` | 用户按文档配，配不出结果 | ✅ 实测发现比初判更严重 |

### 关于第 1 条的实测（本轮唯一动用运行时验证的结论）

```python
cv = contextvars.ContextVar('x', default=None)
def task(i):
    prev = cv.get()      # 读上一个任务残留
    cv.set(i)
    return (i, prev)
# 单 worker: [(0, None), (1, 0), (2, 1), (3, 2)]
# 双 worker: [(0, None), (1, 0), (2, 1), (3, 2)]   ← 两个 worker 也残留
```

`ThreadPoolExecutor` 的 worker 线程复用同一个 context，`submit` 不做 `copy_context()`。
落到本仓：`enter_registration_stage()`(:87) 读到上一个账号残留的 gate → `:88` `if held[0] == group: return 0.0`
**直接跳过获取**；随后任一方的 `release_registration_stage()`(:123-134) 会释放**对方**的信号量，
`BoundedSemaphore` 许可数被抬高，`ValueError` 在 :133 被吞。`_DEFAULT_CAPS`（auth=1 / network=4 / payment=2）形同虚设。

---

## 一、P0：会静默出错或导致故障

### 1.1 并发门失效（见上文实测）

- `registration_concurrency.py:46` — `_held_gate` 用 `ContextVar`，但 `batch_runner.py:265` 用 `ThreadPoolExecutor`
- 附带：`_rate_limit_blocked_until`(:53) 是**进程级**全局，单账号收到 429 → `mark_registration_rate_limited()`(:56) 让**整个进程**所有账号的 auth 组（配额 1）停摆最长 3600 秒。单账号限流被放大成全进程熔断。

**修法**：返回显式 token 上下文管理器（`with enter_registration_stage(s) as token:`），或退化 thread-local + 强制 `try/finally`。限流态按账号/批次维度隔离，不做进程级。

### 1.2 常驻通道 stderr 未读

- `DesktopReadClient.cs:307` `RedirectStandardError = true`，但全类**无任何** `StandardError` 读取点（grep 命中仅 :307 开关、:310 编码、:365 只读 stdout）
- Python 侧持续 stderr 输出 → 缓冲区（约 4–8 KB）满 → **写端阻塞** → 所有读请求在 :344 的 120s 超时后失败；`GetOrStartResident` 又因 `IsAlive`(:262) 为真不重启 → 界面持续空数据
- 对照 `PythonBackendClient.cs:58-59` 同时泵 stdout/stderr，写法正确 —— 说明是漏不是不懂

### 1.3 状态持久化非原子写（21 处直接覆盖）

| 类别 | 位置 | 风险 |
|---|---|---|
| 配置分片（最致命） | `config.py:79-86` | 3 个分片是全站配置，截断即不可用；**无 `.bak`** |
| 渠道代理状态 | `blik:591` `ideal:505` `twint:498` 直接写；`kakao:348-349` 却用了 temp+replace | **同族 4 个文件两种写法**；且这些是并发子进程，进程内 RLock 跨进程无效 |
| 会话 / token 文件 | `account_recovery.py:865` `account_scan.py:459` `codex_oauth.py:892` `codex_export.py:79/91/304` `session_refresh.py:104` `commands/mailbox_ops.py:77` `commands/one_click.py:184` `store/normalize.py:364` | 含 access/refresh token，崩溃即截断 |
| 手机号池游标 | `phone_reuse.py:128` | 无锁 + 直接覆盖 → **同一号码派给两个账号** |
| canary 状态 | `payment_batch.py:986` 直接写；同文件 :926 却用 temp+replace | 同文件内两种写法 |

**已有正确范式可复用**：`payment_operation.py:225` `_atomic_write`（uuid4 临时文件 + `replace`）、`paypal_proxy.py:404-406`、`store/accounts.py:362-365`（且先 `read_bytes()` 留备份）。

### 1.4 并发上限被绕过

- `BackendTaskCoordinator.cs:36` 用 `BackendTaskAlreadyRunningException` 强制单任务
- 但 `PaymentBatchService.cs:38`、`ProtocolPaymentService.cs:36` 注入的是 `IBackendClient` 而非协调器；`MainWindow.PaymentBatch.cs:14` 开窗口前不查 `IsRunning`
- 结果：主窗口跑注册批次时打开「批量协议支付」→ 2 个 Python 进程，共用同一 `sessions/` 与 SQLite

### 1.5 单例无锁 + 文件锁覆盖率仅 3 处

- `paypal_proxy.py:599` `_PAYPAL_PROXY_STATE_CACHE` 无锁（该文件 7 处 lock 全在 `PayPalProxyState` 内部）。竞态 → 同一 key 建两个实例 → 各自 `os.replace` 同一文件 → 计数互相丢失
- `CrossProcessSemaphore` 全项目仅 **3 处**使用（`payment_batch.py:166`、`payment_operation.py:125`、`registration_concurrency.py:178`）；配置分片、`sessions/`、`runtime/paypal_proxy_state.json`、各渠道 `proxy_state.json` 全部裸奔
- `browser_fingerprint_pool.py:192` 与 `fingerprint_pool.py:219` 各有一份同名 `_SHARED_POOLS` + 各配一把锁 = **两份互相独立的池**，两条入口的账号不共享指纹配额

---

## 二、P1：架构债

### 2.1 支付渠道无抽象层 —— 最大的一块债

| 文件 | 行数 | 引用 `common/protocol_core` |
|---|---|---|
| `blik/blik_qr_extract.py` | 3,795 | 是 |
| `ideal/ideal_qr_extract.py` | 3,200 | 是 |
| `twint/twint_extract.py` | 3,187 | 是 |
| `momo/momo_qr_extract.py` | 2,046 | 否（自有 `ac_paylink_core.py` 851 行） |
| `kakao/kakao_extract.py` | 1,493 | 否 |
| `direct_card/direct_card_extract.py` | 1,174 | 否 |
| `pix/pix_extract.py` | 795 | 否（自有 `pix_core.py` 839 行） |

合计 **15,690 行**，共享层 `common/protocol_core.py` 仅 **315 行（2.0%）且只有 3/7 引用**。
跨 7 文件同名符号出现 ≥4 次的 **23 个**：`stripe_init`(5) `create_checkout`(5) `stripe_update_tax_region`(4) `proxy_for_country`(4) `new_session`(4) `load_token`(4) `save_proxy_state`(4) `redact_log_text`(4) `is_user_already_paid_error`(4) `env_bool`(4) `env_int`(4) …

**成本**：新增渠道 = 复制 1,000–3,800 行 + 改 `pay_link/adapters.py` / `pay_link/base.py` / `payment_catalog.py` 至少 3 个注册点。
对照：浏览器驱动层**有抽象**（`registration_drivers/base.py` + 6 个 driver），说明这层是能做好的。

### 2.2 幂等与补偿只覆盖了一半

- 支付侧设计完整：`payment_contracts.py:31-32` 的 `operation_id` / `idempotency_key_hash`，`payment_operation.py:197` `_replay_allowed()`、`PaymentOperationConflict`、原子写
- 但引用方**只有** `pay_link/*` 与 `payment_link_manager`。**注册 → 绑卡链路没有等价机制**：`commands/registration.py:192` 的"幂等"是结果落盘去重，不阻止"已提交但结果未知"时的重复提交
- 更矛盾：`mailbox_remail.py:291-300` 用**稳定**幂等键（注释明确写"a retry reuses the same Idempotency-Key rather than double-charging"），而同文件 :480 的批量下单用 `uuid.uuid4()` 每次新键 = **零去重**
- **0 处业务级补偿**：grep `rollback|compensat|undo_|_on_failure` 只命中 SQLite 的 `conn.rollback()`。已创建的 checkout 不撤销、已占用的手机号不归还

### 2.3 重试与异常层级碎片化

- **33 处手写** `for … in range(...)` 重试，参数来源五花八门：`phone_registration.py:295` 硬编码 `range(6)`、`mailbox_gmail.py:248` `range(2)`、`commands/payment.py:557` `range(1,3)`；配置驱动的只有 `payment_batch.py:344/378/419`。**无 tenacity/backoff，requirements 亦未声明**
- **838 处 `raise` 中 `RuntimeError` 占 363 处（43.3%）**，自定义异常仅 31 个且分散在 27 个文件、无公共基类。调用方无法区分「可重试 / 需人工介入 / 幂等冲突」，只能 catch 所有

### 2.4 `MainWindow` 是"切文件"不是"拆类"

- 18 个 partial / 5,065 行 / 约 247 个方法，但 **40 个字段中 37 个声明在 `MainWindow.xaml.cs:5-53`**（✅ 实测前 60 行确为 40 个字段声明）
- 跨文件字段读写 **118 处**（Pools 31 / Helpers 28 / Tasks 15 / Navigation 13 / SmsBower 9 …）；最散的字段 `desktopRead`（6 文件）、`rootDir`（6 文件）
- 判定：**假拆分**。物理分页 ≠ 职责分离

**建议的拆分边界（按实测字段亲和性，不按文件现状）**：

| 目标类 | 收拢字段 | 收拢 partial |
|---|---|---|
| `PoolWorkspace` | `allRows`/`PagedRows`/`currentPage`/`filteredCount`/`accountSort*`/`SelectedRow`/`poolsRefreshRunning` | Pools + Navigation + ContextMenu + Detail |
| `BackendTaskHost` | `backendTasks`/`backendClient`/`taskSeq`/`doctorProbeStarted`/`lastHotPersistenceRefreshUtc` | Tasks + Helpers(Log/Notify) |
| `AccountOperations` | `desktopRead`/`settingsService`/`rootDir` | Export + Register + SmsBower + Payment |
| `WindowChromeHost` | `_currentTheme`/`sidebar*`/`themeIconGeometry` | Theme + Sidebar + WindowChrome |

第一刀建议切 `PoolWorkspace`：只碰 9 个字段、48 处跨文件引用，其余三类都读它，风险最低。

### 2.5 异步：无 `async void`、无 `.Result`，但 31 个 async 方法 0 个 `CancellationToken`

- ✅ 干净项：`async void` **0 处**；`.Result` / `.Wait()` **0 处**；`ObservableCollection` 变更 17 处全在 await 之后的 UI 续体中，无后台线程直改
- ❌ `MainWindow.*.cs` 中 `async Task` 方法 **31 个，接受 `CancellationToken` 的 0 个**。长任务唯一取消来源是 `MainWindow.Tasks.cs:375` 的按钮，关窗/切任务都不能停。契约层与 Service 层其实支持（`PythonBackendClient.RunAsync:28`、`PaymentBatchService.RunAsync:266`），只是没接到 UI
- ❌ fire-and-forget 2 处：`App.xaml.cs:41`（有 try/catch，安全）、`MainWindow.Pools.cs:39`（**无兜底**，异常只被 `App.xaml.cs:59` 的 `OnUnobservedTaskException` 吞掉）。项目已有正确范式 `MainWindow.Helpers.cs:8` `RunUiTask`，这两处没走
- ❌ `MainWindow.Pools.cs:44-45` 刷新在忙时**静默丢弃**（不排队不合并），批次期间的热刷新很可能整批被丢，界面停在旧快照；且 :49 无条件 `allRows.Clear()` 会重置用户已勾选的 `IsChecked`

### 2.6 契约层双向对账：死命令 0 条，未接线能力 110 个 flag

- C# 发出的 73 个 flag 在 `cli.py` 中**全部有定义**（2 个疑似项 `--incognito`/`--new-window` 经查是 Chrome 参数，误报）
- 反向：`cli.py` 181 个 flag 中 **110 个（61%）C# 从未发出**，按能力簇：omakse 支付 20、sub2api 导入 12、代理变体 14、remail 采购 7、gmail-send 6、工作区扫描 6、PayPal BA 队列 6、CPA 配额 4、注册驱动/模式 4

> **判断**：CLI 能力多于 GUI 本身正常，但 omakse / sub2api / Gmail 代发 / BA 队列这几块**已实现的后端能力完全没有入口**，且无 GUI 调用方 = 无端到端验证 = 易腐化。要么补入口，要么明确标注为「仅 CLI」。

- schema 有版本但**静默降级**：`BackendJsonProtocol.cs:23-29` 校验 `version==2` && `schema=="smsworkbench.ipc.v2"`，不匹配时 :35-36 直接落回 legacy 扫描（尾部找 `{` 强解），**无日志无告警**。升级 v3 时会表现为"用旧解析器读新数据"
- `<Nullable>` 停在 `annotations` 而非 `enable`，空检查全关；`!` 抑制符 4 处

### 2.7 配置耦合：27.4% 的文件在函数体内直连全局配置

- `sms_tool` 下 **49/179 文件（27.4%）** 在缩进内直接读 `CFG.get` / `load_merged_config()`；34 个文件直接 `from .config import CFG`
- 更细的问题：配置被**反复重读**而非注入。`phone_proxy.py:78` 每次调用重新合并 12 个键；`pay_link/adapters.py:95/237/372/434/536` 每进一个渠道适配器重新解析一次 timeout。同批次内不同账号可能走不同参数
- 无依赖注入 → 多租户/多配置并行只能靠进程隔离

### 2.8 可测试性：决策与副作用未分离

98 处 `time.sleep` 中 **51 处（52%）集中在 5 个文件**：`paypal/form_steps.py`(20)、`paypal/flow_steps.py`(10)、`registration_drivers/playwright.py`(8)、`account_2fa.py`(7)、`captcha_solver.py`(6)。

典型耦合点：
- `mailbox_poll.py:61-83` 真实时钟轮询（`time.time()` + `while` + `sleep`），测"OTP 超时"要真等一个周期 —— 而 OTP 解析本身（`mail_otp.py`）0 处 IO/sleep，**脏的只是这层壳**
- `phone_reuse.py:114-128` 业务操作内联 `save_state()` 落盘
- `payment_batch.py:419-461` 重试循环内联真实支付请求，transport 不可注入

**正面对照**：`payment_flow.py` 与 `payment_executor.py` 是纯决策层（0 处 IO/sleep/网络），可完全脱离环境单测 —— 说明**分离可行，只是没铺开**。

---

## 三、测试有效性

### 3.1 覆盖盲区（✅ 已独立复核）

| 区域 | 行数 | 测试 | 后果 |
|---|---|---|---|
| `store/` 持久化层（6 模块） | 1,439 | **0** | 账号去重 / 邮箱归一化 / 断点 / 建表全裸奔 |
| `pay_link/`（5 模块） | 1,768 | **0** | 支付链接子包整体无测试 |
| `commands/`（6 模块） | 1,196 | **0** | 与「131 个 flag 零文档」是同一批 |
| `paypal/`（5 模块） | 1,178 | **0** | 唯一实际花钱的路径 |
| `blik`/`ideal`/`twint`/`pix`/`direct_card` 抽取器 | **12,990** | **0** | 仅 momo、kakao 有真实单测 |
| `paypal_link/{reconciliation,gen_link}` | 2,560 | **0** | 对账是金额一致性最后一道防线 |

`services/protocol-payment` 7 个渠道中 **5 个零测试**。注意：`tests/test_payment_link_manager.py:352-418` 的 blik/ideal 断言测的是 **sms_tool 侧对子进程 stdout 字符串的解析**（拼 `BLIK_RESULT:{...}` 字面量喂给 `CompletedProcess`），**不是抽取器本身**；`test_account_liveness.py:138-144` 对抽取器的引用是 `rglob` **静态文本扫描**。

### 3.2 测试「量够浅」

- 1,250 个测试函数，断言分布：断言 ≤2 的 **616 个（49.3%）**，其中恰好 1 条的 307 个（24.6%）
- **3 个零断言**的测试函数：`test_config_runtime.py:15`、`test_payment_proxy_health.py:85`、`test_proxy_routing_config.py:15`
- ✅ 正向：skip 仅 1 处（`test_sentinel_runner.py:70`，Node 缺失，理由正当）、xfail 0 处；网络/浏览器/子进程**全部打桩**，默认离线
- ❌ **无 `conftest.py`**：无共享 fixture、无全局 mock 层、无自定义 marker、无 `--strict-markers`；120 个测试文件中 **35 个（29.2%）完全不使用任何 mock**，各文件自己 `sys.path.insert`
- ❌ `tests/fixtures/` 仅 4 个 JSON，覆盖 3/15 个支付方式

### 3.3 C# 侧

- ✅ 断言风格健康：**0 处 `Mock.Verify`**（772 条断言全是值断言），占位断言仅 32/772（4.1%）且均为深度断言前的空守卫
- ❌ **唯一 spawn Python 的 `PythonBackendClient` 0 测试**（进程启停、超时杀树、stderr 脱敏、载荷提取）
- ❌ `MainWindow` 唯一测试是 `DesktopWindowSmokeTests.cs:491` 的**单个** `[Fact]`：695 行 / 115 条断言挤在一个用例里，任一环节失败整体红；靠反射 `Invoke` 私有方法（:319,:370,:398,:461），**改名即变 `NullReferenceException` 而非编译错误**

---

## 四、文档债

### 4.1 `docs/` 是四种性质文件的混装

| 类别 | 数量 | 处置 |
|---|---|---|
| Release note（不可变快照） | 20 | → `docs/releases/` |
| 审计/一次性快照 | 6 | → `docs/audits/`，其中 3 份浏览器审计主题重叠且结论矛盾，合并为 1 份封存 |
| 设计/架构活文档 | 5 | 保留，但需拆分去重 |
| 索引 | 1 | 重建（当前只收录 5/32，最新 release 指针停在 `v2026.08.09`，实际已有 `v2026.08.31`） |

6 份快照占 `docs/` 总行数 **31.8%** 但按定义永不更新，与活文档混排导致「文档是否在描述当前实现」无法判断。

### 4.2 `architecture.md` 是垃圾抽屉

1,140 行中 `## Boundary Rules`(:215-1042) 独占 **828 行 = 72.6%**，塞进 30 个 `###` 子节，从运行时配置一路到 WPF / 支付 / 测试 / 存储 / 已废弃清单，**无主线**。

**已过时内容（「文档说 X」vs「代码实际 Y」）**：

| 位置 | 文档说 | 代码实际 |
|---|---|---|
| `:766` | `It only runs when the user requests --one-click-pay` | ✅ 复核：`--one-click-pay` **不存在**，全树只有 `--one-click-scan` / `--one-click-sms` |
| `:986` | `Run all tests with: python -m unittest discover -s tests` | `pytest.ini`、`README.md:578`、CI 全是 `python -m pytest -q`；`tests/` 有 85 个文件用 `pytest.mark`/`monkeypatch` |
| `:990` | `sms_tool/storage.py owns: SQLite schema creation and migrations` | ✅ 复核：`storage.py` 仅 **8 行**，是 `store` 子包兼容壳；建表在 `store/connection.py:45,82,105` |

**语义重叠 3 组**：代理路由（`architecture.md:309-401` 是 `registration-and-proxy-architecture.md:97-181` 的**劣化副本**—— 前者 `lane` 命中 0、`fingerprint` 3，后者 11/19）、PayPal 授权（`:879-928` ↔ `protocol-payment-enhancement.md:1-67`）、Checkout 契约（`:667-705` ↔ 同文档 :140-179）。逐字重复仅 1 处，危害是「同一契约两份口径」。

**拆分方案**（按「谁的边界」切，详见 A.2 表）：保留 235 行骨架 → 拆出 `boundaries/runtime-config.md`(105) / `registration.md`(175) / `payment.md`(350) / `desktop.md`(150)，代理 93 行并入 `registration-and-proxy-architecture.md`，运维约定 70 行并入 `directory-map.md`。

### 4.3 文档—代码一致性：三处系统性失配

**① 环境变量覆盖率 6.4%**（✅ 复核：直接 `os.getenv` 读取 92 个，加上 `external_sessions.py:30-79` 的 `env_overrides` 表间接读取，去重后约 140 个；文档仅提及 9 个）
典型遗漏恰是**凭据类**：`ROXY_API_TOKEN`、`CLOAK_LICENSE_KEY`、`ADSPOWER_USER_ID` —— 用户无法知道「可以不落盘、改走环境变量」这条规避路径。

**② 配置分片零文档**：`config.py:26-58` 的 `SHARD_OWNERSHIP` 共 **29 个顶层键**归属在任何 md 中 0 次出现；`README.md` 与 `README_EN.md` 对三个分片文件的提及次数均为 **0**。分片改造是 08-30 做的、08-31 发布的 —— **用户第一入口文档完全不知道分片存在**。
（附带澄清：`sensitive_policy.json` 不是分片，是 `sanitizer.py:22` 加载的脱敏策略，文档里与之并列提及易误读为 4 片。）

**③ 中英 README 严重不同步**

| 中文 `README.md`（646 行 / 12 节） | 英文 `README_EN.md`（83 行 / 7 节） |
|---|---|
| 项目架构（111 行） | **缺整章** |
| 核心配置（115 行） | **缺整章** |
| 常用操作（73 行） | **缺整章** |
| 测试、构建与发布（54 行） | 仅 3 行命令 |

英文版 83 行 = 中文版 **12.8%**，缺失 4 个整章共 353 行（54.6%）。

### 4.4 数字失配 —— 复核发现比初判更严重 ✅

`README.md:214` 列 **12 个**支付方式，`payment_methods.json` 实为 **15 个**：缺 `qris`(ID) / `bizum`(ES) / `naver_pay`(KR)（均为 canary）。`README_EN.md` **完全未列任何支付方式**。

`README.md:224` 列 11 个目标出口国 `US JP VN ID IN NL BR KR PL CH PH`，实际 `checkout_countries` 为 **13 个** `US ID JP TR TH VN PH IN GB DE ES KR BR`。这是**双向**失配：

- 文档有、实际无（**幽灵国家**）：`NL` 荷兰、`PL` 波兰、`CH` 瑞士 —— 3 个
- 实际有、文档无：`TR` `TH` `GB` `DE` `ES` —— 5 个

用户按文档配 NL/PL/CH 会直接配不出结果。

### 4.5 有命令无文档：131/181（72.4%）

缺口呈**整族性**而非零散：Omakse 20 个、SUB2API 11 个、配额/扫描 11 个、改邮箱 7 个、Gmail 发送 6 个。
另有 **4 个完整 argparse 入口零文档**：`paypal_link/gen_link.py:456-461`(1255 行)、`direct_card_extract.py`、`momo/run_momo.py`、`pix/run_pix.py`；6 个 `scripts/` 运维脚本零文档。

反向（有文档无命令）经逐条核验**仅 1 条真实失配**：`--one-click-pay`（其余 30 个候选为 git/dotnet 工具 flag、URL 分隔符误匹配、或存在于第二套 argparse）。

**另一个功能缺口**：`cli.py:248` `--registration-driver` choices 只有 5 个，**漏了 `adspower`**（✅ 复核确认）—— 而 `registration_drivers/base.py:11-15` 枚举 6 个、`config.py:415` 与 `external_sessions.py:78,700-754` 均已实现 adspower。CLI 选不了，文档也没指出。

---

## 五、工程化

### 5.1 CI 缺 10 个关键环节（✅ 全文复核）

`.github/workflows/ci.yml` 仅 34 行、单 job、13 步。做了的：环境预检、两个自建扫描、`RestoreLockedMode` 锁 NuGet。缺的：

1. **无 `timeout-minutes`** —— 卡死耗到 GitHub 6 小时硬上限
2. **无 `concurrency` 组** —— 同一 PR 连续 push 并发跑满队列
3. **无 .NET 缓存** —— `setup-dotnet@v5` 未设 `cache: true`（Python 侧设了 `cache: pip`）
4. **无 lint / 格式门禁** —— 无 ruff/black，`dotnet format --verify-no-changes` 也没有；分析器告警未转失败
5. **无安全扫描** —— 仓库自带的 `scan_hardcoded_secrets.py`、`scan_sensitive_history.py`、`precommit_guard.py`(322 行) **一个都没接进 CI**
6. **无覆盖率** —— 52 个零测试模块在 CI 上完全不可见
7. **无 Python 版本矩阵** —— 仅 3.12，而 README 声明「3.10 或更高」
8. **无 artifact 上传 / smoke test** —— 构建成功只证明「能编译」
9. **无 release workflow** —— 打 tag、写 note、上传 3 个资产全手工
10. **无 job 拆分** —— `dotnet test` 失败会阻断后续所有门禁

### 5.2 版本号无单一来源

- 4 个 csproj 全无 `<Version>`/`<AssemblyVersion>`/`<FileVersion>`（grep 命中 0）
- `global.json:3` 的 `10.0.300` 是 **SDK 版本**，不是应用版本
- `scripts/build_installer.ps1:21` 默认版本取**当天日期** `v$(Get-Date -Format 'yyyy.MM.dd')` —— 同日两次构建产出同名不同内容的文件
- **26 个 tag vs 20 份 release note**：8 个 tag 无 note（`v2026.06.14` `v2026.07.03/04/12/17/22/23/24`）、2 份 note 无 tag —— 其中 **`v2026.08.31`（含配置分片改造的最近一次发布）写了 note 却没打 tag**
- `README.md:617-625` 的 7 步发布检查清单中，5 步可自动化，**0 步自动化**

### 5.3 依赖治理两侧标准不一致

- ✅ .NET 侧：集中版本管理 **100% 合规** —— `Directory.Packages.props` 声明 10 个版本，4 个 csproj 中 `Version=` 硬编码命中 **0**；`RestorePackagesWithLockFile=true` + `RestoreLockedMode`；NU1605/NU1901-1904 设 `WarningsAsErrors`（**依赖漏洞硬失败**）
- ❌ Python 侧：13 个依赖中仅 `curl_cffi==0.16.0` 精确锁定，1 个有上界，**11 个只有下界 `>=`**；无 lock / constraints / 哈希；`playwright>=1.40.0` 无上界意味着浏览器驱动可能与 SDK 漂移

### 5.4 可观测性是最弱一环

- Python 侧 **534 处 `print()`**，仅 **19/179 模块 import logging**（✅ 复核）；`services/` 下 print 32 处、**logging 使用 0 处**（17,934 行协议支付代码无一条 logging）
- **零轮转零落盘**：`RotatingFileHandler`/`basicConfig`/`FileHandler`/`logging.config` grep 命中 **0**。C# 侧引入了 Serilog + Sinks.File，但 Python 子进程 stdout 只能整块捕获 → 两侧割裂
- **零崩溃上报、零健康检查、零运行指标**（`token_telemetry.py` 是 AT 指纹哈希，非运行时指标）
- **162 个错误码中仅个位数有文档解释**，典型不可操作示例：
  - `playwright.py:188` `browser_proxy_blocked` —— 无 detail，`__str__` 只输出裸码，用户无法判断该换代理 / 换出口国 / 还是被判机房 IP
  - `account_health_queue.py:75` `RuntimeError("account_health_queue_full")` —— 不带当前长度、上限，也不说该等还是调大并发
  - `codex_oauth.py:826` `RuntimeError("oauth_state_mismatch")` —— 无法区分并发撞车 / 时钟漂移 / 回调被改写
  - 对照正向案例：`manual_challenge_required` 在 `README.md:140` 有详细解释 —— **说明文档能写清楚，只是没覆盖到**

---

## 六、建议执行顺序

### 第一批（会真出事，1–2 天）

1. `registration_concurrency.py` 的 `_held_gate` 改显式 token 上下文管理器（**实测确认的 bug**，先写个复现测试锁住）
2. `DesktopReadClient.cs:307` 常驻通道加 stderr 泵（唯一会致死的 C# 缺陷）
3. `config.py:79` `_write_shards` 改原子写 + 写前留 `.bak`
4. `PaymentBatchService` / `ProtocolPaymentService` 走 `IBackendTaskCoordinator`；`MainWindow.PaymentBatch.cs:14` 补 `IsRunning` 检查
5. `cli.py:248` 补 `adspower` 到 choices（改代码）或文档标注（二选一）

### 第二批（正确性兜底，1 周）

6. 给 5 个零测试抽取器补契约级单测（照 `test_kakao_extract.py` 的 `sys.path.insert` 模式，先覆盖 `protocol_payment.v1` 输出契约与脱敏，不碰网络）
7. 给 `store/` 6 个模块补测试（1,439 行持久化层，数据损坏当前不可发现）
8. `phone_reuse.py:128` 加文件锁 + 原子写（同一号码派两个账号是真实资损）
9. `_PAYPAL_PROXY_STATE_CACHE` 加 `_cache_lock`；跨进程写路径复用 `CrossProcessSemaphore`
10. `BackendJsonProtocol.cs` 版本不匹配改为「记 Warning + 不静默降级」；`PythonBackendClient.cs:81` 解析异常单独归类为「响应解析失败」而非「启动失败」

### 第三批（降本，2–4 周）

11. 收口 `common/protocol_core.py`：先把 ≥4 份的 23 个同名符号（`stripe_init` / `create_checkout` / `save_proxy_state` / `proxy_for_country` / `env_*`）统一
12. 把 `payment_operation.py` 的幂等边界推广到 `commands/registration.py`；`mailbox_remail.py:480` 改稳定幂等键
13. 抽 `MainWindow` 第一刀 `PoolWorkspace`（9 字段 / 48 处引用，风险最低）
14. 31 个 async 方法补 `CancellationToken`，接窗口 `Closing`
15. print → logging 迁移（534 处 / 54 模块）+ `RotatingFileHandler`；建「错误码 → 用户可操作建议」映射表

### 第四批（工程化，可并行）

16. CI 补 `timeout-minutes` / `concurrency` / dotnet `cache: true` / `pytest --cov`，并把仓库已有的 3 个 secrets 扫描脚本接进去
17. 版本号单一来源（`Directory.Build.props` 加 `<Version>`，`build_installer.ps1:21` 从 tag 读）；补 `v2026.08.15.1`、`v2026.08.31` 两个缺失 tag
18. 拆 `docs/architecture.md`（828 行抽屉 → 235 行骨架 + 4 份边界文档）；20 份 release note + 6 份审计移入 `docs/releases/`、`docs/audits/`
19. 修 `README.md:214,224` 的支付方式（12→15）与国家清单（双向失配）；补 29 个分片键与配置分片说明
20. Python 依赖加 `constraints.txt` 或锁文件

---

## 七、本轮方法论沉淀

- **ThreadPoolExecutor + ContextVar 是一类隐蔽反模式**：worker 线程复用 context，`submit` 不做 `copy_context()`。查法是「全局态用 ContextVar 但消费方是线程池」—— 两者单独看都对，放一起就错。这类 bug 只能靠运行时实测，grep 看不出来。
- **文档数字失配是双向的**：初判写成「多 TR、ES」，复核发现是「3 个幽灵国家 + 5 个缺失国家」。核对外延型清单（国家/渠道/支付方式）必须**两个方向都算差集**。
- **测试覆盖盲区要区分「有引用」和「真测到」**：`test_payment_link_manager.py` 里 blik/ideal 的断言测的是 sms_tool 侧对 stdout 字符串的解析，不是抽取器本身。按文件名判断覆盖会高估。
