# 桌面端（WPF）UI 架构

本文回答「改界面该动哪个文件、哪些逻辑不能留在界面里」。协议与后端行为见
[`registration-and-proxy-architecture.md`](registration-and-proxy-architecture.md)、
[`protocol-payment-enhancement.md`](protocol-payment-enhancement.md)；
物理目录归属见 [`directory-map.md`](../directory-map.md) 第 10/11 条规则。

## 1. 分层与所有权

| 层 | 位置 | 拥有 | 不拥有 |
| --- | --- | --- | --- |
| 应用壳 | `SmsWorkbench/App.xaml(.cs)`、`AppHost.cs` | 依赖注入容器、启动窗口、全局资源字典合并 | 任何业务流程 |
| 窗口与呈现 | `Views/`（`MainWindow.*` 分部类、`SettingsWindow`、`PaymentBatchWindow`、`ProtocolPaymentWindow`） | 布局、绑定、交互反馈、窗口级状态（主题、侧栏折叠、分页游标） | 命令语义、HTTP、持久化 |
| 视图模型 | `ViewModels/`（`PoolViewModel`、`TaskListViewModel`、`LogViewModel`、`StatusBarViewModel`、`SettingsViewModel`、`PaymentBatchViewModel`、`ProtocolPaymentViewModel`、`ExportViewModel`、`StageMatrixViewModel`、`RegisterViewModel`） | 可观察状态、展示格式化、输入校验 | 直接写配置或发 HTTP |
| 服务 | `Services/`（`ConfigStore`、`SettingsService`、`PaymentBatchService`、`ProtocolPaymentService`、`MailboxPoolFileStore`、`StageMatrixStore`、`WindowThemeService`、`ApplicationPaths`、`FileLauncher`、`LegacyConfigMigration`、`BackendTaskCoordinator`） | 读写本地配置/池文件、组装后端任务、迁移旧配置 | 解析厂商协议 |
| 后端边界 | `Interop/`（`PythonBackendClient`、`DesktopReadClient`、`BackendResultInterpreter`、`PythonPathResolver`、`SmsProviderCatalogClient`） | 启动 Python 进程、只读快照、把 JSON 结果翻译成领域对象 | 业务规则 |
| 模型 | `Models/` | 桌面侧契约（`AccountBatchProgress`、`DesktopThemePreference`、`PaymentBatchModels`、`SettingsModels`、`SmsProviderCatalog`…） | 与 `SmsWorkbench.Contracts` 重复的定义 |

硬边界：界面只**发起**命令并展示结果，注册/支付/邮箱改写全部在 `sms_tool`。
`SmsWorkbench/` 不得内联协议端点、重试策略或代理串解析（Rule 03/05/13；
`scripts/architecture_scan.py` 与 `tests/governance/test_architecture_rules.py` 守着）。

## 2. 启动与资源

`App.xaml` 现在只有 16 行：合并三个字典，顺序即优先级。

```text
Wpf.Ui.xaml   ->   Themes/Palette.xaml   ->   Themes/Controls.xaml
基础模板与控件        颜色令牌（不可被覆盖）      工作台覆盖样式
```

| 文件 | 行数 | 内容 |
| --- | ---: | --- |
| `SmsWorkbench/App.xaml` | 16 | `ResourceDictionary.MergedDictionaries`，无内联样式 |
| `SmsWorkbench/Themes/Palette.xaml` | 67 | 48 个 `SolidColorBrush`：语义色板、`Badge*` 状态徽章、`Accent*` 卡片强调色 |
| `SmsWorkbench/Themes/Controls.xaml` | 666 | 隐式/具名控件样式、三个 `ControlTemplate`、滚动条重模板、`DialogWindow` |

两条规则，来自 WPF 的解析时机而不是口味：

1. **`StaticResource` 只在自己那个字典里查。** 它在该字典加载时解析，跨字典引用
   只能靠合并顺序碰巧成功。因此 `Controls.xaml` 内部的 `BasedOn="{StaticResource
   {x:Type Button}}"` 等引用全部指向本文件里的键。
2. **读色板必须用 `DynamicResource`。** 主题切换（浅/深）在运行时替换颜色，只有
   `DynamicResource` 会跟着刷新；`Palette.xaml` 因此不含任何 `StaticResource`。

`tests/workbench/test_sms_workbench_layout.py` 钉住「根目录只有应用壳」「`Themes/`
是纯资源目录（无 code-behind）」「`Source=` 相对路径必须从 XAML 自身目录可解析」。

## 3. `MainWindow` 的组合方式

`MainWindow.xaml`（874 行）是唯一的窗口布局；`MainWindow.xaml.cs` 与 14 个
`MainWindow.*.cs` 分部类合计约 5.4k 行，按**功能**而不是按控件切分：

| 分部类 | 行数 | 职责 |
| --- | ---: | --- |
| `MainWindow.xaml.cs` | 305 | 字段/依赖注入、`INotifyPropertyChanged`、窗口生命周期、主题与图标几何 |
| `MainWindow.Commands.cs` | — | 工具栏命令（注册、支付、导出、体检）到 `IPaymentBatchService` / 后端的编排 |
| `MainWindow.ContextMenu.cs` | — | 账户右键菜单与批量操作 |
| `MainWindow.Detail.cs` | 377 | 详情面板：选中账户的只读渲染 |
| `MainWindow.Export.cs` | 713 | 导出流程与文件落地 |
| `MainWindow.Helpers.cs` | 456 | 剪贴板、对话框、共用小工具 |
| `MainWindow.Inbox.cs` | 340 | 收件箱/验证码读取 |
| `MainWindow.Navigation.cs` | 136 | 分页游标与筛选导航 |
| `MainWindow.Payment.cs` | 66 | 支付窗口入场 |
| `MainWindow.Pools.cs` | 498 | 池文件加载/切池/合并 |
| `MainWindow.Register.cs` | 765 | 注册任务发起与进度消费 |
| `MainWindow.Sidebar.cs` | 82 | 侧栏折叠动画与持久化 |
| `MainWindow.SmsProvider.cs` | 483 | 一键接码供应商面板 |
| `MainWindow.Tasks.cs` | 510 | 任务列表与取消 |
| `MainWindow.Theme.cs` | 186 | 浅/深主题切换与 `DesktopThemePreference` 落盘 |
| `MainWindow.WindowChrome.cs` | 39 | 自绘标题栏（最小化/最大化/关闭） |

三条阅读要点：

* **窗口自己就是 `DataContext`。** `Pool`、`TaskList`、`LogPanel`、`StatusBar` 是
  `MainWindow` 上的视图模型属性，XAML 里直接 `{Binding Pool.PagedRows}`；没有
  单独的中介 ViewModel。
* **代码后置按名字抓控件。** XAML 里有 23 个 `x:Name`（`AccountGrid`、
  `TaskGrid`、`LogTextBox`、`SidebarColumn`、`SearchClearButton`…），13 个分部类
  跨文件读写它们 —— 分部类里「本文件没有引用」不代表没人用。
* **副作用都在服务里。** 界面只调用 `IPaymentBatchService` /
  `IProtocolPaymentService` / `IConfigStore` / `IFileLauncher` 等接口，
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
| `scripts/dotnet_warning_ratchet.py` | C# 构建告警零基线（见 [`dotnet-gates.md`](dotnet-gates.md)） |
| `dotnet test GPTRegisterTool.slnx -c Release` | 568 个测试（CI 必跑） |

`SmsWorkbench.Tests` 里没有 UI 自动化：所有窗口级断言都在真实布局对象上做
（`ActualWidth`、`Template.FindName`、`FindVisualChildren`），不做像素比较。

## 7. 已知缺口与下一步

1. **`MainWindow.xaml`（874 行）仍是单文件**，样式之外的三个候选抽取块已经可以
   命名：侧栏（品牌 + 折叠 + 导航，`SidebarColumn`/`SidebarToggleButton` 等 6 个
   `x:Name`）、筛选与分页条（`SearchClearButton` + 4 张统计卡）、任务/日志控制台
   （`TaskGrid`、`LogTextBox`、`SnackbarPresenter`）。
   抽取的前置条件是**先切断按名字访问**：每个 UserControl 必须把自己的状态暴露成
   `DependencyProperty` 或独立 ViewModel（侧栏 → `SidebarCollapsed`；控制台 →
   `TaskListViewModel` + `LogViewModel`），否则 13 个分部类里的 `x:Name` 引用会
   在编译期集体失效。`DesktopWindowSmokeTests` 只能覆盖打开/布局，抽取后需要为每个
   控件补一条模板断言。
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
