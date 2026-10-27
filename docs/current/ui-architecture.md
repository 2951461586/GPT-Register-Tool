# 桌面端（WPF）UI 架构

本文回答「改界面该动哪个文件、哪些逻辑不能留在界面里」。协议与后端行为见
[`registration-and-proxy-architecture.md`](registration-and-proxy-architecture.md)、
[`protocol-payment-enhancement.md`](protocol-payment-enhancement.md)；
物理目录归属见 [`directory-map.md`](../directory-map.md) 第 10/11 条规则。

## 1. 分层与所有权

| 层 | 位置 | 拥有 | 不拥有 |
| --- | --- | --- | --- |
| 应用壳 | `SmsWorkbench/App.xaml(.cs)`、`AppHost.cs` | 依赖注入容器、启动窗口、全局资源字典合并 | 任何业务流程 |
| 窗口与呈现 | `Views/`（`MainWindow.*` 分部类、`TitleBar`、`Sidebar`、`AccountFilterBar`、`AccountGridView`、`WorkbenchConsole`、`SettingsWindow`、`PaymentBatchWindow`） | 布局、绑定、交互反馈、窗口级状态（主题、侧栏折叠、分页游标） | 命令语义、HTTP、持久化 |
| 视图模型 | `ViewModels/`（`PoolViewModel`、`TaskListViewModel`、`LogViewModel`、`StatusBarViewModel`、`SettingsViewModel`、`PaymentBatchViewModel`、`ExportViewModel`、`RegisterViewModel`） | 可观察状态、展示格式化、输入校验 | 直接写配置或发 HTTP |
| 服务 | `Services/`（`ConfigStore`、`SettingsService`、`PaymentBatchService`、`MailboxPoolFileStore`、`WindowThemeService`、`ApplicationPaths`、`FileLauncher`、`LegacyConfigMigration`、`BackendTaskCoordinator`、`TransientArtifactCleanup`） | 读写本地配置/池文件、组装后端任务、迁移旧配置、清理临时凭据 | 解析厂商协议 |
| 后端边界 | `Interop/`（`PythonBackendClient`、`DesktopReadClient`、`BackendResultInterpreter`、`PythonPathResolver`、`SmsProviderCatalogClient`） | 启动 Python 进程、只读快照、把 JSON 结果翻译成领域对象 | 业务规则 |
| 模型 | `Models/` | 桌面侧契约（`AccountBatchProgress`、`DesktopThemePreference`、`PaymentBatchModels`、`SettingsModels`、`SmsProviderCatalog`…）**不含窗口**（窗口在 `Views/` 或 `Dialogs/`） | 与 `SmsWorkbench.Contracts` 重复的定义 |
| 对话框 | `Dialogs/`（`DialogFactory`、`SettingsDialogService`、`PaymentBatchDialogService`、`ChangeEmailDialogService`、`AccountBatchProgressDialog`） | 代码构造的对话框：窗口外壳、表单行、动作行、异步信息/确认框 | 业务流程；对话框里不拼 CLI 参数 |

硬边界：界面只**发起**命令并展示结果，注册/支付/邮箱改写全部在 `sms_tool`。
`SmsWorkbench/` 不得内联协议端点、重试策略或代理串解析（Rule 03/05/13；
`scripts/architecture_scan.py` 与 `tests/governance/test_architecture_rules.py` 守着）。

## 2. 启动与资源

`App.xaml` 现在只有 18 行：合并三个字典，顺序即优先级。

```text
Wpf.Ui.xaml   ->   Themes/Palette.xaml   ->   Themes/Controls.xaml
基础模板与控件        颜色令牌（不可被覆盖）      工作台覆盖样式
```

| 文件 | 行数 | 内容 |
| --- | ---: | --- |
| `SmsWorkbench/App.xaml` | 18 | `ResourceDictionary.MergedDictionaries`，无内联样式 |
| `SmsWorkbench/Themes/Palette.xaml` | 67 | 48 个 `SolidColorBrush`：语义色板、`Badge*` 状态徽章、`Accent*` 卡片强调色 |
| `SmsWorkbench/Themes/Controls.xaml` | 690 | 隐式/具名控件样式、三个 `ControlTemplate`、滚动条重模板、`DialogWindow`、`MenuIconPath`/`ToolbarIconPath`/`BooleanToVisibilityConverter` |

两条规则，来自 WPF 的解析时机而不是口味：

1. **`StaticResource` 只在自己那个字典里查。** 它在该字典加载时解析，跨字典引用
   只能靠合并顺序碰巧成功。因此 `Controls.xaml` 内部的 `BasedOn="{StaticResource
   {x:Type Button}}"` 等引用全部指向本文件里的键。
2. **读色板必须用 `DynamicResource`。** 主题切换（浅/深）在运行时替换颜色，只有
   `DynamicResource` 会跟着刷新；`Palette.xaml` 因此不含任何 `StaticResource`。

`tests/workbench/test_sms_workbench_layout.py` 钉住「根目录只有应用壳」「`Themes/`
是纯资源目录（无 code-behind）」「`Source=` 相对路径必须从 XAML 自身目录可解析」。

## 3. `MainWindow` 的组合方式

`MainWindow.xaml`（54 行）现在只剩窗口骨架与五个 UserControl 的摆放；
`MainWindow.xaml.cs` 与 16 个 `MainWindow.*.cs` 分部类合计 5040 行（2026-10-25 实测，
注册选项对话框改走 `DialogFactory` 后从 5115 行降下来），
按**功能**而不是按控件切分。五个控件（`TitleBar` 71、`Sidebar` 51、
`AccountFilterBar` 22、`AccountGridView` 83、`WorkbenchConsole` 126 行 code-behind）
与分部类合计 5396 行：

| 分部类 | 行数 | 职责 |
| --- | ---: | --- |
| `MainWindow.xaml.cs` | 255 | 字段/依赖注入、`INotifyPropertyChanged`、窗口生命周期、主题与图标几何 |
| `MainWindow.Commands.cs` | 49 | 工具栏命令（注册、支付、导出、体检）到 `IPaymentBatchService` / 后端的编排 |
| `MainWindow.ContextMenu.cs` | 223 | 账户右键菜单与批量操作 |
| `MainWindow.Detail.cs` | 379 | 详情面板：选中账户的只读渲染 |
| `MainWindow.Export.cs` | 703 | 导出流程与文件落地 |
| `MainWindow.Helpers.cs` | 450 | 剪贴板、对话框、日志落盘与共用小工具 |
| `MainWindow.Inbox.cs` | 342 | 收件箱/验证码读取 |
| `MainWindow.Language.cs` | 37 | EN/CN 切换：`Loc` 切换、偏好落盘、刷新缓存的展示串 |
| `MainWindow.Navigation.cs` | 114 | 分页游标、筛选导航与选中行回退 |
| `MainWindow.Payment.cs` | 68 | 批量支付窗口入场 |
| `MainWindow.Pools.cs` | 513 | 池文件加载/切池/合并 |
| `MainWindow.Register.cs` | 668 | 注册任务发起与进度消费（两个选项对话框走 `DialogFactory`） |
| `MainWindow.Sidebar.cs` | 74 | 侧栏列宽动画（视觉状态在 `Sidebar` 控件里） |
| `MainWindow.SmsProvider.cs` | 481 | 一键接码供应商面板 |
| `MainWindow.Tasks.cs` | 492 | 任务列表与取消 |
| `MainWindow.Theme.cs` | 186 | 浅/深主题切换与 `DesktopThemePreference` 落盘 |

三条阅读要点：

* **窗口自己就是 `DataContext`。** `Pool`、`TaskList`、`LogPanel`、`StatusBar` 是
  `MainWindow` 上的视图模型属性，XAML 里直接 `{Binding Pool.PagedRows}`；没有
  单独的中介 ViewModel。五个 UserControl **都不设置自己的 DataContext**，直接继承
  窗口的，所以抽取没有改动任何绑定路径；窗口只通过 DP 传状态（`Sidebar.IsCollapsed`、
  `AccountGridView.Pool`、`WorkbenchConsole.TaskList/LogPanel`）和方法收回操作。
* **代码后置按名字抓控件。** XAML 里有 23 个 `x:Name`（`AccountGrid`、
  `TaskGrid`、`LogTextBox`、`SidebarColumn`、`SearchClearButton`…），13 个分部类
  跨文件读写它们 —— 分部类里「本文件没有引用」不代表没人用。
* **副作用都在服务里。** 界面只调用 `IPaymentBatchService` /
  `IConfigStore` / `IFileLauncher` 等接口，
  `AppHost` 负责注入具体实现；视图模型的构造参数因此可测试
  （`tests/SmsWorkbench.Tests/PaymentBatchViewModelTests.cs` 等）。

## 4. 命令与结果路径

```text
按钮 -> MainWindow.Commands（规划） -> *Service（组装 CLI 参数）
     -> Interop.PythonBackendClient（子进程，JSON 行协议）
     -> sms_tool（真正执行）
     <- BackendResultInterpreter（JSON -> 领域对象 / 展示文本）
     <- ViewModel（可观察状态）  <- 绑定  <- XAML
```

* **写**走 CLI：界面不拼 HTTP，只拼命令行；`--json` 输出由
  `BackendResultInterpreter` 解析成 `PaymentBatchModels` 等契约。
* **读**走只读快照：`DesktopReadClient` 消费 Python 侧 `desktop_ipc` 的只读契约
  （Rule 09：IPC writer 在 Python 侧）。
* 进度是事件，不是轮询：`BackendTaskCoordinator` 把 stdout 的 JSON 行转成
  `AccountBatchProgress`，`TaskListViewModel` 消费（Rule 11：进度与并发分离）。
* **代理不进 argv**：计划里仍然拼 `--proxy` / `--proxy-pool` / `--checkout-proxy-pool`
  / `--approve-proxy-pool`（CLI 契约不变、`BackendCommandPlanner` 保持纯函数），但
  `PythonBackendClient` 启动子进程前把这四个值换成
  `SMS_TOOL_PROXY` / `SMS_TOOL_PROXY_POOL` / `SMS_TOOL_CHECKOUT_PROXY_POOL` /
  `SMS_TOOL_APPROVE_PROXY_POOL` 环境变量并从 argv 移除 —— 住宅代理串带
  `user:pass`，而 Windows 上同用户进程可读彼此的命令行。四个 flag 全部登记在
  `sensitive_policy.json` 的 `sensitive_options`，所以任务日志里的参数展示也是脱敏的。
  详见 `PROXY_GUIDE.md` 的 *Desktop transport*。

## 4.1 状态判定与展示的分界

账号行的文字是**展示**：`Loc.T` 在英文界面返回英文标签。因此：

* 判定（是否已注册 / 是否需要关注 / 能否导出）**不得**比较展示串的字面量，
  否则切换语言会改变行为（实测：英文下注册计数归零、已注册邮箱被重新排进批次）。
  判定统一走 `Loc.DisplayContains(text, marker)`：它对**每种已投放语言**的形态
  都匹配，且与当前语言设置无关；守卫在
  `tests/governance/test_wpf_localized_decision_guard.py`。
  它的词表按 **marker 与词条的两个方向**展开（`StartsWith` 覆盖标签族，如 `已导入`
  → `已导入CPA`；`EndsWith` 覆盖限定词，如 `失败` → `网络失败`）。只做单向曾经
  又造出同形的语言不对称：中文 `网络失败` 命中 `失败` 而需要关注，英文
  `Network failure` 里没有 `Failed`，于是同一行在英文界面下**静默漏出**关注筛选
  与关注计数（`network_failed` / `mailbox_failed` 两个机器码，2026-10-26 实测）。
  同一根因也体现在徽章颜色上：`StatusSeverityConverter` 的英文危险词表原只有
  `Failed`，现补 `failure`。
* **已 pin 的分类缺口（非语言问题）**：`auth_state_failed` 渲染成 `会话失效` /
  `Session invalid`，两种语言都不含关注 marker，所以它**不在**关注筛选/计数里，
  尽管徽章把它画成危险色。是否让“会话失效”计入关注属于**产品决定**，
  `LocalizedStatusDecisionTests.A_session_invalid_row_is_a_pinned_attention_gap`
  显式钉住现状，避免被误当成已覆盖。
* 已迁移的点：`RegistrationStatusPresentation.cs`、`RegisterViewModel`、
  `PoolViewModel`、`ExportViewModel`；`Views/AccountGridPresentation.cs` 只做
  徽章配色（不是业务判定），仍保留手写的中英双清单。
* 彻底的做法是判定只读机器态（`registration_status` / `at_probe_status`），
  已作为 `docs/audits/plans/plan-2026-10-25-wpf-row-status-decoupling.md` 立项。

## 5. 主题与配色

`WindowThemeService` 读 `DesktopThemePreference`（`Models/`），切换
`Wpf.Ui.Appearance.ApplicationTheme`；因为 `Palette.xaml` 全用
`DynamicResource`，一次切换就能覆盖所有窗口、徽章与日志面板。
`SidebarBrandTitle` 旁的图标几何与 `IconNavButton` 的自绘模板都用
`StaticResource` 指向本窗口资源里的 `MenuIconPath` / `ToolbarIconPath`。

## 6. 测试与门禁

| 门禁 | 覆盖 |
| --- | --- |
| `tests/SmsWorkbench.Tests/DesktopWindowSmokeTests.cs` | 真实 `App` + `InitializeComponent()`，在 STA 线程打开设置/支付/主窗口，断言模板、`FindResource("SettingsSelectionBg")`、`PasswordBoxBinding` 往返；**这是资源字典与样式唯一的运行时证据** |
| `tests/workbench/test_sms_workbench_layout.py` | 根目录只放应用壳；角色目录集合；`Themes/` 是纯资源目录；XAML `Source=` 相对路径必须可解析 |
| `tests/workbench/test_localization_catalog.py` | 每个 `{loc:Tr}` / `Loc.T` / `Loc.F` 键都有英文；XAML 文本属性不得留中文；**`MessageBox.Show`、`Notify*`、`DialogFactory.Show*Async`、`Content`/`Text`/`Title` 等属性直接写字面中文即失败**（2026-10-25 新增，此前这类字符串静默漏了 25 条） |
| `tests/SmsWorkbench.Tests/BackendCommandPlannerSurfaceTests.cs` | `BackendCommandPlanner` 中「无生产调用方」的 builder 集合被冻结：新增死代码、接线、删除都要显式改白名单，且每个条目必须在 doc comment 里写明 |
| `scripts/dotnet_warning_ratchet.py` | C# 构建告警零基线（见 [`dotnet-gates.md`](dotnet-gates.md)） |
| `dotnet test GPTRegisterTool.slnx -c Release` | 557 个测试（CI 必跑） |

`SmsWorkbench.Tests` 里没有 UI 自动化：所有窗口级断言都在真实布局对象上做
（`ActualWidth`、`Template.FindName`、`FindVisualChildren`），不做像素比较。

## 7. 已知缺口与下一步

1. **`MainWindow.xaml` 已从 874 行降到 54 行（A0–A5 全部落地，2026-10-24）。**
   五个控件：`TitleBar`、`Sidebar`、`AccountFilterBar`、`AccountGridView`、
   `WorkbenchConsole`；`MainWindow.WindowChrome.cs` 与 `Themes/MainWindowResources.xaml`
   已删除，窗口级样式/转换器分别归入各自的控件字典或 `Themes/Controls.xaml`。
   完整 seam 记录与剩余阶段（B3/B4 已落地）见
   [`plan-2026-10-24-mainwindow-and-handlers-decomposition.md`](../audits/plans/plan-2026-10-24-mainwindow-and-handlers-decomposition.md)。
   code-behind 对窗口内控件的按名访问只剩 **3 个**（`SidebarColumn`、
   `Chrome`（模板内）、`SnackbarPresenter`）；其余状态都走 DP 或方法
   （`Sidebar.IsCollapsed`、`AccountGridView.SelectedRow/ClearSelection`、
   `WorkbenchConsole.ScrollToLast/TryAppendLogLine`）。`DesktopWindowSmokeTests`
   已为每个控件补了运行时断言（筛选条清除按钮的可见性往返、标题栏三按钮、
   侧栏折叠路由、账号表列/上下文菜单、控制台布局）。
2. **`MainWindow.Commands.cs` 等分部类没有单测**：它们能测的部分（参数规划）应
   沉进 `Services/`，界面只保留「取值 → 调用」。
3. **`Themes/Controls.xaml` 的滚动条模板仍绑着具体像素**：换字号或换 DPI 时最容易
   出问题的一段，改动需要同时看 `MainWindow.xaml` 里的 `ScrollViewer` 设置。

## 8. 事实归属

| 事实 | 权威位置 |
| --- | --- |
| 颜色令牌与控件样式 | `SmsWorkbench/Themes/Palette.xaml` / `Controls.xaml` |
| 资源合并顺序 | `SmsWorkbench/App.xaml` |
| 窗口布局与绑定 | `SmsWorkbench/Views/MainWindow.xaml` |
| 命令规划 | `SmsWorkbench/Views/MainWindow.Commands.cs` + `Services/` |
| 后端 JSON → 领域对象 | `SmsWorkbench/Interop/BackendResultInterpreter.cs` |
| 只读桌面契约 | `SmsWorkbench/Interop/DesktopReadClient.cs` + Python `desktop_ipc` |
| C# 构建告警基线 | `scripts/dotnet_warning_ratchet.py` |
