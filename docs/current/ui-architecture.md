# 桌面端（WPF）UI 架构

本文回答「改界面该动哪个文件、哪些逻辑不能留在界面里」。协议与后端行为见
[`registration-and-proxy-architecture.md`](registration-and-proxy-architecture.md)、
[`protocol-payment-enhancement.md`](protocol-payment-enhancement.md)；
物理目录归属见 [`directory-map.md`](../directory-map.md) 第 10/11 条规则。

## 1. 分层与所有权

| 层 | 位置 | 拥有 | 不拥有 |
| --- | --- | --- | --- |
| 应用壳 | `SmsWorkbench/App.xaml(.cs)`、`AppHost.cs`、`Themes/` | 依赖注入容器、启动窗口、全局资源字典合并（令牌 / 字形 / 光暗色板 / 控件样式） | 任何业务流程 |
| 窗口与呈现 | `Views/`（`MainWindow.*` 分部类、`TitleBar`、`Sidebar`、`AccountFilterBar`、`AccountGridView`、`WorkbenchConsole`、`SettingsWindow`、`PaymentBatchWindow`、`ProxyPoolWindow`） | 布局、绑定、交互反馈、窗口级状态（主题、侧栏折叠、分页游标） | 命令语义、HTTP、持久化 |
| 视图模型 | `ViewModels/`（`PoolViewModel`、`TaskListViewModel`、`LogViewModel`、`StatusBarViewModel`、`SettingsViewModel`、`PaymentBatchViewModel`、`ExportViewModel`、`RegisterViewModel`） | 可观察状态、展示格式化、输入校验 | 直接写配置或发 HTTP |
| 服务 | `Services/`（`ConfigStore`、`SettingsService`、`PaymentBatchService`、`MailboxPoolFileStore`、`WindowThemeService`、`ApplicationPaths`、`FileLauncher`、`LegacyConfigMigration`、`BackendTaskCoordinator`、`TransientArtifactCleanup`、`AppVersion`、`VersionComparer`、`UpdateChecker`） | 读写本地配置/池文件、组装后端任务、迁移旧配置、清理临时凭据 | 解析厂商协议 |
| 后端边界 | `Interop/`（`PythonBackendClient`、`DesktopReadClient`、`BackendResultInterpreter`、`PythonPathResolver`、`SmsProviderCatalogClient`） | 启动 Python 进程、只读快照、把 JSON 结果翻译成领域对象 | 业务规则 |
| 模型 | `Models/` | 桌面侧契约（`AccountBatchProgress`、`DesktopThemePreference`、`PaymentBatchModels`、`SettingsModels`、`SmsProviderCatalog`…）**不含窗口**（窗口在 `Views/` 或 `Dialogs/`） | 与 `SmsWorkbench.Contracts` 重复的定义 |
| 对话框 | `Dialogs/`（`DialogFactory`、`SettingsDialogService`、`PaymentBatchDialogService`、`ChangeEmailDialogService`、`AccountBatchProgressDialog`） | 代码构造的对话框：窗口外壳、表单行、动作行、异步信息/确认框 | 业务流程；对话框里不拼 CLI 参数 |

硬边界：界面只**发起**命令并展示结果，注册/支付/邮箱改写全部在 `sms_tool`。
`SmsWorkbench/` 不得内联协议端点、重试策略或代理串解析（Rule 03/05/13；
`scripts/architecture_scan.py` 与 `tests/governance/test_architecture_rules.py` 守着）。

## 2. 启动与资源

`App.xaml` 合并六个字典，顺序即优先级。其中**两套**主题字典各自换：Wpf.Ui 的
`Resources/Theme/*.xaml` 归 `ApplicationThemeManager`，`Themes/Colors.*.xaml` 归
`MainWindow.SwapColorScheme`。

```text
Wpf.Ui.xaml -> Wpf.Ui Theme/Light.xaml -> Themes/Tokens.xaml -> Themes/Glyphs.xaml -> Themes/Colors.Light.xaml -> Themes/Controls.xaml
基础模板        WPF-UI 主题键（换主题）      间距/圆角/字阶/动效     字形几何            颜色（运行时整体替换）        工作台覆盖样式
```

| 文件 | 行数 | 内容 |
| --- | ---: | --- |
| `SmsWorkbench/App.xaml` | 35 | `ResourceDictionary.MergedDictionaries`，无内联样式 |
| `SmsWorkbench/Themes/Tokens.xaml` | 122 | 与主题无关的刻度：`Space.*`（4px 网格）、`Radius.*`/`Corner.*`、`Type.*`、`Font.Mono`、`Size.ScrollBar.*`/`Size.Splitter.*`、`Motion.*` |
| `SmsWorkbench/Themes/Glyphs.xaml` | 105 | 34 个 Tabler v3.50.0 字形（`Glyph.*`，MIT）+ 2 个手绘窗口 chrome 字形；同义几何复用同一个键，每个键上方注明来源图标名 |
| `SmsWorkbench/Themes/Colors.Light.xaml` | 108 | 光色：5 级表面 `Surface.*`、3 级文字、状态徽章、`Divider`、ComboBox 弹层键 |
| `SmsWorkbench/Themes/Colors.Dark.xaml` | 86 | 暗色，**与光色同一套键**（缺一个键就会在暗色下回落到 WPF 默认笔刷） |
| `SmsWorkbench/Themes/Controls.xaml` | 911 | 隐式/具名控件样式、`ControlTemplate`、滚动条与分隔条重模板、`DialogWindow`、`FocusRingStyle` |

三条规则，来自 WPF 的解析时机与类型而不是口味：

1. **颜色一律 `DynamicResource`，令牌一律 `StaticResource`。** 换主题是替换整个
   `Colors.*` 字典（`MainWindow.SwapColorScheme`），只有 `DynamicResource` 会跟着
   重解析；`Tokens.xaml` / `Glyphs.xaml` 合并在色板之前且不随主题变化，所以
   `Corner.*` / `Type.*` / `Glyph.*` 用 `StaticResource` 是确定性的 —— 顺序写错会在
   首次解析时**硬失败**，而不是静默降级成默认笔刷。
2. **`CornerRadius` 用 `Corner.*`，`RadiusX/Y` 用 `Radius.*`。** 同名不同型
   （`CornerRadius` vs `double`），拿错会在模板加载时抛 `XamlParseException`；
   2026-10-28 实际踩过一次，被 `DesktopWindowSmokeTests` 抓到。
3. **色板键名是对外契约。** `WindowThemeService` 从 `PanelBg`/`TextMain` 取 DWM
   标题栏色，`DesktopWindowSmokeTests` 断言 `SettingsSelectionBg`/`…Border`，所以旧
   键在 `Colors.*` 里保留为别名，不删。
4. **Wpf.Ui 的主题字典必须出现在合并列表里，而且位置有讲究。** Wpf.Ui 4.3.0 的
   `Wpf.Ui.xaml` 只合并控件模板与静态色表，**不含** `Resources/Theme/{Light,Dark}.xaml`；
   而 `ApplicationThemeManager.Apply` 只会替换「Source 同时含 `wpf.ui;` 与 `theme`」
   的那个合并字典，找不到就在写缓存前 `return`。缺这一行时 `Apply` 是**静默 no-op**：
   `GetAppTheme()` 永远 `Unknown`、`Changed` 不触发、未被 `Controls.xaml` 覆盖的
   WPF-UI 模板键（`TextFillColor*Brush`、`MenuItem`/`DataGrid` 禁用态…）全部悬空。
   位置必须在 `Wpf.Ui.xaml` 之后（主题键通过它解析 `SystemAccentColorPrimary`）、
   `Themes/Colors.Light.xaml` 之前（两套字典共有的 `ComboBoxDropDown*` 键由工作台
   色板胜出，否则深色模式会继承 Wpf.Ui 的浅色弹层）。
5. **启动主题只有一条日志。** `MainWindow` 构造函数用 `ApplyStartupTheme(theme, log: false)`
   静默播种色板（供 `DesktopWindowSmokeTests` 这类不走 `App.OnStartup` 的场景），
   `App.OnStartup` 是**唯一**写「启动主题已应用」的调用点。两处都写时，每次启动会先
   打印构造函数的猜测（`Unknown` 不是 `Dark`，于是 `Light`）再打印偏好值（`Dark`）。

`tests/workbench/test_sms_workbench_layout.py` 钉住「根目录只有应用壳」「`Themes/`
是纯资源目录（无 code-behind）」「`Source=` 相对路径必须从 XAML 自身目录可解析」；
`tests/workbench/test_theme_palette_consistency.py` 钉住两套色板的键集与阶梯；
`tests/workbench/test_startup_theme_contract.py` 钉住上面第 4、5 条（合并项及其位置、
日志调用点唯一），行为面由 `DesktopWindowSmokeTests` 兜底（字典真的换、构造函数真的
不打日志）。

## 3. `MainWindow` 的组合方式

`MainWindow.xaml`（73 行）现在只剩窗口骨架与五个 UserControl 的摆放；
`MainWindow.xaml.cs` 与 16 个 `MainWindow.*.cs` 分部类合计 5201 行（2026-10-28 实测），
按**功能**而不是按控件切分。五个控件的 code-behind 合计 958 行。

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
| `MainWindow.Theme.cs` | 113 | 浅/深主题切换：换色板字典 + DWM 标题栏 + `DesktopThemePreference` 落盘 |
| `MainWindow.NavigationState.cs` | 56 | 侧栏「运行中」状态：`ActiveNavKey` DP、pending key 的交接与清除 |

三条阅读要点：

* **窗口自己就是 `DataContext`。** `Pool`、`TaskList`、`LogPanel`、`StatusBar` 是
  `MainWindow` 上的视图模型属性，XAML 里直接 `{Binding Pool.PagedRows}`；没有
  单独的中介 ViewModel。五个 UserControl **都不设置自己的 DataContext**，直接继承
  窗口的，所以抽取没有改动任何绑定路径；窗口只通过 DP 传状态（`Sidebar.IsCollapsed`、
  `AccountGridView.Pool`、`WorkbenchConsole.TaskList/LogPanel`）和方法收回操作。
* **代码后置按名字抓控件。** XAML 里有 34 个 `x:Name`（`AccountGrid`、
  `AccountCardList`、`CompactColumnsToggle`、`TaskGrid`、`LogTextBox`、
  `SidebarColumn`、`SearchClearButton`…），分部类跨文件读写它们 —— 分部类里「本文件
  没有引用」不代表没人用。**新增的控件名一律不直接用生成的字段**：`AccountGridView`
  已把这个约定写在文件头（「per-file analyzers do not see AccountGridView.g.cs」），
  用 `FindName` 装进自己声明的只读字段。
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

## 3.1 标题栏的版本号与更新检查

标题栏左侧是一个簇：`GRT` 标记 → 版本号（可点，等于「立即检查」）→
有更新时出现的胶囊（点开下载页）。版本号是**唯一**说明当前构建的地方，
所以它同时是触发检查的入口。

| 关注点 | 归属 |
| --- | --- |
| 版本号来源 | `Directory.Build.props` 的 `Version`（默认 `0.0.0`），发布时由 `build_dotnet.ps1 -Version` 盖戳；运行时读 `AssemblyInformationalVersion` → `Services/AppVersion.cs` |
| 昵称 | 未盖戳的构建显示 `dev` 而不是 `v0.0.0`：没人盖的号不是发布 |
| 比较 | `Services/VersionComparer.cs`，按段数值比较（日历版本 `2026.10.28` 用 `System.Version` 会抛或错序） |
| 数据源 | `Services/UpdateChecker.cs`，GitHub Releases API；仓库默认 `2951461586/GPT-Register-Tool`，可用 `GRT_UPDATE_REPO` 覆盖（fork 或测试夹具） |
| 节流 | 6 小时，缓存写在 `runtime/update_check.json`；未认证 API 每小时 60 次，重启几次就能打光 |
| 失败 | 一律返回结果对象，不抛异常（离线 / 限流 / JSON 畸形）；手动检查才弹提示，自动检查静默 |
| 手动检查 | `MainWindow.Update.cs` 的 `CheckUpdatesCommand`（点版本号）；启动时 `Loaded` 跑一次自动检查 |
| 打开下载页 | `OpenReleaseCommand` → 既有的 `OpenUrl`（只放行 http/https） |

三条不变量：**自动检查不打扰**（无更新或失败都不弹 snackbar）、**缓存答案要标注是缓存**
（`UpdateCheckResult.FromCache`，UI 才能区分"昨天是最新"和"现在是最新"）、
**绝不自动下载或替换文件**——桌面端是"exe + Python 核心"的形态，更新只能是跳转下载页。

图标来源：`Themes/Glyphs.xaml` 的 34 个功能图标取自
[tabler-icons](https://github.com/tabler/tabler-icons) v3.50.0（MIT），逐条照抄上游
`icons/outline/<name>.svg` 的 `d` 属性，键上方注明来源图标名，便于对库复核；
窗口最小化/最大化两个字形仍是手绘（OS chrome，1px 描边对齐像素栅格）。

### 启动时主题的唯一来源（2026-10-28）

亮/暗由 `runtime/desktop_theme.txt` 决定：`App.OnStartup` 读它 →
`ApplicationThemeManager.Apply(theme)` → `MainWindow.ApplyStartupTheme(theme)`
（后者负责换色板字典与主题图标）。**不要**改用
`ApplicationThemeManager.GetAppTheme()` 作为启动依据 —— WPF-UI 的文档写明它在失败时
返回 `ApplicationTheme.Unknown`，而 Unknown 不等于 Dark，于是暗色安装每次启动都会被
悄悄刷回浅色板（实测日志：`启动主题已应用: Light` 紧跟着 `启动主题已应用: Dark`）。
构造函数的 `GetAppTheme()` 只作为测试/设计时的兜底。语言同理：`desktop_language.txt`
在 `MainWindow` 构造之前由 `Loc.SetLanguage` 应用。

### 三条面板布局约束（2026-10-28，均由界面实报倒查）

- **多行文本必须用 `MultilineTextBox` 样式**。隐式 `TextBox` 样式为了单行输入把
  `Height` 定成 32；本地 `MinHeight` **不能取消**样式里的 `Height`，只会把它抬高。
  日志区因此长期是固定 220px，行高不足时底部（连同滑块末端）被裁掉。多行区域一律
  套 `MultilineTextBox`（把 `Height` 重新设为 `NaN` = Auto），高度交给 Grid 行。
- **`SystemColors.ControlBrushKey` 是 WPF 的漏网之鱼**。`DataGrid` 自带模板用它在两条
  滚动条之间画一个不透明方块，且不理会主题色板；小窗口出现横向滚动条时，那块浅灰正好
  落在纵向滑块的末端，看起来像"滑块底部被遮挡"。`AccountGridView` 在自己的资源里把它
  覆盖为 `Transparent`。
- **位图缩放到小尺寸必须显式指定采样模式**。侧栏 Logo 曾在 22px 上用默认双线性缩放
  1024×1024 的 PNG（更早还是从 `app-icon.ico` 取帧，帧选择与 22px 无关）。现在用 PNG +
  `RenderOptions.BitmapScalingMode="HighQuality"` + 40px 底板内取偶数 24px。

这三条都有 XAML 结构守卫（`tests/workbench/test_sms_workbench_layout.py`），因为编译器
看不到任何一条。

### 徽章与圆角的两条硬约束（2026-10-28）

- **徽章底色/字色是"低饱和"的**：`Badge*Bg` 为同色相淡洗、`Badge*Fg` 为同色相中等
  饱和，五态靠**色相**区分而不是靠鲜艳度。改动后需验证徽章内文字对比度 ≥ 4.5:1
  （当前最差 4.84:1，暗色 Danger）。
- **`CornerRadius` 不得超过短边的一半**：WPF 不像 CSS 那样夹取半径，半径超过短边一半时
  四角弧线互相重叠，矩形会被画成**透镜（两头细中间粗）**。因此 3px 细条（选中竖条、
  分隔条手柄）用 `Corner.Hairline`(1.5)，8px 滚动条滑块用 `Corner.Xs`(4)；
  `Corner.Full`(999) 只允许用在短边足够大的场景。

### 徽章配色只读机器态（支付资格，2026-10-28）

其余徽章仍把**展示文案**喂给 `StatusSeverityConverter` 做子串匹配，因此每加一个
标签都要在两种语言里各加一条规则 —— 支付资格列就是这种做法的受害者：实测 2026-10-28，
只有 `AT失效` / `检测失败` 能命中，`可用` / `无可用方式` / `无0元优惠` / `币种不符` /
`出口被拒` / `未确定` 全部落回中性灰 `BadgeNeutralFg`(#57606A)。

该列改为由 `payment_qualification_state`（`sms_tool/payment/qualification_states.py`
的六态机器码）决定颜色，`PaymentQualificationSeverityConverter` 采用 MultiBinding：
`state`（权威）+ `label`（仅用于 state 字段出现之前写入的旧行回退）。映射：

| state | 徽章 |
| --- | --- |
| `eligible` | success（可用·方法） |
| `ineligible` | warn（无可用方式 / 无0元优惠 / 币种不符 / 地区不符） |
| `egress_denied` | warn（出口被拒：什么都没学到，换出口重试） |
| `auth_invalid` | danger（AT失效） |
| `probe_failed` | danger（检测失败 / 风控拦截） |
| `unknown` | info（未确定） |

这与 §4.1 的方向一致：判定读机器态，展示文案只是展示。守卫在
`tests/SmsWorkbench.Tests/AccountGridPresentationTests.cs`。

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

光/暗是**两个字典**（`Colors.Light.xaml` / `Colors.Dark.xaml`，同一套键）。
`MainWindow.SwapColorScheme` 用 **pack URI**（显式命名 assembly）替换合并列表里的
色板条目 —— 相对 URI 在 `dotnet test` 宿主里会「找不到资源」，2026-10-28 实际踩过。
所有 `DynamicResource` 随之重解析，**已打开的窗口不必逐个刷**；换成字典之前这里有
约 120 行 `SetBrush("#hex")`，且必须靠 `ApplyToOpenWindows` 追着刷。
`WindowThemeService` 现在只剩 DWM 标题栏属性（读 `PanelBg`/`TextMain`），切换后调一次。
`ApplicationThemeManager.Apply` 仍然要调，那是 Wpf.Ui 自有控件的主题；
`DesktopThemePreference`（`Models/`）负责落盘。

配色的判定规则（结构用明度分层、颜色只给状态）与逐阶段证据见
[`plan-2026-10-28-wpf-visual-system.md`](../audits/plans/plan-2026-10-28-wpf-visual-system.md)；
源码里的事实归属是 `Themes/Controls.xaml` 的样式与 `Themes/Colors.*.xaml` 的键。

## 6. 测试与门禁

| 门禁 | 覆盖 |
| --- | --- |
| `tests/SmsWorkbench.Tests/DesktopWindowSmokeTests.cs` | 真实 `App` + `InitializeComponent()`，在 STA 线程打开设置/支付/主窗口，断言模板、`FindResource("SettingsSelectionBg")`、`PasswordBoxBinding` 往返；**这是资源字典与样式唯一的运行时证据** |
| `tests/workbench/test_sms_workbench_layout.py` | 根目录只放应用壳；角色目录集合；`Themes/` 是纯资源目录；XAML `Source=` 相对路径必须可解析 |
| `tests/workbench/test_localization_catalog.py` | 每个 `{loc:Tr}` / `Loc.T` / `Loc.F` 键都有英文；XAML 文本属性不得留中文；**`MessageBox.Show`、`Notify*`、`DialogFactory.Show*Async`、`Content`/`Text`/`Title` 等属性直接写字面中文即失败**（2026-10-25 新增，此前这类字符串静默漏了 25 条） |
| `tests/SmsWorkbench.Tests/BackendCommandPlannerSurfaceTests.cs` | `BackendCommandPlanner` 中「无生产调用方」的 builder 集合被冻结：新增死代码、接线、删除都要显式改白名单，且每个条目必须在 doc comment 里写明 |
| `scripts/dotnet_warning_ratchet.py` | C# 构建告警零基线（见 [`dotnet-gates.md`](dotnet-gates.md)） |
| `dotnet test GPTRegisterTool.slnx -c Release` | 622 个测试（CI 必跑） |

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
3. ~~**`Themes/Controls.xaml` 的滚动条模板仍绑着具体像素**~~ —— 2026-10-28 已收口：
   轨道 12 / 滑块 6（最小长度 18）与分隔条手柄改为 `Size.ScrollBar.*` /
   `Size.Splitter.*` 令牌；滑块 hover 时变粗 1px 并用 `TextMuted`，拖拽时 `TextSub`，
   分隔条手柄 `Opacity=0`，仅 hover/拖拽时显形（拖拽染 `Primary`）。

## 8. 事实归属

| 事实 | 权威位置 |
| --- | --- |
| 颜色令牌与控件样式 | `SmsWorkbench/Themes/Colors.{Light,Dark}.xaml` / `Controls.xaml` |
| 刻度与字形 | `SmsWorkbench/Themes/Tokens.xaml` / `Glyphs.xaml` |
| 侧栏运行中状态 | `Views/MainWindow.NavigationState.cs` + `Sidebar.ActiveKey` |
| 资源合并顺序 | `SmsWorkbench/App.xaml` |
| 窗口布局与绑定 | `SmsWorkbench/Views/MainWindow.xaml` |
| 命令规划 | `SmsWorkbench/Views/MainWindow.Commands.cs` + `Services/` |
| 后端 JSON → 领域对象 | `SmsWorkbench/Interop/BackendResultInterpreter.cs` |
| 只读桌面契约 | `SmsWorkbench/Interop/DesktopReadClient.cs` + Python `desktop_ipc` |
| C# 构建告警基线 | `scripts/dotnet_warning_ratchet.py` |

## 6. 外壳能力：主题三态 / 托盘 / 仪表盘 / 工作区

这一层是「窗口的外面」——它不属于任何业务面板，但决定长跑批次能不能活着。

| 能力 | 文件 | 契约 |
| --- | --- | --- |
| 主题三态 | `Models/DesktopThemeMode.cs`、`DesktopThemePreference.cs`、`Views/MainWindow.Theme.cs` | 持久化的是**模式**（`System`/`Light`/`Dark`），不是解析后的色板；`System` 由 `SystemThemeWatcher` 持续跟随，回调经 `ApplicationThemeManager.Changed` 把工作台色板一起换掉。侧栏按钮循环 `Light -> Dark -> System` |
| 托盘 + 关闭到托盘 | `Services/TrayIconService.cs`、`Services/CloseToTrayPolicy.cs` | `Shell_NotifyIcon` + 隐藏工具窗口（不引入 WinForms：一个 16px 图标不值得第二套 UI 栈，且消息窗口收不到 `TaskbarCreated`）；关闭时隐藏，只有托盘「退出」才真关 |
| 全局热键 | `Services/GlobalHotkeyService.cs` | `Ctrl+Alt+R` 显示/隐藏；注册失败只警告（组合可能已被占用），绝不阻塞启动 |
| 吞吐/成功率仪表盘 | `ViewModels/MetricsViewModel.cs`、`Views/Sparkline.cs` | 数据源是同一条 `BackendProgressEvent` 流（每个账号一个 terminal 事件），滚动窗口 10 分钟 / 10 秒一格；`cancelled` 不计入分母。**失败分类**按事件自带的 `FailureClass` 分桶（机器名原样显示，与日志可 grep 的词汇一致），无分类的归入本地化「未知」桶；没有失败时整段隐藏 |
| 池页内联测速 | `ViewModels/ProxyPoolViewModel.cs::MeasureRowAsync` | 复用单代理测试通道写回**该行**，不重建表格（保住选中、滚动与其它行的筛查结论） |
| 外壳开关（可关） | `Models/DesktopShellPreference.cs`、`Views/MainWindow.Shell.cs` | `runtime/desktop_shell.json` 存 `{tray, hotkey}`，默认都开；关掉托盘后「关闭」即退出（`CloseToTrayPolicy` 自然返回 false），关掉热键后 `Dispose` 释放注册 |
| 工作区备份/恢复 | `Services/WorkspaceBackupService.cs` | 显式白名单（配置 shard + 账号库 + 两个 UI 偏好）**加上配置里 `email_registration.token_file` 指向的邮箱池文件**；manifest 同时充当恢复时的允许名单；恢复前把现有文件复制到 `runtime/restore-<ts>/`，并做 zip-slip 双重校验 |

三条不变式，各有守卫：

1. **关闭 ≠ 退出。** `CloseToTrayPolicy` 的真值表是纯函数并被单测钉住；窗口级行为由 `DesktopWindowSmokeTests` 覆盖（有图标 → 隐藏且不销毁；托盘退出 → 真关且释放热键）。
2. **仪表盘只读事件，不猜日志。** 指标全部来自结构化 `BackendProgressEvent`；窗口算术显式接受 `nowUtc`，因此可以在不睡眠的情况下测过去。
3. **备份是白名单，不是目录复制。** `WorkspaceBackupServiceTests` 断言日志/缓存不会进包、恢复不会写出到根目录之外。

`scripts/broad_except_ratchet.py` + `scripts/broad_except_baseline.json` 是新增的 Python 侧闸门（
`tests/governance/test_broad_except_ratchet.py` 保护它），按三层判定：

* `whitelist`——**设计上就需要**宽泛处理的文件，每条必须写 `reason`（目前 4 条：代理桥接清理、浏览器进程回收、
  desktop-serve 请求隔离、日志初始化）；理由为空即失败，文件已没有宽泛处理器仍留在名单里也失败（那是过期许可）。
* `policy`——全局预算（`max_broad`/`max_silent`）。只冻结单文件，等于允许"A 文件删一个、B 文件加一个"；预算堵住这条路。
* `files`——其余文件的冻结额度，是债务不是背书，只应下降。

和 `dotnet_warning_ratchet.py` / `pyright_ratchet.py` 同一套思路：先冻结、再加预算、最后逐条白名单化。
