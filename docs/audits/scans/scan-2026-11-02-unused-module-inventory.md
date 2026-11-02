# 扫描：死代码 / 长期未用模块盘点（2026-11-02）

- **日期**：2026-11-02
- **HEAD**：`b8666a6`（`docs(rules): add Rule 26 for the broad-except marker and its budget`）
- **范围**：427 个 tracked Python 模块（`sms_tool/` 327 · `services/` 35 · `scripts/` 65）的「零外部引用」候选；202 个 tracked C#/XAML 文件（`SmsWorkbench/` 113 `.cs` + 15 `.xaml`、`SmsWorkbench.Contracts/` 20、`tests/SmsWorkbench.Tests/` 54）里的 **254** 个类型声明
- **不在范围**：任何源码删除或生产行为变更。本文只记录测量与核实结论；§5 是建议，未落地
- **相关**：`scan-2026-10-28-graph-refresh-extractor-kernel-deadcode.md` §4（同一类误报的第一次记录）、`docs/directory-map.md`、`docs/current/ui-architecture.md` §3.1

---

## 0. 结论先行

1. **真死模块 0 个**。三信号同时为空的候选只有 2 个 Python 模块，逐一核实后都不该删：一个是 subprocess 边界（静态图看不见），一个是文档化的 plug-in seam（有测试、无生产读者）。
2. **C# 侧 0 个无外部引用的生产类型**。254 个声明里 45 个是 xUnit 测试类（按约定被测试框架发现，没有外部引用是正常的），9 个是「只在声明文件里出现」的生产类型——全部是**同文件局部类型**，1 个是 XAML 标记扩展的文本误报。
3. **tracked 集合里没有构建/临时产物**：0 个 `.log` / `.bak` / `.tmp` / `.orig` / `.pyc`，0 个 `__pycache__` 目录入库。本轮清理掉的 460 MB 全部是被 `.gitignore` 排除的中间产物（§4），`git status` 在清理前后均无 tracked 变化。

---

## 1. 方法与判据

### 1.1 Python（427 模块）

只有**三条信号同时为空**才进入候选，避免把「按文件名调用」的模块当死码：

| 信号 | 提取方式 |
| --- | --- |
| import 声明 | `^\s*(from X import …|import X)` 的 `X`（含子模块前缀） |
| basename 标识符 | 模块文件名（去 `.py`）作为完整标识符出现在**其它** tracked 文件里 |
| dotted name 字符串 | 模块 dotted name 作为字符串出现在其它文件（覆盖 `importlib` 与注册表写法） |

排除项（结构性不可判）：`__init__.py`、`__main__.py`、`cli.py`，以及 `scripts/**`（操作入口，按文件名手工或 CI 调用）。

### 1.2 C# / XAML（254 类型）

类型声明正则抓取 `public|internal (sealed|static|abstract|partial|record|readonly)* (class|record|interface|enum|struct) Name`，再在**全部** `.cs` / `.xaml` / 测试源码里搜完整标识符；只有出现位置全部落在声明文件之内才算候选。XAML 侧覆盖 `{x:Type}` / `{loc:Tr}` / `x:DataType` 这类**不以类型名**出现的用法只能靠人工复核（§3.3）。

### 1.3 为什么这份清单不能单独用来删代码

`vulture` 在本环境**未安装**（`python -m vulture` → `No module named vulture`），与 2026-10-28 那次扫描同口径：候选清单只有一个信号——可达性。而本仓有三类结构让可达性天然失真（§3）。因此本报告的每一条候选都带人工核实结论，**不提供「可直接删除」的清单**。

---

## 2. 结果

### 2.1 Python 候选（2/427）

| 模块 | 外部引用 | 核实结论 |
| --- | --- | --- |
| `services/protocol-payment/kakao/kakao_extract.py` | 静态 import 0 | **误报**。由 `sms_tool/pay_link/adapters.py::_build_kakao_env` 起的 subprocess 按**目录名**驱动，脚本名不出现在任何 import 或字符串里。与 2026-10-28 扫描 §4 的 4 个提取器同因（提取器靠运行时 `sys.path.insert` + `subprocess`，静态图 `imports` 解析率 0%）。最后改动 `64aa42c`（2026-10-20）。 |
| `sms_tool/capability_browser.py` | 生产 0 / 测试 1 | **不是死码，但它是全仓最接近死码的模块**。它是文档化的 plug-in seam：`payment_method_capability_probe(fallback_transport=…)` 的浏览器回退传输（`docs/current/account-health.md:126`），模块 docstring 明写「deliberately a plug-in seam, not a default」。唯一读者是 `tests/payment/test_checkout_sentinel.py`（`from sms_tool import capability_browser as cb`）。最后改动 `72c8005`（2026-10-21，`refactor(payment): move the payment family into sms_tool/payment`），即**随重构搬迁但一直没有生产调用点**。 |

### 2.2 C#「仅声明文件内出现」的 9 个生产类型

| 类型 | 声明文件 | 该文件内出现次数 | 结论 |
| --- | --- | --- | --- |
| `ChangeEmailDialogOptions` | `SmsWorkbench/Dialogs/ChangeEmailDialogService.cs` | 4 | 同文件局部 options 记录，非死码 |
| `CollectedAccountExport` | `SmsWorkbench/Services/AccountExportAssembler.cs` | 3 | 同上（组装结果只在装配器内部流通） |
| `ScopeOption` | `SmsWorkbench/ViewModels/PoolViewModel.cs` | 5 | 同上（XAML 通过 `ItemsSource` 绑定实例，不写类型名） |
| `SettingsCategoryDefinition` | `SmsWorkbench/Services/SettingsCatalog.cs` | 4 | 同上 |
| `SettingsSectionDefinition` | `SmsWorkbench/Services/SettingsCatalog.cs` | 4 | 同上 |
| `PaymentCountryDocument` | `SmsWorkbench.Contracts/PaymentMethods.cs` | 4 | 同上（`payment_methods.json` 的反序列化形状，只在本文件用） |
| `PaymentMethodDefinition` | `SmsWorkbench.Contracts/PaymentMethods.cs` | 4 | 同上 |
| `PaymentMethodDocument` | `SmsWorkbench.Contracts/PaymentMethods.cs` | 4 | 同上 |
| `TrExtension` | `SmsWorkbench/Localization/TrExtension.cs` | 1 | **误报**：XAML 里写的是 `{loc:Tr …}`，类型名从不出现在标记扩展用法里（见 §3.3） |

其余 45 个是 `tests/SmsWorkbench.Tests/` 里的测试类（`*Tests`），由 xUnit 按命名约定发现，外部无引用是**预期**形态，不是死码。

---

## 3. 误报清单（为什么可达性一个信号不够）

1. **subprocess 边界**：`services/protocol-payment/` 的 7 个提取器与 `services/mail-otp-web/` 由父进程以「目录 / 脚本路径」启动，脚本名不进 import 图，也不出现在任何字符串里。删掉任何一个都会在**运行时**才炸。
2. **plug-in seam**：`capability_browser.py` 这类模块的存在意义就是「被注入」而不是「被调用」；判断它是否还有用，要看功能是否还有意保留，不是看引用数。
3. **XAML 标记扩展 / 绑定**：`{loc:Tr}`、`ItemsSource="{Binding …}"`、`x:DataType` 让类型或成员在 C# 文本里零出现。`TrExtension` 只有这一种用法。
4. **同文件局部类型**：`record XxxOptions` / `record XxxDocument` 本来就不该跨文件出现；按「出现次数 = 1」判定会一次删掉 8 个仍在用的类型。

第 1 条与 2026-10-28 扫描 §4 的结论一致（那次的 15 条候选 15/15 全是误报）；本轮的 2 条 Python 候选里，1 条是第 1 类、1 条是第 2 类。

---

## 4. 清理记录（只有被忽略的产物）

| 对象 | 处置 | 说明 |
| --- | --- | --- |
| `__pycache__/` × 44（`sms_tool/`、`scripts/`、`services/`、`tests/`） | 删除 | 字节码；`.venv/` 与 `runtime/python/` 内的**未动**（它们属于解释器环境） |
| `.pytest_cache/`、`.ruff_cache/` | 删除 | 测试/格式工具缓存 |
| `SmsWorkbench/obj/`、`SmsWorkbench.Contracts/{bin,obj}/`、`tests/SmsWorkbench.Tests/{bin,obj}/`、`scripts/installer/{bin,obj}/` | 删除 | .NET 中间产物（`SmsWorkbench/bin` 已由 `build_dotnet.ps1` 的 `clean_dotnet_workspaces.ps1` 清掉）；重新构建即恢复 |
| `runtime/tmp/*` | 清空 | 一次性探针/还原脚本的约定位置（`docs/directory-map.md`：本轮结束时删除）。`runtime/` 其余部分是操作者状态（SQLite / 日志 / 缓存），**未动** |
| `dist/net10/` | 保留 | 本轮重新发布：`build_dotnet.ps1 -Version 2026.11.02`（`FileVersion 2026.11.2.0`） |
| `dist/release/` | 保留 | v2026.10.28 的发布资产（zip / installer / sha256），是发布证据，不是中间产物 |

合计释放 **460 MB**（2638 MB → 2178 MB）。清理前后 `git status --porcelain` 的内容一致：没有任何 tracked 文件被删除或改动。

---

## 5. 建议（未落地）

1. **不删任何模块。** 两个候选都不满足「删除后行为与可维护性同时不变」这个条件。
2. `sms_tool/capability_browser.py` 如果要处置，正确顺序是：先决定浏览器回退这条能力是否还保留 → 若保留，在 `payment/capability.py` 或 CLI 侧给它一个**真实调用点**（目前 `fallback_transport` 只有测试在用）；若不保留，连同 `tests/payment/test_checkout_sentinel.py` 的相关用例一起删，并在 `docs/current/account-health.md:126` 记一笔。单删模块会留下一条指向空气的文档与一个孤立测试。
3. 下一次做这类盘点时，把 §1.1 的三信号扫描脚本落到 `scripts/`（本轮的扫描是内联脚本，`vulture` 未安装所以没有可复用入口）；在此之前，本报告的口径只能靠人工复现。
