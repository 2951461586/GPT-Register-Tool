# 第五轮深度审计 — 代码质量 / 解耦 / C# 剩余面 / 测试安全 / 文档

- 日期：2026-09-02
- 基线：`d0813a6`（第四轮第 12–18 项落地后）
- 规模：Python 生产代码 **199 个 .py / 80,048 行**；C# **97 .cs + 5 .xaml / 13,812 行**；539 个跟踪文件
- 方法：五路并行取证（Python 代码质量 / 架构解耦 / C# WPF / 测试与安全 / 文档体系）+ 主 agent 对**全部 P0 与高危 P1 逐条独立复核**
- 排除：`dist/`、`runtime/`、`scripts/installer/`、`.venv/`、`__pycache__/`、`sessions/`、`**/bin`、`**/obj`
  （不排除则 .py 从 199 涨到 1088，grep 结论 5 倍污染）

## 与前四轮的关系（本轮是补集）

| 轮次 | 主题 |
|---|---|
| 一轮 | 死代码 / 重复实现 / import 环 / 磁盘 / 凭据泄漏 |
| 二轮 | 并发 / 配置耦合 / 幂等 / 测试有效性 / CI |
| 三轮 | 发布出口 / 数据正确性 / 资源生命周期 / 交付工程 |
| 四轮 | 性能 / WPF 前端 / SQLite / IPC / 依赖 |
| **五轮（本轮）** | **代码级质量 / 解耦边界 / C# 剩余面 / 测试安全卫生 / 文档体系** |

分报告：
- `audit-2026-09-02-round5-python-quality.md`
- `audit-2026-09-02-round5-decoupling.md`
- `audit-2026-09-02-round5-csharp-wpf.md`
- `audit-2026-09-02-round5-tests-security.md`

---

## 零、最关键的 5 条（按「会不会真出事」排序）

### 1. 🔴 P0 脱敏漏网 6 类 —— 实测坐实 ✅ 主 agent 独立复核

本项目唯一发生过真实泄漏事故的领域（2026-08-31 凭据前缀进发布包），最后一道防线有洞。
用假值 `ZZFAKE123456` 实测 `sms_tool/sanitizer.py`：

| 输入 | 实测输出 | 判定 |
|---|---|---|
| `{'api_key': ...}` | `[REDACTED]` | ✅ 对照通过 |
| `{'session_token': ...}` | `[REDACTED]` | ✅ 对照通过 |
| `{'password': ...}` | `[REDACTED]` | ✅ 对照通过 |
| `{'cookie': ...}` | **明文** | ❌ 漏 |
| `{'session_id': ...}` | **明文** | ❌ 漏 |
| `{'smsbower_api_key': ...}` | **明文** | ❌ 漏 |
| `?access_token=...` | `[REDACTED]` | ✅ 对照通过 |
| `?token=...` | **明文** | ❌ 漏 |
| `set-cookie: session=...` | **明文** | ❌ 漏 |
| `https://user:pw@host` | `[REDACTED]@host` | ✅ 对照通过 |
| `{"proxy": "user:pw@host"}` | **明文** | ❌ 漏（非 URL 形态） |

**根因**：`sensitive_policy.json` 的 `sensitive_keys` 只列了**完整键名**（`cookie_header`、`session_token`、
`api_key`），`sensitive_key_fragments` 只有 8 个（`token`/`secret`/`password`/`card_number`/
`cardnumber`/`card_last4`/`authorization`/`license_key`）。

**荒谬之处**：`cookie_header` 在名单里、裸 `cookie` 不在 —— 而 `cookie` 才是本项目最核心的凭据载体
（代码里 60+ 处 cookie 标识）。同理 `session_token` 在、`session_id` 不在。

**改法**：`sensitive_key_fragments` 增补 `cookie`、`session_id`、`key`、`proxy`、`set-cookie`；
`text_patterns` 的 `named_secret` 正则增列裸 `token` 与 `session_id`。
**必须配套写 pytest 用例**（见第五节 G1/G2），否则下个新增字段名继续漏。

### 2. 🔴 P0 595 处静默吞异常，其中 82 处「返回空值伪装成功」

AST 全量统计，拆三类（这个区分前几轮没有）：

| 类型 | 数量 | 说明 |
|---|---|---|
| `except: pass` | 216 | 纯静默 |
| **完全丢弃异常对象** | **242** | 既不 log 也不再抛 |
| **`str(e)` 进返回值、零日志** | **82** | **最危险：失败被伪装成成功** |

最危险的形态在**花钱和解析路径**上：
- `services/protocol-payment/direct_card/direct_card_extract.py:568` → `return {}`（解析失败伪装成"解析成功但无数据"）
- `kakao_extract.py:213,757` → `return ''`
- `momo_qr_extract.py:228` → `return (None, None)`
- `sms_tool/paypal/dom_fields.py:56,61,72,85` → DOM 改版后所有选择器静默失配，统一返回 `False`

`dom_fields` 那条尤其危险：PayPal 前端一改版，所有字段定位全落空，上层看到的是"填表成功"，
实际啥也没填。这类 bug 不会报错、不会告警，只会体现在成功率下滑上。

**改法不能批量机械做**（595 处里混着大量合理的"探测失败就跳过"）。
建议：**先只处理 82 处「返回空值伪装成功」**，逐个改成 log + 带原因的失败标记。

### 3. 🟠 P0 C# UI 线程硬等 IPC 最长 120 秒 ✅ 主 agent 独立复核

`SmsWorkbench/MainWindow.Register.cs:756-757`：

```csharp
return desktopRead.ReadMailboxLineAsync(...)
    .GetAwaiter().GetResult().Trim();
```

内部有 `ConfigureAwait(false)` 所以**不会死锁**，但超时由 `DesktopReadProtocol.cs:39` 定为 **120 秒**。
后端 Python 卡住时整个主窗口无响应 2 分钟，用户只会以为程序死了。

**改法**：`FindMailboxLineFromBackend` 改异步签名，调用点 await。

### 4. 🟠 P1 PayPal 资金主干 6,361 行零测试

比前几轮报的"5 个抽取器"更严重 —— 抽取器好歹补了 `tests/test_extractors_contract.py`
（152 行 / 12 用例，docstring 自述 *"only pure helpers and the shared reporter are exercised"*，
1.3 万行解析逻辑仍无行为测试）。而**真正动钱的 PayPal 主干一行没测**：

| 模块 | 行数 |
|---|---|
| `paypal_link/gen_link.py` | 1,255 |
| `paypal_reverse.py` | 1,145 |
| `captcha_solver.py` | 755 |
| `paypal/orchestrator.py` | 477 |
| `paypal/form_steps.py` | 432 |
| `paypal/dom_fields.py` | 429 |
| `paypal/flow_steps.py` | 355 |
| `pp_link_helpers.py` | 330 |

**已核实闭环**：`store/` 已有 `tests/test_store_modules.py`；测试已改 `tmp_path`，不再往 `runtime/` 落文件。

### 5. 🟠 P1 Python ↔ C# 有 512 个字面量两边各抄一份

| 重复逻辑 | Python | C# | 共享字面量 |
|---|---|---|---|
| **CLI argv 规划** | `cli.py`（969 行） | `BackendCommandPlanner.cs`（**644 行**） | **50 个 flag** |
| **账号状态解释** | `store/normalize.py:268,333` | `AccountStatusInterpreter.cs`（398 行/25 方法） | **46 个** |
| 配置分片归属 | `config.py:121,29,36` | `ConfigStore.cs:89,18,29` | 21 个 |
| IPC 信封 | `desktop_ipc.py:10,57,19` | `BackendJsonProtocol.cs:9,33` | 前缀 3 + schema 4 |
| 支付状态 | `pay_link/normalize.py` | `ProtocolPaymentExecution.cs` | 38 个 |
| 邮箱行解析 | `mailbox_parsers.py:235` | `MailboxCredentialLineParser.cs` | 分隔符 + 域名表 |
| 代理归一化 | `phone_proxy.py:103` | `ProxyInputNormalizer.cs:30` | 同算法 |

**复制粘贴铁证**：账号状态里有个 typo `account_deatived`（应为 deactivated），
Python 和 C# **两边各错一份、错得一模一样**。

**改法**：抽 `contracts/*.json` 两边共用 —— 照抄项目**已经做对**的范式：
`payment_methods.json` 就是 `payment_catalog.py:14` ↔ `PaymentMethods.cs:102` 共用的。
优先做 CLI 参数表（644 行的 C# planner 全靠它）。

---

## 一、Python 代码质量

### 1.1 ideal / twint 是同一文件两份副本（94.5% 相同）

`services/protocol-payment/ideal/` 与 `twint/` 归一化渠道名后仅 **175 / 3197 行不同**。
`create_checkout`(102 行) 相似度 **0.999**、`run_provider_flow`(172 行) **0.979**。

> ⚠️ **修正上一稿**：初判 Jaccard 1.00 是**低估**（token 级 0.936 / difflib 0.918 看着像部分克隆，
> 但归一化渠道名后是 94.5% 相同）；而 blik↔ideal 的 0.81 是**高估**（实为 0.602，只有
> `create_checkout` 1.000 该合，编排层已分化到 0.196）。
> 初判把 momo/kakao 计入冗余是**错误归类**（Jaccard 仅 0.02–0.10，非克隆）。

另发现：`blik_qr_extract.py:2195` 定义了 `stripe_confirm_ideal` 并在 `:2928` 调用 ——
**一个文件里并存两个渠道的确认实现**。

### 1.2 53 组全等克隆函数（最大仅 47 行）—— 最低垂的果实

token 级全等的克隆函数 **53 组**，最大 47 行，总计约 **1,705 行纯搬运冗余**。
纯机械提取公共函数，风险极低。

### 1.3 118 个超大函数（LOC≥80 且 CC≥15 且非数据表）

| 函数 | 位置 | 行数 / 圈复杂度 |
|---|---|---|
| `run_payment_batch` | `payment_batch.py:80` | **493 / CC=155** ✅ 复核 |
| （浏览器注册主流程） | `registration_drivers/playwright.py:1559` | 457 / CC=117 |

`playwright.py` 整个文件 **2,032 行、扇出 20**。

### 1.4 硬编码：URL 88 个 / 402 次，无 endpoints 模块

- 跨 ≥5 文件的 URL 有 16 个：`chatgpt.com` 出现在 **30 个文件**、`api.stripe.com/v1/payment_pages` 在 **10 个文件 / 34 次**
- 超时魔法值：`DEFAULT_TIMEOUT = 30` 在 **10 个文件各定义一遍**
- 全仓库**压根没有 endpoints 模块** → 属"该建未建"
- 对照：`payment_catalog.py` 被 16 个文件 import，但 `paypal` 仍在 **29 文件 / 66 次**硬编码 → 属"建了没用满"

### 1.5 其他（P2）

- `print()` **591 处 / 67 文件**（库代码里应走 logging）
- logger 仅 16 处；`proxy_pool.py:25` 与 `start_proxy_pool.py:23` **同名 logger 会串扰配置**
- f-string 进 logger 3 处：`account_2fa.py:261,346` 把邮箱 PII 写进 INFO 日志
- 旧式 typing（`List`/`Dict`/`Optional`）**57 文件 / 81 处**
- UTF-8 BOM 2 个文件（见第四节方法学坑）

**健康项**：全量扫描 logging 参数，**未发现** token/password/cookie/Authorization 头进日志
（15 条初筛均为 `authenticated` 等词的子串误报）。

---

## 二、架构与解耦

### 2.1 7 个 import 环仍在 —— 靠「把 import 挪进函数体」糊住，不是解开

80 个模块共 **319 处函数级延迟内部导入**（`registration_handlers` 22、`account_recovery` 19、
`cli` 19、`commands/payment` 17 居首）。

| 环 | 规模 | 关键边 |
|---|---|---|
| **A** | 11 | `payment_link_manager:48,49 → pay_link` ↔ `pay_link/*:4 → payment_link_manager` |
| **B** | 7 | `storage.py:7,8 → store` → `store/accounts:143 → mailbox_remail:165 → storage` |
| **C** | 4 | **`config.py:179 → registration_drivers.external_sessions._driver_config`** ✅ 复核 |
| D | 3 | `payment_catalog:193,194 ↔ checkout_contract:10 / payment_flow:9` |
| E | 3 | `account_liveness:80 ↔ registration_drivers.playwright:16` |
| F | 3 | `sentinel.__init__:3 → sentinel.client:330 → sentinel_tokens:405` |
| G | 2 | `payment_routing:216,241,406,432 ↔ paypal_proxy:695` |

**C 号环最刺眼**：`config.py` 是**扇入 64 的全仓总线**，它反向 import 了浏览器驱动子包里的
**私有符号** `_driver_config`。这不是分层倒置，是把总线的针脚焊进了外设。

**A 号环是纯人造伤**：`pay_link/` 7 个子模块每个第 4 行都 `import sms_tool.payment_link_manager as _plm`，
共 **24 处 `_plm.*` 调用**。其中 `_plm.subprocess`（adapters:40,48,60）、
`normalize:244 _plm.subprocess.TimeoutExpired` **纯属多余** —— 直接 `import subprocess` 就行。
7 个子模块还共用一段**逐字节复制**的 18–26 行 import 序言。

### 2.2 God module TOP（扇入 = 隐式总线）

| 模块 | 扇入 | 模块 | 扇出 |
|---|---|---|---|
| `config` | **64** | `registration` | 27 |
| `storage` | 26 | `registration_drivers/playwright` | 20（2032 行） |
| `paths` | 26 | `cli` | 19（969 行） |
| `sanitizer` | 24 | `account_recovery` | 17 |
| `phone_proxy` | 23 | `pay_link/{adapters,core}` | 各 17 |

**耦合积异常值**：`payment_link_manager` **121** —— 全文只有 49 行，却有全仓第二高耦合积，
纯粹因为它是 A 号环的枢纽。

### 2.3 全局可变状态：总体健康，5 处裸奔

44 个模块级锁/信号量单例，25 个被原地修改的模块级可变绑定中 **20 个有锁**。真无锁的：

- `services/protocol-payment/momo/momo_qr_extract.py:35 COUNTRY_CURRENCY`（公开名，`:36 setdefault` + `:2016 assign`）
- `sms_tool/desktop_read.py:140,141,176` 三个缓存 —— **check-then-act**：`:149 clear()` 紧跟 `:150 assign`，无锁
- `sms_tool/mailbox_chongzhi.py:29 _last_fetch_ts`（`:176`）

`global` 语句 12 模块 / 23 处，重灾区 `cli.py` 6 处。

**两个伪全局值得说明**：
- `config.py:648 CFG` 实为 `ContextVar`（`:243`）支撑的 `LegacyConfigView`，**不是真全局**，
  但把 37 模块 / 65 处调用钉死在 config 形状上
- `pay_link/base.py:166 CFG: dict[str, Any] = {}` ✅ 复核 —— 子包里**第二个** CFG，
  **零导入零修改 = 死全局，直接删**

### 2.4 缺接口：「加一个实现要改 N 处」

**已有正确范式（照抄即可）**：
- `sms_provider.py:20 SmsProviderAdapter(ABC)` —— 2 实现 + 工厂 `:736`
- `payment_catalog` 数据驱动 + `PaymentAdapterRegistry`（8 适配器）

**邮箱适配器 = 5 处**（比已知线索多 1–2 处）：
`mailbox.py:24-38` 导入 → `mailbox.py:67-192` 两次 register → `mailbox_parsers.py:245-256` 6 分支链
→ **`mailbox_parsers.py:299-326` 同样的 6 分支链逐行复制粘贴** → `mailbox_strategies.py:183` 硬编码排除集。
外加 `account_email_change.py:119-123` 又一份独立 4 分支。

9 个实现方证明契约真实存在 → 应定义 `MailboxProvider`：
`key` / `aliases` / `match(mailbox)` / `parse_pool_line(...)` / `fetch_messages(...)` / `poll_otp(...)` / `create_mailboxes(...)`

**注册驱动 = 6 处**：`base.py:8-14` enum（含 `ADSPOWER` 但**无对应模块**）→ `:18-24` frozenset 再抄
→ `:26-52` 别名集 → `cli.py:248` argparse choices 再抄 → `config.py:448` 再抄
→ `external_sessions.py:78` env 表 + `:881-893` if 链 + 4 个 `*_BrowserSession` 类。
且 `camoufox`/`cloak`/`roxy` 三个 12 行近复制 shim，`adspower` 却没有。
→ 定义 `BrowserDriver`：`key`/`aliases`/`env_spec()`/`validate()`/`open_session()`/`run_registration()`

**其他同构**：`protocol-payment/{blik:1719, ideal:1262, twint:1255}` 同一个 `promo_mode` 4 分支链复制 3 份；
`cli.py:448` 5 分支；`pay_link/adapters.py:133` 对 `spec.key` 的 4 分支 env 注入（14 个协议脚本各一套 env 契约、全仓无声明）。
**`providers/` 半迁移**：9 个邮箱 provider 只搬了 2 个。

### 2.5 子系统内聚度（哪些能独立出去）

| 子包 | 出边 | 入边 | 内聚 | 结论 |
|---|---|---|---|---|
| `services/protocol-payment` | **1** | 0 | 0.00 | **已事实独立**，再拆无收益 |
| `sms_tool.sentinel` | **7** | 9 | 0.46 | **最容易独立**，断 F 环即可 |
| `sms_tool.paypal` | 14 | 2 | 0.89 | 10 条反向父壳依赖下沉后即可 |
| `sms_tool.store` | 13 | 1 | 0.88 | 卡在 B 号环 |
| `sms_tool.paypal_link` | 74 | 2 | 0.93 | 内聚最高但出口最宽（含 8 私有符号） |
| `sms_tool.registration_drivers` | 34 | 12 | 0.35 | 需先建注册表 + 拆 2032 行 playwright |
| `sms_tool.pay_link` | **179** | 1 | 0.55 | **最差**，必须先断环 |
| `sms_tool.commands` | 76 | 3 | 0.16 | 纯编排层，本就该薄 |

---

## 三、C# / WPF 剩余面

### P1
1. **16 组魔法字符串导航**（`Navigation.cs:12-31` ↔ `MainWindow.xaml:461-483`）——
   XAML 的 `CommandParameter` 与 C# 的 `case` 是两份字面量，改一处漏一处则**按钮静默失效**；
   还伪造 `new RoutedEventArgs()` 去调 Click 处理器。
2. **`SetScope("邮箱池"/"已注册")` 筛了等于没筛**（`Navigation.cs:133,135` vs `Pools.cs:17-18`）——
   设置的筛选值 `FilterRow` 根本不识别。
3. **6 个死事件处理器**（`Navigation.cs:34,105,131,133,135,137`）—— 一轮删 13 个死方法时漏网的。
   ⚠️ 别连带删：`AddSessionFileArg`/`SessionFileFor`/`SelectedEmailRowOrNotify` 仍有活引用。
4. **UI 层异常全以 Information 级别、不带异常对象落盘**（`Helpers.cs:399-415`）。
   全仓 13 处 Serilog 调用，12 处在两个 backend client，MainWindow 只剩 2 处。
5. **`AccountStatusInterpreter`（21.8 KB，含 3 个裸 `catch`）零测试**。
6. **`SensitiveDataSanitizer` 零测试** —— 脱敏唯一入口，漏正则 = 明文进 `runtime/app_.log`（**保留 14 天**）。
7. **`UnobservedTaskException` 静默吞掉**（`App.xaml.cs:59-63`）—— fire-and-forget 异常表现为"点了没反应"。

### P2
- **XAML 重复**：9–10 个图标 `<Path>` 共享 7 个属性、6 个分页 Path、4 张统计卡整块复制
  （含冗余双层 StackPanel）、`IconNavButtonStyle` 重复 16 次、`AccountContextMenuItemStyle` 重复 10 次。
  **附带 copy-paste bug**：`:600`「清空」与 `:615`「全选」用了**相同 Path**。
- **C# 手搓 UI**：94 处 `FindResource("...")` + 176 处 `new 控件`，资源键是魔法字符串，改 key 只在运行期炸。
- **6 个未使用 `x:Name`**：`MaxIcon`(351)、`SidebarHost`(405)、`BatchPaymentButton`(468)、
  `ChangeEmailButton`(472)、`SettingsButton`(481)、`App.xaml:428`。
  ⚠️ `Minimize/Maximize/CloseButton` **不能删**（`Click=` 在下一行，g.cs 靠字段名连线）。
- **两个生产项目都是 `<Nullable>annotations</Nullable>`**（测试项目反而是 `enable`）——
  这正是 `NavCommand { get; } = null!;` 能混过编译的原因。
- `MainWindow` 是 DI 单例、`_lifetimeCts` 只 Cancel 不 Dispose、`NavCommand` 在 `DataContext` 之后赋值
  （靠绑定延迟求值侥幸生效）。

### 已复核通过、不重复报 ✅
全局异常处理**已完整**（`App.xaml.cs:24-26`）；`DispatcherPriority` 全量 3 处**全是 Background，无 Render**；
库层 `ConfigureAwait(false)` 齐全；`SemaphoreSlim` 未释放是**有注释的 deliberate 决策**；
无 `#pragma` 滥用；`ConfigStore` 空桶 bug 已被测试锁住。

### C# 测试
192 Fact + 15 Theory，无 Skip。配置合并 / IPC 序列化 / 支付批处理**都已覆盖**；
缺口是 `AccountStatusInterpreter`、`SensitiveDataSanitizer`、`ProtocolPaymentViewModel`。
**未发现断言错误行为的测试**。
⚠️ `DesktopWindowSmokeTests` 有 8 处反射调私有方法 —— 重命名私有成员会让测试**静默变绿**。

---

## 四、测试质量与安全卫生

### 4.1 测试卫生（好消息先说）

1372 用例全绿 / 93.5s。**0 空测试、0 无理由 skip、0 裸 `pytest.raises`、0 try/except 吞测试体、
sleep 仅 4 处、无真实网络**。问题只在**覆盖率盲区**（见第零节第 4 条）。

- **A3** 安全闸门脚本自身零测试：`scan_release_payload.py`(191) / `scan_hardcoded_secrets.py`(138) / `sensitive_field_scan.py`(117)
- **A5** 3 个无断言测试；**A6** 16 处弱断言
- **A7** 103 个测试 patch ≥ 4（最重 31 个），验证的是编排状态机而非真实浏览器交互

### 4.2 安全卫生

**危险 API 全干净** ✅：`eval` / `exec` / `os.system` / `pickle` / `yaml.load`(非 safe) / `subprocess(shell=True)`
**全部 0 命中**；`__import__` 4 处全字面量；`chmod` 仅 0o600 / 0o755；生产代码 0 处动态路径拼接。

真正的坑是**脱敏覆盖度**（见第零节第 1 条，已实测坐实 6 类漏网）。

### 4.3 依赖与仓库工程

- **C1** `nodriver` 未进 `constraints.txt`，且是**函数内延迟 import** —— 装完不报错，
  **PayPal 兜底路径触发时才炸**
- **C2** `selenium` 全仓 **0 引用**（纯僵尸依赖）、`httpx` 0 实际使用；
  **反向检查：代码 import 但未声明的依赖 0 个**（此前 17 条疑似缺失是 `sys.path.insert` + 裸导入的第一方模块误报）
- **C3** `config.example.json` 漂移：56 个键 example 有而本地无（含三条 `proxy.*_pool` 泳道），61 个键反之
- **C5 仓库卫生健康** ✅：539 个跟踪文件，`sessions/`(1114) / `runtime/`(21216) / `dist/` **全部 0 入库**；
  根目录 7 个凭据载体均 `tracked=no & ignored=yes`

---

## 五、文档体系（主 agent 本轮亲扫）

### 5.1 核心问题：8 个后拆子包，架构文档基本没跟上 ✅

`docs/architecture.md`（**1140 行**，最后更新 2026-08-29）与 `docs/directory-map.md`（144 行）
对子包的覆盖度：

| 子包 | architecture.md | directory-map.md |
|---|---|---|
| `sms_tool/paypal/` | 5 次 | **0** |
| `sms_tool/paypal_link/` | 1 次 | **0** |
| `sms_tool/commands/` | 1 次 | **0** |
| `sms_tool/providers/` | **0** | 1 次 |
| `sms_tool/store/` | **0** | **0** |
| `sms_tool/sentinel/` | **0** | **0** |
| `sms_tool/pay_link/` | **0** | **0** |
| `sms_tool/registration_drivers/` | **0** | **0** |

`store/` 是**扇入 26 的持久化核心层**，架构文档一次没提。
`pay_link/` 是全仓内聚最差、最该被理解的子包（179 出边），也是 0 次。

### 5.2 文档引用失效：149 个源码路径引用中 19 个已不存在

多数是示例路径（`_diag_x.py`、`endpoints.py` 是"建议新建"）或历史文档里的正常记录
（`architecture.md:895` 是在说明 `paypal_links.py` **已被移除**，属正确记载）。

真漂移 2 处：
- `scan_headless_browser_proxy_fingerprint_2026-08-29.md:115` 写 `SmsWorkbench/BackendCommandPlanner.cs`，
  实际在 **`SmsWorkbench.Contracts/BackendCommandPlanner.cs`** ✅

### 5.3 文档结构

37 篇 docs 中 **26 篇是 release notes**（`release-v*.md`），5 篇审计，6 篇设计/评估。
release notes 全是手写、没有生成脚本 —— 与 `scripts/build_installer.ps1` 是两条平行流程，易漏写。

---

## 六、本轮的方法学坑（值得单独记）

### UTF-8 BOM 让 AST 工具静默失效 ✅ 复核

```
sms_tool/phone_proxy.py    扇入 23   ← 前 3 字节 ef bb bf
sms_tool/codex_oauth.py    扇入  8   ← 前 3 字节 ef bb bf
```

用默认 `utf-8` 做 `ast.parse` 会**静默**抛 `invalid non-printable character U+FEFF`。
**此前任何用默认编码跑的分析工具，对这两个模块的结论都是错的**（本轮用 `utf-8-sig` 修复后多出 27 条 import 边）。

对 Python 解释器运行**无影响**（tokenizer 正确处理 BOM），只影响工具链。
建议清掉，成本近乎为零。

### Grep 工具的 `.gitignore` 不生效

`obj/` 虽在 `.gitignore` 内，Grep **仍会扫到 `MainWindow.g.cs`**。C# 那一路改用显式白名单
（102 文件）重做了全部搜索才拿到正确结论。

---

## 七、落地路线图

### 第一批：纯机械、低风险、可一次做完

| # | 动作 | 收益 |
|---|---|---|
| 1 | **补 `sensitive_policy.json` 脱敏项** + 写 pytest 守卫（G1/G2） | 堵住 6 类实测漏网，本项目唯一有事故史的领域 |
| 2 | 处理 82 处「返回空值伪装成功」（逐个 log + 失败标记） | 消除"解析失败伪装成功" |
| 3 | 提取 **53 组全等克隆函数** | 减约 1,705 行，最大仅 47 行，纯搬运 |
| 4 | 删 `_plm` 24 处改直接导入 + 断 B/F 号环 | 解开 2 个环、降 A 号环复杂度 |
| 5 | 删死全局 `pay_link/base.py:166` | 1 行 |
| 6 | `desktop_read.py` 三缓存加 Lock | 消 check-then-act 竞态 |
| 7 | 合并 `mailbox_parsers` 两条复制链（`:245-256` / `:299-326`） | 少一处同步点 |
| 8 | 清 2 个 UTF-8 BOM | 修好工具链静默失效 |
| 9 | C#：删 6 个死处理器 + 6 个 `x:Name` + 抽 `MenuIconPath`/`ToolbarIconPath` 样式 | 低风险 |
| 10 | C#：两个生产项目 `<Nullable>` 改 `enable` | 让 `null!` 不再混过编译 |
| 11 | `nodriver` 进 `constraints.txt`；删僵尸依赖 `selenium`/`httpx` | 装完不会漏 |
| 12 | `C# FindMailboxLineFromBackend` 改异步 | 消除 UI 硬等 120 秒 |

### 第二批：需设计决策，分阶段

1. **建 3 个 Protocol + 注册表**：`MailboxProvider`（消 5 处硬编码名单）、
   `BrowserDriver`（消 6 处）、`ProtocolScriptAdapter`（消 14 套 env 契约）
2. **断 C 号环**：`config.py:179` 反向 import 私有符号 —— config 扇入 64，动它能同时消两条边，需谨慎
3. **拆 `registration_drivers/playwright.py`（2032 行）**：建议先建注册表再拆
4. **Python ↔ C# 契约 JSON 化 + CI drift 检查**：优先 CLI 参数表（50 个 flag）
5. **补 PayPal 主干 6,361 行的测试**
6. **补 `docs/architecture.md` 的 8 个子包章节**

### 明确不值得做

- `paths.py`（26 行扇出 0）和 `sanitizer.py`（106 行扇出 0）是**健康叶子**，不要包 façade
- `sms_tool/__init__.py`（1 行）拆它零收益
- **D 号环**（`payment_catalog:193,194`）是故意的启动顺序设计，**加注释即可**
- `services/protocol-payment` 已事实独立（出边 1 / 入边 0），再拆无收益
- `pay_link`/`commands` 的高扇出是**机械拆分未收尾的症状**，不要单独为降扇出而动
- 595 处异常吞噬**不可批量机械处理**（混着大量合理的"探测失败就跳过"）
- 超大函数拆分**先补测试再动**
- `unittest` 139 个 TestCase + 2655 断言是既定风格，不要迁移

---

## 七之二、第一批落地记录（2026-09-02 当晚）

改动 **19 个文件 / +244 −97**，`pytest` = **1454 passed, 0 failed**（基线 1372，净增 82 个守卫用例）。

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 补脱敏策略 + pytest 守卫 | ✅ | fragments +7、`bare_proxy_credentials` 正则、新增 `safe_key_paths`；G1/G2 两个测试文件共 82 用例；**变异测试 7/7 被测出** |
| 2 | 清 2 个 UTF-8 BOM | ✅ | 各 −3 字节，diff 仅首行；`ast.parse(utf-8)` 通过 |
| 3 | `nodriver` 进 constraints | ✅ | 补 `nodriver==0.50.3` + `pytest-cov==7.1.0`，删僵尸 `selenium`；依赖双向 15=15 |
| 4 | 删 `_plm` 24 处 + 断 A 环 | ✅ | 35 处替换、0 残留；6 个子模块全部独立导入且不再拉起壳 |
| 5 | 断 F 环 | ✅ | `sentinel_tokens` 改直接 import `sentinel.client` |
| 6 | 断 B 环 | ⛔ **刻意保留** | 见下 |
| 7 | 处理 82 处伪装成功 | ⏸ 叫停 | 见下 |
| 8 | 提取 53 组克隆函数 | ⏸ 叫停 | 见下 |

### 落地中修正的三条结论

**① `selenium` 才是僵尸，`httpx` 不是。**
`pip show httpx` → `Required-by: cloakbrowser`。它是传递依赖，requirements 里显式 pin
是为了锁住这条闭包，**保留**。`selenium` 的 `Required-by` 为空 + 代码/文档 0 引用 → 真僵尸，已删。
（第四轮报告把两者并列判为僵尸，`httpx` 那条是错的。）

**② 脱敏加了 `safe_key_paths`：过度脱敏会损坏功能。**
给 `sensitive_key_fragments` 加 `session_id` 后，`proxy_affinity.session_id` 被脱敏 →
`_restore_session()` 拼出 `sid-[REDACTED]` 的坏代理凭据，**静默连不上**。
`sanitizer.sanitize()` 因此新增 `path` 参数与 `safe_key_paths`（点分路径后缀豁免）。
守卫用例 `test_path_exemption_is_surgical_not_global` 锁定两个方向都红：
移除豁免 → 代理重建坏；放宽到裸键 → `session_id` 全局泄漏。

**③ C# 侧只实现 `text_patterns` + `sensitive_options`，没有键名脱敏。**
所以补 fragments 只影响 Python；而新增的 `bare_proxy_credentials` 正则两边都生效。
C# 内嵌的策略就是根目录那一份（csproj `EmbeddedResource ..\sensitive_policy.json` + LogicalName），无需同步副本。

### 🔴 本轮最大的教训：兼容壳 = 测试的 patch 注入面

删 `_plm` 后 **8 个测试红**，根因不是改错，是测试依赖壳的一层间接性：
`patch.object(manager, "_state_path")` 生效的前提，是子模块通过 `_plm.X` **在调用时重读壳的属性**。
子模块一绑定本地 import，patch 就**静默失效** —— 单独跑仍绿，只有全量跑才因状态串扰而红。

同一模式在本仓出现三次：`pay_link` 的 `_plm`（已改，retarget 42 处）、
`sentinel/__init__`（已改，2 处）、`store/connection.py` 走 `storage` 壳取 `database_path`
（**7 个测试文件依赖，刻意保留**，已加 `DELIBERATE REVERSE DEPENDENCY` 注释）。

**规则：任何"消除壳间接层"的重构，必须同步把测试 patch 目标从壳改到实际使用点。**

### 叫停的两项及理由

**「处理 82 处伪装成功」** —— 595 处异常吞噬混着大量合理的"探测失败就跳过"，
逐个人工判断才可改，不属于机械批量范畴。建议后续按模块分批做，每批配套补测试。

**「提取 53 组克隆函数」** —— 实测 87 组（比初判多 34 组）分三档：

| 档 | 组数 | 冗余行 | 判断 |
|---|---|---|---|
| **PURE**（只依赖 builtin/import） | 21 | 285 | 可安全机械提取 |
| **CONST**（依赖各脚本自己定义的函数） | 58 | 1429 | 提取 = 连带搬迁依赖链，是**重构**不是机械提取 |
| **STATEFUL**（引用模块级可变状态） | 8 | 248 | 提取会让 4 个脚本**共享** `_proxy_redaction_lock` 等状态，**改语义** |

可机械提取的只有 285 行 = 全仓 0.36%，而改动对象是**零行为测试覆盖的付费路径**
（1.3 万行渠道解析逻辑至今无行为测试）。**建议先补测试再动**。
分析产物留在 `F:\tmp\audit5\clone_groups.json` / `clone_buckets.json`。

---

## 七之四、第二批落地记录（2026-09-02 收口 2 项）

老板拍板：第二批 6 项里**先收口「建注册表」相关的 2 项**（BrowserDriver + ProtocolScriptAdapter）；
playwright.py 拆分、契约 JSON 化、PayPal 主干测试、`docs/architecture.md` 子包章节推迟（见第七之二 / 七）。

**改动文件（相对 `71c5479`，本批真实改动）：**

| 文件 | 落地内容 |
|---|---|
| `sms_tool/registration_drivers/base.py` | 新增 `BrowserDriverSpec` + 单一真相源 `DRIVERS` 字典；派生 `BROWSER_REGISTRATION_DRIVERS` / `KNOWN_DRIVER_ALIASES` / `driver_choices()` / `normalize_registration_driver()`（行 33/48/77/83/86/102） |
| `sms_tool/registration_drivers/external_sessions.py` | 新增 `_BROWSER_SESSION_FACTORIES` 注册表 + import-time 断言 `assert set(_BROWSER_SESSION_FACTORIES) == (BROWSER_REGISTRATION_DRIVERS - {"playwright"})`；playwright 单独 `PlaywrightBrowserSession(**kwargs)`（行 774/781/817） |
| `sms_tool/pay_link/adapters.py` | `ProtocolScriptAdapter` 用 `_PROTOCOL_BUILD_ENV` 字典 + 5 个 `_build_*_env` 函数替换 `_run_protocol_script` 内的 `spec.key` if-chain（pix 仍显式分发，因代理走环境而非 seed 文件；未知 key 返回错误并 `unlink` proxy 文件）（行 84/95/99/122/132/139/187/194） |

**验证：** `pytest` = **1858 passed / 6 skipped / 77 subtests**（修 `.git` 后比 1454 基线净增，零回归）；`dotnet test` = **253 passed**。

**遗留（非本批，记入 backlog）：**

- `tests/test_payment_result_contract.py:137` 仍 patch `payment_link_manager.subprocess.run`，
  但 `_run_extractor_subprocess` 在子包拆分时已挪到 `adapters.py`，真实调用点是 `adapters.subprocess.run`。
  全量跑绿、定向跑红（patch 目标错位，非本批回归）。需改 patch 目标到实际使用点。
- 第二批其余 4 项推迟（见第七之二 / 七）。

**文档同步 — `Nullable` 收尾状态（属 09-01 第三批，本轮补记）：**

- `SmsWorkbench.Contracts.csproj`：`<Nullable>enable</Nullable>`，**零警告**。
- `SmsWorkbench.csproj`：显式保留 `<Nullable>annotations</Nullable>`（2026-09-02 实测翻 `enable`
  会报 **147 处** nullability findings / ~25 文件，多数需改签名 + 读上下文，非机械编辑；
  半改会引入 ~380 条警告噪音，比不检查更糟）。决策：**按文件用 `#nullable enable` 渐进迁移**，不一次性翻。

---

## 八、建议常驻的自动化守卫（写成 pytest 用例，不动 workflow）

> 本项目 CI 的 gh token 缺 `workflow` scope —— **加检查必须写成 pytest 用例，不能改 workflow**。

| 优先级 | 守卫 | 作用 |
|---|---|---|
| **G1** | 脱敏行为用例（用假值断言 11 个 case） | 锁住第零节第 1 条 |
| **G2** | **策略完整性用例**：AST 扫源码里的凭据形态标识符，断言都落在 `sensitive_keys`/`fragments` 内 | 新增字段名自动被发现 |
| G3 | 依赖双向断言（声明↔import） | 会让 `selenium` 立刻红 |
| G4 | `config.example.json` 与实际配置的奇偶检查 | 防 C3 漂移 |
| G5 | Python↔C# 共享字面量 drift 检查 | 防第零节第 5 条 |
| G6 | 断言密度下限 | 防 A5/A6 |
| G7 | 文档引用的源码路径存在性检查 | 防 5.2 漂移 |
