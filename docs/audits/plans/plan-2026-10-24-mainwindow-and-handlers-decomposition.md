# MainWindow.xaml 与 registration/handlers.py 拆分方案

> **状态（2026-10-24 全部落地）**：**A0–A5、B1–B4 全部完成。**
> `MainWindow.xaml` 874 → **54 行**：窗口资源随控件归位（`Themes/MainWindowResources.xaml`
> 最终删除）、统计卡 + 筛选/分页 → `AccountFilterBar`、标题栏 → `TitleBar`、
> 侧栏 → `Sidebar`、账号表 + 上下文菜单 → `AccountGridView`、任务/日志 →
> `WorkbenchConsole`；`MainWindow.WindowChrome.cs` 删除。
> `handlers.py` 1175 → **506 行**（阶段顺序 + 生命周期 + 39 个委托）：bootstrap →
> `bootstrap_stages.py`（148）、identity → `identity_stages.py`（272）、auth →
> `auth_stages.py`（176）、register → `register_stages.py`（277）。
> 门禁：全量 pytest **6859 passed / 10 skipped**、`dotnet test` **548 passed**
> （顺带修掉 `Loc` 跨线程刷新的死锁，测试集合改为串行且稳定）、`dotnet build` 0 warning、
> `architecture_scan` / `docs_consistency_scan` / `pyright_ratchet` /
> `function_length` / `bare_print` / `unused_import`（283 基线）/
> `import_layer_ratchet`（432 边，+6 分两次复核）/ `delayed_import_ratchet`（420）/
> `format_guard` / `line_ending_guard` 全绿。
> 落地过程中新发现并已写回正文的 seam：`RegistrationWorkflow` Protocol 缺 12 个成员、
> 5 个源码扫描守卫钉在旧文件上、`TranslationBindingRegistry` 的跨线程 `Invoke` 死锁。
> 本文其余内容保留为立项时的审计快照。

## 0. 范围与非目标

| 在范围内 | 不在范围内 |
| --- | --- |
| `SmsWorkbench/Views/MainWindow.xaml`（874 行）与 16 个 `MainWindow.*.cs` 分部类（另加 `MainWindow.xaml.cs`）的**控件寻址**耦合 | 引入 `MainWindowViewModel`、改 MVVM 分层或把窗口 DataContext 换成中介对象 |
| `sms_tool/registration/handlers.py`（1175 行 / 56 个方法）的**阶段体**拆分 | 改注册阶段顺序、wire 字段、Sentinel/OTP/AT 探活逻辑、错误分类 |
| 新增 UserControl / ResourceDictionary / registration 阶段模块 | 改名 `RegistrationEmailWorkflow` 或 `handlers` 模块路径 |
| 每阶段的门禁、回滚点、补测 | 顺手修任何业务 bug（发现即单独记账，不混进搬家提交） |

## 1. 测量口径与实测结果

命令（可复现）：

```bash
# XAML 元素/名称/事件/绑定的行号与归属
python - <<'PY'   # 见 §8 附录脚本
PY
# handlers.py 的方法体量与耦合
python - <<'PY'
import ast; ...
PY
git ls-files --eol | awk '$1=="i/lf" && $2=="w/crlf"' | wc -l
```

| 指标 | 实测 |
| --- | --- |
| `MainWindow.xaml` | 874 行；`x:Name` 22 处（**16 个唯一名**）；`Click=` 4 处；其余事件处理器 4 处；`Command=` 36 处（其中 `NavCommand` 17 处） |
| 被 code-behind 引用的 `x:Name` | **8 个**：`Chrome`(模板内)、`SidebarColumn`、`SidebarToggleButton`、`SearchClearButton`、`AccountGrid`、`TaskGrid`、`LogTextBox`、`SnackbarPresenter` |
| 窗口资源块 | L19–L311，**293 行**（33%）；内含 15 个具名资源 + 1 个隐式 `ScrollBar` 样式 |
| 跨字典 `StaticResource` | **仅 1 处**：`SidebarNavButtonStyle`（L44）的 `BasedOn="{StaticResource {x:Type Button}}"` |
| `handlers.py` | 1175 行 / 56 个方法 / 941 方法行 / **703 代码行**（去注释与空行） |
| 已是 2 行委托的方法 | **31 个**（占方法数 55%） |
| 真正的方法体 | 25 个非委托方法共 **641 代码行**（前 12 个约 501 行）；最长 `prepare_identity`（109 行 / 97 代码行）、`auth_flow`（91 / 80） |
| 注释膨胀的典型 | `_abort_user_register_failure` 106 行，**代码只有 14 行**（其余是 2026-09-16 的决策记录） |
| 测试面 | **31 个测试文件**引用 `RegistrationEmailWorkflow`（**19 个实例化**）；**10 处** `patch("sms_tool.registration.handlers.<name>")`（6 个名字） |
| 函数长度棘轮 | `handlers.py` **没有任何 >150 行的函数**；63 条冻结项全在别处 ⇒ 拆分不受该棘轮阻塞 |

结论先行：

1. **XAML 的耦合比文档估计的小得多。** `docs/current/ui-architecture.md` §7 写的是
   “23 个 `x:Name`、13 个分部类跨文件引用”，实测 code-behind 只碰 **8 个名字**，
   且其中 `Chrome`、`SidebarColumn`、`SnackbarPresenter` 只在一个文件里、用法单一；
   `SidebarBrandTitle` 没有任何 code-behind 引用。**“先切断按名字访问”的前置条件已经接近完成**。
2. **`handlers.py` 已经拆过一次，剩下的是“阶段体 + 编排壳”。** 31 个 2 行委托说明
   外围（OTP / account / sentinel / finalize / resume / edge / warmup / reference）都已
   出站；真正的 640 行集中在 bootstrap、identity、auth_flow、user_register 四组。
3. **两个文件的风险都不在“搬代码”，而在静默失效的 seam。** 2026-10-21 的
   `plan-2026-10-21-architecture-remediation.md` 已经记录过：`patch()` 打的是定义模块、
   `StaticResource` 只在自己字典里查、`delayed_import_baseline` 按文件计数。本文把这些
   seam 逐条列成检查表（§3.3、§4.3）。

## 2. 现状结构

### 2.1 MainWindow.xaml 区块（行号为当前文件）

| 区块 | 行 | 行数 | 内容 | code-behind 耦合 |
| --- | ---: | ---: | --- | --- |
| `FluentWindow.Resources` | 19–311 | 293 | 侧栏/图标/上下文菜单样式、两个转换器、隐式 `ScrollBar` | 无（纯资源） |
| 标题栏 | 319–421 | 103 | 拖拽、最小化/最大化/关闭、3 个按钮模板 | 5 个处理器，全部只碰 `WindowState`/`DragMove`/`Close` |
| 侧栏列 | 428–511 | 84 | 品牌 + 8 个导航按钮 | `SidebarColumn`（动画）、`SidebarToggleButton`（tooltip） |
| 右列骨架 | 513–870 | 358 | 行定义：统计卡 / 筛选条 / 账号表 / splitter / 控制台 / 状态栏 | — |
| 统计卡 | 523–562 | 40 | 4 张计数卡 | 纯绑定 |
| 筛选/分页条 | 564–644 | 81 | 搜索、范围、页大小、6 个分页按钮 | `SearchClearButton`（可见性） |
| 账号表 | 646–800 | 155 | DataGrid + 10 项上下文菜单 + 13 列 | `AccountGrid`、`Sorting`、`SelectionChanged` |
| 控制台 | 804–865 | 62 | 任务表 + 日志框 | `TaskGrid`（滚动/选中）、`LogTextBox`（append/clear） |
| 状态栏 / Snackbar | 867–872 | 6 | 纯绑定 + presenter | `SnackbarPresenter` |

### 2.2 handlers.py 方法分组（代码行，非总行）

| 组 | 方法 | 代码行 | 外部 seam |
| --- | --- | ---: | --- |
| 生命周期/编排 | `run` `_run_stage` `_abort_result` `_set_outcome` `_close_sessions` `__init__` `r` `_abort` | 177 | 状态机、checkpoint、取消、timing |
| bootstrap | `_run_bootstrap` `_bootstrap` `_apply_reference_lane` | 80 | `_sentinel_stages`、`reference_lane`、`_safe_tock` |
| identity | `prepare_identity` `extract_sentinel` `_device_context` `_fingerprint_geo_metadata` `_device_identity_seeded_by_email_enabled` | 151 | `prepare_registration_context`、`fingerprint_pool`、`_apply_protocol_fingerprint` |
| auth | `auth_flow` `_password_lane_active` `_with_password_claim` | 122 | `auth_flow` 包、`signup_lane_verdict`、`_registration_concurrency` |
| register | `user_register` `_user_register_passwordless` `_abort_user_register_failure` `_post_user_register` | 83 | `retry_guard`、`_password_lane_active`、响应 dump |
| 开关/超时小件 | `_stage_timeout` `_otp_poll_timeout` 等 | 28 | config、`flags` |
| 2 行委托 | 31 个 | 62 | 各阶段模块 |

## 3. 方案 A：MainWindow.xaml 拆分

### 3.1 目标布局

```text
SmsWorkbench/
  Views/
    MainWindow.xaml            目标 ~250-300 行（骨架 + 布局 + 绑定）
    TitleBar.xaml/.cs          103 行
    Sidebar.xaml/.cs            84 行（视觉；列宽动画仍在 MainWindow）
    AccountFilterBar.xaml/.cs  121 行（统计卡 + 筛选/分页）
    AccountGridView.xaml/.cs   155 行（表格 + 上下文菜单）
    WorkbenchConsole.xaml/.cs   62 行（任务表 + 日志）
  Themes/
    MainWindowResources.xaml   293 行（窗口级样式，只被 MainWindow 合并）
```

硬约束（来自 `docs/current/ui-architecture.md` §2 与
`tests/workbench/test_sms_workbench_layout.py`）：

- `Themes/` 只能放资源字典，**不能有 code-behind**；
- `StaticResource` 必须在自己的字典内解析，跨字典只允许在合并顺序中已加载的键；
- 每个 UserControl 的 `Source=` 相对路径必须从 XAML 自身目录可解析；
- UserControl **不设置自己的 DataContext**，直接继承窗口的 DataContext，这样
  `{Binding Pool.PagedRows}` 等 36 处绑定一行不改。

### 3.2 分阶段（按风险从低到高）

| 阶段 | 内容 | 删除的 code-behind 引用 | 前置条件 | 回滚 |
| --- | --- | --- | --- | --- |
| **A0** | 窗口资源 → `Themes/MainWindowResources.xaml` | 0 | 处理唯一跨字典键：`SidebarNavButtonStyle` 的 `BasedOn="{StaticResource {x:Type Button}}"`。它已自带完整 `Template`，**删除 BasedOn 并补齐 Foreground/Padding/FontSize** 即可自包含；若坚持保留 BasedOn，则把该样式留在 `MainWindow.xaml`（只搬其余 14 个） | 单文件删除 + smoke test |
| **A1** | 统计卡 + 筛选/分页条 → `AccountFilterBar` | `SearchClearButton` | `PoolViewModel` 增加 `HasSearchText`（`SearchText` 变化时 `OnPropertyChanged`），XAML 用 `BooleanToVisibilityConverter`；删除 `MainWindow.ContextMenu.cs:16` 的 `UpdateSearchClearVisibility` 与 `MainWindow.xaml.cs:173` 的调用 | 控件文件删除 + 还原 2 处绑定 |
| **A2** | 标题栏 → `TitleBar` | 0 | 5 个处理器搬进 UserControl，用 `Window.GetWindow(this)` 取宿主；`MinimizeButton`/`MaximizeButton`/`CloseButton` 仅是 XAML 名，无外部引用 | 控件文件删除 |
| **A3** | 侧栏视觉 → `Sidebar` | `SidebarToggleButton`、`SidebarBrandTitle` | `SidebarCollapsed` 保留在 MainWindow（`SidebarColumn` 与列宽动画都在窗口里）；控件暴露 `IsCollapsed` DP + `ToggleRequested` 事件，tooltip/几何/图标在控件内部用触发器切换。**动画不能搬**，因为 `SidebarColumn` 是窗口 Grid 的列 | 控件文件删除 + 还原触发器 |
| **A4** | 控制台 → `WorkbenchConsole` | `TaskGrid`、`LogTextBox` | 控件继承 DataContext，暴露 `ScrollToLast()` / `ClearLog()`；`LogPanel.Cleared` 订阅搬进控件；`TaskGrid_Loaded` 搬到控件 | 控件文件删除 |
| **A5** | 账号表 + 上下文菜单 → `AccountGridView` | `AccountGrid` | 控件暴露 `SelectedRow`、`ClearSelection()`、`SortRequested`；`AccountGrid_Sorting`（`MainWindow.Navigation.cs:111`）与 `SelectionChanged`（`MainWindow.Tasks.cs:98`）搬进控件，排序仍调用继承 DataContext 的 `Pool.SortBy`；`SelectedRowsOrCurrent()` 里的 `accountGrid.SelectedItem` 兜底改成控件的 `SelectedRow` | 控件文件删除；这一步最大，独立提交 |

### 3.3 seam 检查表（每阶段逐条核对）

| seam | 现状 | 拆分后必须做的 | 不做的后果 |
| --- | --- | --- | --- |
| `x:Name` 生成字段 | 分部类直接写 `SidebarToggleButton` 等 | 搬控件前先把引用改成 DP / 方法；否则编译期整批失效（不会只坏一个） | 编译错误（可发现，成本低但面积大） |
| `StaticResource` 跨字典 | 仅 `{x:Type Button}` | A0 的自包含方案；smoke test `FindResource` 断言 | 运行时 `XamlParseException`，只有打开窗口才暴露 |
| DataContext | 窗口即 DataContext | UserControl 不设 DataContext；如果设了，36 处绑定静默失效 | 界面空白，无编译错误 |
| 上下文菜单 | `PlacementTarget.DataContext` 取窗口 DataContext | 菜单跟随 DataGrid 移动，绑定原样保留 | 菜单命令全失效（无报错） |
| `FindName("AccountGrid")` | `MainWindow.xaml.cs:153` | A5 时删掉，改用控件属性 | 启动即抛 `InvalidOperationException` |
| 事件处理器 | 4 个 Click + 4 个 DataGrid/标题栏事件 | 随控件搬走；窗口不再订阅 | 按钮无响应（无报错） |
| 语言切换 | `TranslationBindingRegistry` 按 (target, property) 记录 | UserControl 内部的 `{loc:Tr}` 仍会注册，无需改 | 切换语言后部分控件保持旧语言 |
| 布局测试 | `Source=` 相对路径、Themes 无 C# | 新增 XAML 放 `Views/`，资源字典放 `Themes/` | `test_sms_workbench_layout` 红 |

### 3.4 测试

- **每阶段必跑**：`dotnet build -c Release`（0 warning）、`dotnet test`（当前 548）、
  `pytest tests/workbench/test_sms_workbench_layout.py`、
  `pytest tests/workbench/test_localization_catalog.py`。
- **A0/A2/A3/A4/A5 各补一条模板断言**到
  `tests/SmsWorkbench.Tests/DesktopWindowSmokeTests.cs`：真实 `App` + STA 线程打开窗口，
  `FindResource("SettingsSelectionBg")` 之外再断言新控件的 `Template.FindName` 与
  `ActualWidth > 0`。这是资源与样式唯一的运行时证据，不能只靠 XAML 文本检查。
- **A1 补单测**：`PoolViewModel.HasSearchText` 在 `SearchText` 置空/非空时的通知。
- **A5 补一条行为断言**：排序点击后 `Pool.SortBy` 被调用（ViewModel 已有排序测试，
  只补“控件把事件转成调用”这一层）。

## 4. 方案 B：handlers.py 拆分

### 4.1 目标模块

| 新模块 | 迁入方法 | 代码行 | 依赖方向 |
| --- | --- | ---: | --- |
| `registration/bootstrap_stages.py` | `_run_bootstrap` `_bootstrap` `_apply_reference_lane` | 80 | → sentinel / reference_lane / timing |
| `registration/identity_stages.py` | `prepare_identity` `extract_sentinel` `_device_context` `_fingerprint_geo_metadata` `_device_identity_seeded_by_email_enabled` | 151 | → state / fingerprint_pool / sentinel |
| `registration/auth_stages.py` | `auth_flow` `_password_lane_active` `_with_password_claim` | 122 | → auth_flow 包 / flags / concurrency |
| `registration/register_stages.py` | `user_register` `_user_register_passwordless` `_abort_user_register_failure` `_post_user_register` | 83 | → retry_guard / account_stages |
| `registration/handlers.py`（保留） | `run` `_run_stage` `_abort_result` `_set_outcome` `_close_sessions` `__init__` + 31 个 2 行委托 + `_apply_protocol_fingerprint` | ~330 | 唯一的阶段顺序 owner |

拆分后 `handlers.py` 1175 → **约 330–380 行**（−68%），四个新文件合计约 436 行。
**阶段顺序、错误名、结果契约一行不改。**

### 4.2 分阶段

| 阶段 | 内容 | 兼容策略 | 回滚 |
| --- | --- | --- | --- |
| **B0** | 先建“所有权测试”：断言每个阶段体只在一个模块里定义（`handlers` 只剩委托） | 新测试先红后绿，不改生产代码 | 删测试 |
| **B1** | `bootstrap_stages.py`（最小、无 patch 目标） | `handlers._run_bootstrap` 保留 2 行委托 | 删文件 + 还原方法体 |
| **B2** | `identity_stages.py` | 同上；**`_new_registration_session` 的 3 处 patch 改为打新模块**（或把该 helper 留在 `protocol_helpers`，新模块从那里导入） | 删文件 + 还原 |
| **B3** | `auth_stages.py` | 同上；`_password_lane_active` 是 `register` 组的调用点，保留委托即可 | 删文件 + 还原 |
| **B4** | `register_stages.py` | `_abort_user_register_failure` 的 90 行注释一并搬走；`DEAD_END_SIGNUP_ROUTED_TO_LOGIN` 从 `retry_guard` 导入 | 删文件 + 还原 |

方法体搬迁要满足**“逆向归一化后 AST 相同”**：把 `def method(self, ...)` 改成
`def method(workflow: RegistrationWorkflow, ...)`，函数体内 `self.` → `workflow.`，其余
逐语句不变；用脚本做替换，并像 `plan-2026-10-03-upi-pipeline-stage-split` 那样做一次
**反向往返**（把新函数还原成方法后与 HEAD 原文比较 AST）作为机器证明。

### 4.3 seam 检查表

| seam | 实测/现状 | 拆分后必须做的 | 不做的后果 |
| --- | --- | --- | --- |
| `patch.object(RegistrationEmailWorkflow, ...)` | 19 个测试文件实例化 / 31 个引用 | **保留类方法委托**，`run()` 继续调用 `self.auth_flow()`；测试对类打 patch 仍然生效 | 大量测试静默测到真实实现 |
| `patch("...handlers.<name>")` | 10 处调用 / 6 个名字：`_new_registration_session`×3、`_apply_protocol_fingerprint`×3、`set_auth_fingerprint`、`collect_codex_oauth_tokens`、`RegistrationRetryGuard`、`RegistrationEmailWorkflow` | 把被搬走方法体内的模块级名字，改为从新模块解析；同步改这 10 处 patch 目标（或在 `handlers` 保留该名字并让新模块**函数内**导入它——不推荐，等于制造 seam） | patch 打空，测试绿但测的是真配置/真网络 |
| `delayed_import_baseline` | `handlers.py` 记 2 条 | 函数内 import 随方法迁移后，逐文件计数变化；更新 `delayed_import_baseline.json` 与 `delayed_import_reasons.json` | `test_import_layer_ratchet` / `delayed_import_ratchet` 红 |
| 同目录 import | — | 新模块 `from .state import ...` 是**同目录边**，不计入 `import_layer_ratchet` 的跨目录总数 | 无 |
| `RegistrationWorkflow` Protocol | `state.py:99-178` 手抄 33 个方法 | 新模块用 `TYPE_CHECKING` 注解 `RegistrationWorkflow`（已在 `account_stages.py` 证明无环） | 注解解析成环（只有类型检查期可见） |
| 状态字段写入扫描 | `test_registration_state_assignment_guard` 扫整个 `sms_tool/` | 无需改（它不按文件白名单） | — |
| 函数长度棘轮 | `handlers.py` 无 >150 行函数 | 新模块同样保持 <150；搬家不新增超长函数 | 新文件被棘轮抓住 |
| `__all__` / 门面 | `handlers.__all__` 导出 13 个名字 | 保持 `RegistrationEmailWorkflow`、`StorageRegistrationPersistence`、`_apply_protocol_fingerprint` 等的导入路径不变 | 31 个测试文件的 import 断裂 |
| 文档锚点 | `protocol-registration.md` 的 Owners 表写“`registration/handlers.py` 拥有阶段顺序” | 拆分后更新 Owners 表，新增四个模块各自拥有什么；`dependency-graph.md` 重新生成 | `docs_consistency_scan` 红（符号表/路径） |

### 4.4 测试

- **B0 所有权测试**（新增 `tests/registration/test_handlers_module_ownership.py`）：
  - 每个阶段名在且仅在目标模块定义；
  - `handlers.py` 的同名方法体 ≤ 3 行且是委托；
  - `handlers.py` 行数上限（例如 450）作为防回涨棘轮。
- **等价性**：`tests/registration` 现有 1961 个用例全绿是主证据；另外用
  “反向往返 AST 比较”作为机器证明（见 §4.2）。
- **patch 迁移测试**：10 处 patch 调用（6 个名字）改完后，各补一条“patch 生效”断言
  （否则又是静默失效）。
- **每阶段必跑**：`pytest tests/registration tests/governance`、`ruff check`、
  `scripts/architecture_scan.py`、`scripts/docs_consistency_scan.py`、
  `scripts/import_layer_ratchet.py --detail`、`scripts/delayed_import_ratchet.py`。

## 5. 验收门禁（每个提交）

```bash
dotnet build GPTRegisterTool.slnx -c Release      # 0 warning
dotnet test  GPTRegisterTool.slnx -c Release
python -m pytest -q tests/registration tests/workbench tests/governance
python scripts/architecture_scan.py
python scripts/docs_consistency_scan.py
python scripts/module_coverage_check.py
python scripts/import_layer_ratchet.py
python scripts/delayed_import_ratchet.py
python scripts/function_length_ratchet.py
python scripts/bare_print_ratchet.py
```

## 6. 风险与明确不做

| 风险 | 缓解 |
| --- | --- |
| UserControl 设置自己的 DataContext | 代码评审 + 每阶段 smoke test |
| `StaticResource` 跨字典（A0 唯一一处） | 自包含字典 + `FindResource` 断言 |
| patch 目标静默失效 | §4.3 的 10 处调用清单 + 每处补“patch 生效”断言 |
| 搬方法时顺手改了逻辑 | 逆向 AST 往返比较；diff 只允许 `self.`→`workflow.` 与签名行 |
| 中间态不可提交 | 每阶段一个提交，先加委托再删旧体；不跨阶段混提交 |

**明确不做**：

1. 不引入 `MainWindowViewModel` 或把窗口 DataContext 改成中介对象——36 处绑定与 19 个
   测试会一起动，收益与风险不成比例。
2. 不把 `SidebarColumn` 的列宽动画搬进 UserControl（列定义在窗口 Grid 里）。
3. 不重命名 `handlers.py` / `RegistrationEmailWorkflow`（31 个测试文件引用 + `__all__` 契约）。
4. 不合并/改写任何阶段逻辑、错误名、wire 字段；发现的问题单独开账。
5. 不拆 `AccountGrid` 的列定义成独立资源（列与 `PoolRow` 属性一一对应，拆开只增加跳转成本）。

## 7. 顺序与工作量

| 顺序 | 阶段 | 预估 | 依赖 |
| ---: | --- | --- | --- |
| 1 | A0 资源字典 | 0.5 天 | 无 |
| 2 | A1 筛选条 | 0.5–1 天 | `PoolViewModel.HasSearchText` |
| 3 | A2 标题栏 | 0.5 天 | 无 |
| 4 | B1 bootstrap | 0.5 天 | B0 所有权测试 |
| 5 | B2 identity | 1–1.5 天 | patch 迁移 3 处 |
| 6 | B3 auth | 0.5–1 天 | — |
| 7 | B4 register | 1 天 | — |
| 8 | A3 侧栏 | 1 天 | `SidebarCollapsed` DP |
| 9 | A4 控制台 | 0.5–1 天 | — |
| 10 | A5 账号表 | 1.5–2 天 | 排序/选择事件迁移 |

合计约 **8–10 个工作日**（不含 live 回归）。A1/A2/B1 是低风险起点，可先做一轮验证
“每阶段单独提交 + 门禁全绿”的节奏。

## 8. 附录：测量脚本

```python
# 1) XAML：x:Name、Click、Command、区块行号
import re, xml.parsers.expat
from pathlib import Path

x = Path("SmsWorkbench/Views/MainWindow.xaml").read_text(encoding="utf-8")
print(len(x.splitlines()))
print(re.findall(r'x:Name="([^"]+)"', x))
print(re.findall(r'Click="([^"]+)"', x))
print(len(re.findall(r'Command="\{Binding ', x)))
# 元素树行号（expat 的 CurrentLineNumber）

# 2) handlers.py：方法体量（代码行 = 去掉注释与空行）
import ast, io, tokenize

p = Path("sms_tool/registration/handlers.py")
src = p.read_text(encoding="utf-8")
cls = [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)][0]
for fn in cls.body:
    if isinstance(fn, ast.FunctionDef):
        print(fn.name, fn.end_lineno - fn.lineno + 1)

# 3) code-behind 对 x:Name 的引用
for name in set(re.findall(r'x:Name="([^"]+)"', x)):
    hits = [
        (p.name, len(re.findall(rf"\b{name}\b", p.read_text(encoding="utf-8-sig"))))
        for p in Path("SmsWorkbench/Views").glob("MainWindow*.cs")
    ]
    print(name, [h for h in hits if h[1]])
```
