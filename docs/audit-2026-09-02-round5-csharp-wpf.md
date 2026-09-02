# 第五轮审计 · C#/WPF 侧（SmsWorkbench / Contracts / Tests）

口径：97 个 `.cs` + 5 个 `.xaml`（`SmsWorkbench/` 69、`SmsWorkbench.Contracts/` 11、`tests/SmsWorkbench.Tests/` 22）。
已排除 `**/bin/ **/obj/ dist/ runtime/ scripts/installer/ .dotnet/`——注意 **`obj/` 虽在 .gitignore 内，但 Grep 工具仍会扫到 `MainWindow.g.cs`**，本报告全部结论来自显式白名单脚本（102 文件）。
只读分析，未修改任何文件，未执行 `dotnet build`。

**先说排除项（前几轮的结论复核通过，不再重复）**：全局异常处理**已存在且完整**（`App.xaml.cs:24-26` 三个钩子齐全）；`DispatcherPriority` 全量仅 3 处且**全是 `Background`，无 `Render`**（`MainWindow.Tasks.cs:285`）；库层 `ConfigureAwait(false)` 齐全（DesktopReadClient/PythonBackendClient/BackendTaskCoordinator 共 22 处）；无任何 `#pragma warning disable`；`SemaphoreSlim` 未释放是**有注释的 deliberate 决策**（`DesktopReadClient.cs:148`）；`ConfigStore` 空桶 bug 已被 `ConfigStoreTests` 第 2 个用例锁住。

---

## P0 — UI 线程可能冻结 2 分钟

### P0-1 `FindMailboxLineFromBackend` 在 UI 线程上同步阻塞异步调用
- `SmsWorkbench/MainWindow.Register.cs:756-757`

```csharp
return desktopRead.ReadMailboxLineAsync(OnlyDigits(row.RawLine), row.Identifier)
    .GetAwaiter().GetResult().Trim();   // ← UI 线程硬等
```

`ReadMailboxLineAsync` 走常驻 IPC 通道，超时是 `DesktopReadProtocol.RequestTimeout` = **120 秒**（`SmsWorkbench.Contracts/DesktopReadProtocol.cs:39`，经 `DesktopReadClient.cs:258 ResidentRequestTimeout`）。
虽然 `DesktopReadClient` 内部配了 `ConfigureAwait(false)` 所以**不会死锁**，但后端卡住时整个窗口**无响应最长 120 秒**，连关闭按钮都点不动；且这是全仓唯一的 sync-over-async 生产路径（`App.xaml.cs:91` 的 `GetAwaiter().GetResult()` 在 `OnExit` 里，可接受）。
改法：把调用点改成 `await`，`FindMailboxLineFromBackend` 改签名为 `Task<string> FindMailboxLineFromBackendAsync(PoolRow)`，调用方（`MainWindow.Register.cs` 内邮箱行解析链）随之 await 化。若调用点处于同步属性/getter 无法 await，退一步：`Task.Run(...).GetAwaiter().GetResult()` 也比现在好（把阻塞挪到线程池，UI 仍卡但至少能重绘——**不推荐，仅作止血**）。

---

## P1 — 正确性 / 可诊断性

| # | 一句话结论 | 位置 | 后果 | 改法 |
|---|---|---|---|---|
| P1-1 | 侧边栏导航靠 **16 组魔法字符串** 分发，且伪造 `RoutedEventArgs` 直接调 Click 处理器 | `MainWindow.Navigation.cs:12-31`；XAML `CommandParameter` 见 `MainWindow.xaml:461-483` | XAML 的 `CommandParameter="batchpay"` 与 C# 的 `case "batchpay"` 是两份独立字面量，改一处漏一处 → **按钮静默失效，无任何编译/运行报错**；`new RoutedEventArgs()` 是假事件对象，处理器一旦读 `e.OriginalSource` 就 NRE | 把 16 个分支改成 `Dictionary<string, Func<Task>>` 或每个按钮独立 `RelayCommand`；键抽成 `internal static class NavKeys` 常量，XAML 用 `{x:Static local:NavKeys.BatchPay}` 引用，从根上消除双份字面量 |
| P1-2 | `SetScope("邮箱池")` / `SetScope("已注册")` 设置的筛选值 `FilterRow` **根本不识别** | 设置端 `MainWindow.Navigation.cs:133,135`；消费端 `MainWindow.Pools.cs:17-18` | `FilterRow` 只处理 `"有试用"` 和 `"待处理"`，这两个 scope 落进 else 分支 → **点了等于没筛，全量显示**，用户以为筛选生效了 | 要么在 `FilterRow` 补上两个分支，要么删掉这两个死处理器（见 P1-3）。注意 XAML 下拉框（`MainWindow.xaml:581-583`）只列了 `全部/有试用/待处理`，与 `FilterRow` 自洽，说明是 C# 侧的遗留分支 |
| P1-3 | **6 个死事件处理器**（上一轮删了 13 个死方法，这是漏网的） | `MainWindow.Navigation.cs:34, 105, 131, 133, 135, 137` | 无任何 XAML 引用、无 C# 引用、不在 `OnNavigate` switch 内。其中 4 个是 `SetScope` 包装器，2 个（`ShowMailboxPool_Click`/`ShowRegistered_Click`）还会**设置一个永远不生效的筛选值**，是最容易误导后来者的一种死代码 | 直接删除这 6 个方法。`AddSessionFileArg`/`SessionFileFor`/`SelectedEmailRowOrNotify` **别跟着删**——它们在 `MainWindow.Inbox.cs:150`、`MainWindow.Payment.cs:62`、`MainWindow.Register.cs:210,243` 仍有活引用（已逐个核对） |
| P1-4 | UI 层异常**全部只以 Information 级别、且不带异常对象**落盘 | `MainWindow.Helpers.cs:399-402` → `LogPresanitized` `:413-415`；`RunUiTaskAsync` `:9-23` | 全仓 13 处 Serilog 调用，**12 处集中在 `DesktopReadClient`/`PythonBackendClient`，MainWindow 只剩 `SmsBower.cs:29,34` 两处**。`MainWindow.*` 里约 30 个 `catch (Exception ex)` 全部走 `Log("…" + SensitiveDataSanitizer.Redact(ex.Message))`，最终写成 `logger.Information("{Message}", …)`——**没有堆栈、没有 Error 级别**，日志里看不出这是异常还是普通输出 | `LogPresanitized` 增加一个 `Log(string text, Exception? ex = null)` 重载：`ex is null ? logger.Information(...) : logger.Error(ex, "{Message}", text)`。批量把 `catch (Exception ex)` 里的 `Log(...)` 换成 `Log(..., ex)` |
| P1-5 | `AccountStatusInterpreter` 21.8 KB **零测试** | `SmsWorkbench/AccountStatusInterpreter.cs`（全文件，含 `:162,334,363` 三个裸 `catch`） | 这是 Accounts 状态机/文案解释的核心，`payment_method`、`status_code`、`account_deactivated`、`at_invalid` 等魔法字符串的最大产地（跨 9/7/5/3 个文件）。裸 `catch` + 无测试 = 状态显示错了没人知道 | 按 `BackendResultInterpreterTests`（36 用例）的样式补表驱动测试，先把 `:162,334,363` 三个裸 `catch` 收窄成具体异常类型 |
| P1-6 | `SensitiveDataSanitizer` 零测试 | `SmsWorkbench/SensitiveDataSanitizer.cs` | 这是**脱敏的唯一入口**，全靠它防 token/邮箱进日志。全仓仅 `BackendTaskCoordinatorTests.cs:50` 一处间接触达。脱敏漏一个正则 = 明文凭据写进 `runtime/app_.log`（保留 14 天） | 补测试：正常脱敏、边界（空串/null/超长）、以及"未脱敏字符串不得出现"的反向断言 |
| P1-7 | `TaskScheduler.UnobservedTaskException` 静默吞掉 | `App.xaml.cs:59-63` | `e.SetObserved()` 之后只写日志，**没有任何用户可见提示**。async void / fire-and-forget 的异常（`MainWindow.Pools.cs:37` 的 `_ = RefreshPoolsAsync(...)` 等）会被这个钩子吃掉，表现为"点了没反应"，排查极难 | 保留 `SetObserved()`（防进程崩），但加一次 `snackbarService.Show(...)` 或提高日志级别到 `Error`，让"静默失败"至少在 UI 上留痕 |

---

## P2 — 可维护性 / XAML 卫生

### P2-A XAML 重复内联样式（该进 ResourceDictionary）

| 重复内容 | 行号 | 改法 |
|---|---|---|
| **9-10 个图标 `<Path>` 共享完全相同的 7 个属性** | `MainWindow.xaml:647,653,660,666,672,678,684,690,696,703` | 抽 `<Style x:Key="MenuIconPath" TargetType="Path">`：`Stretch=Uniform Stroke={DynamicResource TextSub} StrokeThickness=1.4 StrokeStart/EndLineCap=Round StrokeLineJoin=Round Fill=Transparent`；每个 Path 只留 `Width/Height/Data` |
| 分頁按钮 6 个 `<Path>` 同上（仅 `StrokeThickness=1.5`） | `MainWindow.xaml:590,594,599,605,609,614` | 上一条的 `BasedOn` 变体 `ToolbarIconPath` |
| **4 张统计卡片整块复制粘贴**（Border+StackPanel+TextBlock×2） | `MainWindow.xaml:506,514,522,530`（内层文本 `:509,517,525,533`） | 抽 `StatCard` DataTemplate（`Label`/`Value` 两个绑定）+ `StatCardBorderStyle`/`StatCardLabelStyle`/`StatCardValueStyle`；顺带**删掉 `:507-508` 的冗余 `<StackPanel Orientation="Horizontal"><StackPanel>` 双层嵌套**（外层只有一个子元素） |
| 3 个窗口控制按钮的 ControlTemplate 完全同构 | `MainWindow.xaml:313-338 / 341-367 / 370-396` | 抽 `WindowControlButtonStyle`，`Path.Data` 用 `TemplateBinding Tag` 或附加属性传入 |
| `Style="{StaticResource IconNavButtonStyle}"` 重复 16 次 | `MainWindow.xaml:461-483` | 改成 `TargetType="local:IconNavButton"` 的**隐式样式**（去掉 `x:Key`），16 处全部可删 |
| `Style="{StaticResource AccountContextMenuItemStyle}"` 重复 10 次 | `MainWindow.xaml:645,651,658,664,670,676,682,688,694,701` | 放进 `<ContextMenu.Resources>` 的隐式样式 |
| 3 个 TextBox 同 4 属性 | `PaymentBatchWindow.xaml:65,75,109` | 抽 `MonospaceMultiLineTextBox` 样式 |

**顺带一个 copy-paste 致错的 UI bug**：`MainWindow.xaml:600`（"清空"）和 `:615`（"全选"）用了**完全相同的 Path Data**（`M9 11l3 3l8-8 …`），两个不同功能的按钮显示同一个对勾图标。

### P2-B 后台代码里用 C# 手搓 UI（最大技术债）
- **94 处 `FindResource("...")` 硬取资源键**：`MainWindow.Export.cs`(24) / `Register.cs`(22) / `Detail.cs`(18) / `Inbox.cs`(11) / `DialogFactory.cs`(7) / `SmsBower.cs`(6) / `AccountBatchProgress.cs`(4)
- **176 处 `new TextBlock/Button/TextBox/Grid/Window/…`** 直接在 .cs 里搭界面

后果：① 资源键全是魔法字符串，App.xaml 里改个 key → 运行期 `ResourceReferenceKeyNotFoundException`，**编译期零提示**；② 无设计器、无预览、无法主题化；③ 直接贡献了 `MainWindow.Export.cs` 879 行、`Register.cs` 773 行。
改法（**不要一次性重写**）：新建 `Views/` 目录，按对话框逐个迁到 XAML UserControl，优先迁 `AccountBatchProgress.cs:39-115`（最完整、最独立、纯展示）和 `DialogFactory.cs`（7 处 FindResource，被所有对话框共用，收益最大）。

### P2-C 未使用的 `x:Name`（可安全删除，不影响 g.cs 事件连线——已逐个核对无 `Click=`/`TargetName` 引用）

| 名称 | 位置 | 说明 |
|---|---|---|
| `MaxIcon` | `MainWindow.xaml:351` | 对比 `CloseIcon`(:380) 被 `:391` 的 Setter 引用，`MaxIcon` 无人引用 |
| `SidebarHost` | `MainWindow.xaml:405` | Grid，后台零引用 |
| `BatchPaymentButton` / `ChangeEmailButton` / `SettingsButton` | `MainWindow.xaml:468 / 472 / 481` | **只删 `x:Name`，保留 `AutomationProperties.Name`**（那是给无障碍用的，是有效的） |
| `DropDownBorder` | `App.xaml:428` | ComboBox 模板内 |

注：`MinimizeButton`/`MaximizeButton`/`CloseButton` **不能删**——它们的 `Click=` 写在下一行（`:316/344/373`），g.cs 靠字段名连线。

### P2-D 资源与生命周期

| 问题 | 位置 | 改法 |
|---|---|---|
| `_lifetimeCts` 只 Cancel 不 Dispose | `MainWindow.xaml.cs:19`（创建）、`:219`（Cancel） | `OnWindowClosing` 末尾补 `_lifetimeCts.Dispose()`；或让 `MainWindow` 实现 `IDisposable`（它是 DI 单例，见下条） |
| `static readonly HttpClient httpClient` 命名违反本文件约定且无 Timeout | `MainWindow.xaml.cs:6` | 改名 `_httpClient` 并显式 `new HttpClient { Timeout = TimeSpan.FromSeconds(30) }`；它是 `static` 挂在 Window 类上，正确做法是移到 `AppHost.cs:36-50` 注册成 `services.AddSingleton<HttpClient>()` 后注入 |
| `MainWindow` 注册为 **DI 单例** | `AppHost.cs:48` | WPF `Window` 作单例意味着关闭后无法再创建（重开窗口会拿到已 Closed 的实例）。若产品不需要多开可接受，但应加注释说明 |
| CTS 被覆盖前未 Dispose | `ProtocolPaymentViewModel.cs:185`（`_cancellation = new CancellationTokenSource()`） | 先 `if (_cancellation != null) { _cancellation.Dispose(); }`。路径：`Cancel()`(:241) 只 Cancel 不置 null，此时再启动新任务就会走到这里 |
| `GC.SuppressFinalize(this)` 但类无终结器 | `ProtocolPaymentViewModel.cs:249` | 直接删（CA1816） |
| 可选 DI 参数 + **静默空实现回退** | `StageMatrixViewModel.cs:66`、`ProtocolPaymentViewModel.cs:16`、`PaymentBatchViewModel.cs:59` | `IStageMatrixStore store = null` → `store?.Load() ?? Array.Empty<…>` 意味着**忘注册服务时阶段矩阵静默不持久化**，无任何报错。改成必填参数，让 DI 在缺注册时直接抛 |

### P2-E 其余

| 问题 | 位置 | 改法 |
|---|---|---|
| `<Nullable>annotations</Nullable>` —— 两个生产项目都**没开可空警告** | `SmsWorkbench.csproj:7`、`SmsWorkbench.Contracts.csproj:9`（测试项目反而是 `enable`） | 分批改 `enable`：先 Contracts（11 文件，无 WPF），再 SmsWorkbench。这也是 `MainWindow.xaml.cs:8` 的 `NavCommand { get; } = null!;` 和 `:38` 的 `private EventHandler sidebarRenderingHandler;`（未初始化）能混过编译的原因 |
| `NavCommand` 在 `DataContext = this` **之后**才赋值 | `MainWindow.xaml.cs:198-201` | 顺序是 `InitializeComponent()` → `DataContext = this` → `NavCommand = new RelayCommand(...)`。目前靠 WPF 绑定延迟求值侥幸生效，但 `NavCommand` 是只读属性**不发 PropertyChanged**。把 `NavCommand = ...` 提到 `InitializeComponent()` 之前 |
| 冗余 `.ConfigureAwait(true)` | `MainWindow.Tasks.cs:35` | 删掉（本就是默认值，写出来像刻意为之，误导读者） |
| `RtDisplayConverter` 硬编码 4 个状态字面量 | `MainWindow.xaml.cs:346-349`（`oauth_present`/`legacy_present`/`no_rt`/`missing`） | 与 `AccountStatusInterpreter` 的状态常量同源，抽 `RefreshTokenStatus` 静态类 |
| 魔法字符串分布（跨文件数） | `payment_method` 9、`status_code` 7、`timed_out` 6、`direct_card` 6、`protocol_payments` 5、`account_deactivated` 5、`secondary_phone_verification_required` 4 | 建议先在 `SmsWorkbench.Contracts/` 建 `BackendPayloadKeys`（IPC 载荷键）与 `ConfigKeys`（配置键）两个静态常量类，按热度迁移 |
| 冒烟测试用**反射调私有方法** | `DesktopWindowSmokeTests.cs:8` 处 `GetMethod(... NonPublic)`，如 `:317,325,447,477` | 重命名私有方法 → 测试静默变绿（`Assert.NotNull(method)` 是唯一守卫，但它只在方法彻底消失时才红）。改法：把被测逻辑提到 `internal` 并对测试程序集开 `InternalsVisibleTo` |
| `OnExit` 里 `.GetAwaiter().GetResult()` | `App.xaml.cs:91` | 退出路径阻塞可接受，保留；但建议加 `.ConfigureAwait(false)` 语义等价的注释说明 |

### 测试覆盖现状（192 `[Fact]` + 15 `[Theory]`，无 Skip）
**已覆盖**：配置合并（`SettingsServiceTests` 16、`ConfigStoreTests` 3）、IPC 序列化（`BackendJsonProtocolTests` 5、`DesktopReadProtocolTests` 7、`DesktopReadClientTests` 11）、支付批处理（`PaymentBatchServiceTests` 12、`PaymentBatchViewModelTests` 11）、`BackendCommandPlanner`(42) / `BackendResultInterpreter`(37)。

**未覆盖（按风险排序）**：`AccountStatusInterpreter`（21.8 KB，0）、`SensitiveDataSanitizer`（0）、`SettingsCatalog`（17 KB，仅被 `SettingsServiceTests` 间接读）、`ProtocolPaymentViewModel`（13.9 KB，0）、`AccountBatchProgress*`（0）、`WindowThemeService`（0）、`SmsBowerCatalogClient`（0）、`MainWindow`（仅 1 个冒烟用例）。

**未发现断言错误行为的测试**——本轮逐条看过 `ConfigStoreTests`（3 个用例断言方向正确，且已锁住空桶 bug）、`StageMatrixTests`、`PaymentBatchServiceTests`，没有发现把 bug 当规范写死的情况。

---

## 低风险可批量修清单（不改行为，可一次性提交）

1. 删 6 个死处理器：`MainWindow.Navigation.cs:34,105,131,133,135,137`。
2. 删 6 个未使用 `x:Name`：`MainWindow.xaml:351(MaxIcon), 405(SidebarHost), 468, 472, 481`（**只删 `x:Name` 属性本身**）；`App.xaml:428`。
3. 抽 `MenuIconPath` / `ToolbarIconPath` 两个 Path 样式，替换 `MainWindow.xaml` 16 处重复属性组。
4. 把 `IconNavButtonStyle` 改隐式样式，删 16 处 `Style="{StaticResource IconNavButtonStyle}"`。
5. `AccountContextMenuItemStyle` 移入 `<ContextMenu.Resources>` 隐式样式，删 10 处引用。
6. `MainWindow.xaml.cs:6` `httpClient` → `_httpClient` + 显式 Timeout（30s）。
7. `MainWindow.xaml.cs`: 把 `NavCommand = new RelayCommand<string>(OnNavigate);` 提到 `InitializeComponent()` 之前。
8. 删冗余 `MainWindow.Tasks.cs:35` 的 `.ConfigureAwait(true)`；删 `ProtocolPaymentViewModel.cs:249` 的 `GC.SuppressFinalize`。
9. 修 `MainWindow.xaml:600/615` 重复的"清空/全选"图标 Path Data。
10. 拆 `MainWindow.xaml:506-537` 统计卡片的冗余双层 `StackPanel`（`:507-508`）。

**需设计决策、不要批量改**：P0-1（改同步签名，要动调用链）、P1-1（导航重构）、P1-2/P1-3（先定"邮箱池/已注册"这两个筛选到底要不要支持）、P2-B（C# 建 UI 迁移，按对话框逐个来）。

---

## 附：本轮脚本

- `F:/tmp/audit5/cs_files.txt` — 102 个源文件的显式白名单（已剔除 `bin/ obj/ dist/ runtime/ scripts/installer/ .dotnet/`）
- `F:/tmp/audit5/s.sh` — 基于白名单的 grep 封装（**Grep 工具会扫到 `obj/**/MainWindow.g.cs`，务必用这个而非裸搜**）
- `F:/tmp/audit5/xaml_name2.py` / `x3.py` — `x:Name` 使用度分析（自动排除有 `Click=`/`TargetName` 引用的项）
- `F:/tmp/audit5/xaml_dup.py` — XAML 重复属性组检测（按 tag 聚合，忽略布局属性）
