# 立项：WPF 视觉系统重构（对齐 FlClash 的表面/状态/刻度原则）

- **日期**：2026-10-28
- **状态**：**Phase 0–5 全部落地**（§6 是带实测证据的落地记录）
- **范围**：`SmsWorkbench/Themes/`、`SmsWorkbench/Views/*.xaml`、`MainWindow.Theme.cs`、`IconNavButton.cs`、`Sidebar.xaml(.cs)`
- **不在范围**：任何后端契约、ViewModel 业务逻辑、Python 侧、`SmsWorkbench.Contracts`
- **参考**：https://github.com/chen08209/FlClash —— 只取它的**设计准则**（`.agents/surfaces.md`、`.agents/skills/ui-work/SKILL.md` 的圆角刻度与字阶），不移植 Material 3 控件
- **相关**：`docs/directory-map.md`（`SmsWorkbench/` 归口）、`docs/architecture.md` Rule 10

---

## 0. 结论先行

现有前端**组件库不缺**（WPF-UI + 自绘 690 行 `Controls.xaml`），缺的是**一套判定"这一层该长什么样"的规则**，于是每个视图各自用边框、各自写 margin、各自写字号：

| 现状 | 证据 |
| --- | --- |
| 色板写两遍：XAML 一份、C# 里 100+ 个 `SetBrush("#hex")` 一份 | `Palette.xaml` / `MainWindow.Theme.cs:42-130` |
| 文字三级层级被压成一个值（`TextMain = TextSub = TextMuted`） | `Palette.xaml`，光 `#58595C` / 暗 `#DFDFDF` |
| 全站圆角 4；侧栏 19 个动作行每个都是 1px 描边方块 | `Controls.xaml`（`CornerRadius="4"` ×9） |
| 状态徽章 5 套容器底色，5 个前景色全是同一个灰 | `Palette.xaml` |
| 选中行是灰块 `#C8C8C8` | `Controls.xaml:490-548` |
| 间距/字号/图标几何全散落（`0,0,8,0`×11；19 个内联 `Path Data`） | `Views/*.xaml` |

**一处需要修正的前提（复核 2026-10-28）**：立项初稿判断「侧栏是 19 个没有选中态的导航目的地」。读代码后不成立 —— `MainWindow.Navigation.cs` 的 `NavActions` 把每个键路由到**一次性动作或对话框**（批次、导入导出、打开窗口），应用是**单页工作台**（账号表 + 控制台），没有可"选中"的目的地；侧栏也已经分成 5 组（`一键操作 / 支付管理 / 邮箱管理 / 账号管理 / 项目设置`）。因此：

- 侧栏不做"假选中态"，改做**真实状态**：`IsActive` = **该行动作正在运行**（批次运行期间高亮，结束清除）。
- 分组不再重做，只修**层级与危险动作的可见性**。

---

## 1. 从 FlClash 取的四条原则

原文（`.agents/surfaces.md`）：*"A surface stands out from what it sits on by a step in lightness, never by a border. Color appears where something is selected, on, failing, or the one action a screen is for."*

1. **靠明度分层，不靠边框**。四级表面 `page / card / block / inset`，每级比所在层深一档；光色在"灰↔纸"之间交替，**暗色只向上变亮**（更深的一档在暗底上读作"洞"而不是"层"）。
2. **颜色只表态**：选中 / 开启 / 失败 / 这一屏唯一的动作。选中项**保留本层底色**，用 primary 标记说话。
3. **圆角由最短边决定**；同心圆角是**推导**出来的（`outer = inner + inset`），不新增刻度。
4. **只留三种边框**：键盘焦点环、输入框、窗口 chrome 与内容的分界；分隔线用 `onSurface` 14% 半透明。

**不移植**：Liquid Glass、iPhone 弹性 sheet、悬浮 dock、超椭圆圆角（WPF 只有圆形半径）、Monet 动态取色。产品是**信息密集的桌面工作台**，FlClash 的"每屏一个动作"降级为"每屏一个**主**动作 + 密集表格"。

---

## 2. 令牌表（Phase 0 的产物，`Themes/Tokens.xaml` + `Colors.*.xaml`）

### 2.1 表面阶梯（`Surface.*`）

| 键 | 光 | 暗 | 用途 |
| --- | --- | --- | --- |
| `Surface.Page` | `#F3F3F3` (94) | `#181818` (6) | 窗口底、控制台外层 |
| `Surface.Card` | `#FFFFFF` (98) | `#232323` (15) | 卡片、面板、表格行 |
| `Surface.Block` | `#F3F3F3` | `#2B2B2B` (21) | 卡片内的块：工具条、未选中段 |
| `Surface.Inset` | `#EAEAEA` | `#333333` (27) | 块内小块：徽章、轨道、日志井 |
| `Surface.Chrome` | `#EDEDED` | `#1E1E1E` | 侧栏/标题栏（离页面 4 档） |

> 光色交替（卡比页亮、块比卡暗），暗色单调变亮。原 `PanelBg/PanelBg2/PanelHover/GridAltBg/SidebarBg/StatusBg/LogBg` 全部映射到这四个别名上，**旧键保留**（`WindowThemeService` 与测试都按 `PanelBg`/`TextMain` 取色）。

### 2.2 文字阶梯（修复"三级同值"）

| 键 | 光 | 暗 | 用途 |
| --- | --- | --- | --- |
| `TextMain` | `#1F2328` | `#E6E6E6` | 标题、数值、正文 |
| `TextSub` | `#57606A` | `#A8B0B8` | 次级说明、表头、标签 |
| `TextMuted` | `#6E7781` | `#7D858D` | 分组标题、占位、元信息 |

对比度：`TextMain` 15.7:1 / `TextSub` 6.4:1 / `TextMuted` 4.9:1（白底）；暗底 12.6 / 7.1 / 4.7，均 ≥ 4.5:1。

### 2.3 圆角刻度（`Radius.*`，按最短边取，不写字面量）

FlClash 的刻度以 40dp 行高为基准；WPF 控件高 28–40px，等比缩小：

| 键 | 值 | 用于 |
| --- | --- | --- |
| `Radius.Xs` | 4 | inset 小块、徽章 |
| `Radius.Sm` | 6 | chip、小方块按钮 |
| `Radius.Md` | 8 | 输入框、按钮、菜单 |
| `Radius.Lg` | 10 | 侧栏行（36–40 高）、工具条 |
| `Radius.Xl` | 12 | 卡片、面板、控制台 |
| `Radius.Full` | 999 | 药丸（分页、状态点） |

### 2.4 间距刻度（`Space.*`，4 的倍数）

`Space.1=4 · Space.2=8 · Space.3=12 · Space.4=16 · Space.5=24 · Space.6=32`（`Thickness` 形式另给 `Space.H1..H3` 常用水平边距）。

### 2.5 字阶（`Type.*`，对应 iOS Dynamic Type 的缩小版）

| 键 | 字号/行高 | 粗细 | 用途 |
| --- | --- | --- | --- |
| `Type.Display` | 28 / 34 | SemiBold | 空态数字、品牌 |
| `Type.Title` | 20 / 26 | SemiBold | 窗口/区块标题 |
| `Type.Subtitle` | 15 / 20 | SemiBold | 卡片标题、段落头 |
| `Type.Body` | 13 / 18 | Normal | 正文、表格 |
| `Type.Caption` | 11 / 15 | Medium | 分组标题、表头、徽章 |

---

## 3. 阶段与验收标准

### Phase 0 · 令牌层（零视觉变化）

#### 改动
- 新增 `Themes/Tokens.xaml`（间距/圆角/字阶，与主题无关）与 `Themes/Colors.Light.xaml` / `Themes/Colors.Dark.xaml`（**同一套键**，含 2.1/2.2 的全部键 + 原有 badge/accent 键）。
- 删除 `Themes/Palette.xaml`；`App.xaml` 合并顺序固定为 `Wpf.Ui → Tokens → Colors.Light → Controls`。
- `MainWindow.Theme.cs` 的 100 行 `SetBrush` 换成**换字典**（按 index 替换 `Colors.*`），`WindowThemeService` 保留（DWM 标题栏色仍从 `PanelBg`/`TextMain` 取）。

#### 验收
1. `dotnet build` 0 warning（`dotnet_warning_ratchet` 基线 0）。
2. 切换主题：所有已打开的窗口（设置、支付、代理池、进度）同帧变色，无需 `ApplyToOpenWindows` 逐窗刷。
3. `DesktopThemePreference` 的读写行为不变（`DesktopWindowSmokeTests` 覆盖）。
4. 视觉：Phase 0 前后截图逐像素一致（本阶段只搬位置，不改值）。

### Phase 1 · 表面阶梯 + 去边框 + 侧栏真实状态

#### 改动
- 侧栏/面板/表格/控制台的 `BorderThickness="1"` 结构性描边改为 `Surface.*` 明度差；只保留 chrome 分界线与输入框/焦点环。
- 侧栏行：透明底 → hover `Surface.Block` → **运行中 `Surface.Card` + 3px primary 左竖条**；行高 38→40、圆角 10。
- 新增 `IconNavButton.IsActive`（DP）+ `Sidebar.ActiveKey`（DP）+ `NavKeyEqualsConverter`（MultiBinding，无 code-behind 遍历）。
- `MainWindow.ActiveNavKey`：`OnNavigate` 记 pending key，`RunBackendCoreAsync` 提升为 active 并在 `finally` 清除 —— 只对**批次**生效（一次性动作不闪）。
- 表格选中：灰块 → 行保留 `Surface.Card` + 3px primary 竖条。
- 文字：标题/正文/次级/元信息分别落到 `TextMain/Sub/Muted`。

#### 验收
1. 任一视图不再有结构性 1px 描边（`grep BorderThickness="1"` 只剩输入框、焦点环、chrome 分割）。
2. 运行「一键注册」批次：该行高亮；批次结束（成功/取消/失败）高亮消失，其它行从不误亮。
3. `DesktopWindowSmokeTests` 全绿（`SidebarToggleButton`/`SidebarBrandTitle` 名字不变）。

### Phase 2 · 颜色纪律

#### 改动
- `PrimaryButton` 改为**填充 primary + 白字**（每屏唯一主行动）；`SecondaryButton` 中性、`DangerButton` 只染文字与描边。
- 状态徽章：颜色落到**文字**（`Badge*Fg` = 语义色），容器保持低彩度同色；`Badge*Bd` 归零（`BorderThickness=0`）。
- 控制台改用 `Surface.Inset`，去掉描边。

#### 验收
1. 徽章在光/暗两色下文字对比度 ≥ 4.5:1，且"成功/警告/危险/信息"四态**仅凭颜色可区分**（不靠读字）。
2. 每个对话框只有一个填充按钮（`grep -c "PrimaryButton"` 每视图 ≤ 1）。

### Phase 3 · 账号表格（产品价值最高）

#### 改动
- 行高 36→40，cell padding 6→10，表头 28→32 且用 `Type.Caption`；关掉斑马纹与单元格下边框，改 hover 填充。
- 列分层：B 级列（创建/更新时间、注册区、优惠、支付资格）收进「精简列」开关（XAML 内切换 `Visibility`，不动 ViewModel）。
- 开启列重排、锁定行高、去掉本地 `RowHeight`/`GridLinesVisibility` 覆盖（它们会让样式失效）。
- **窄窗卡片视图**：内容区 < 960px 时表格换成两行卡片（邮箱 + 状态徽章 / 类型 + AT·RT·2FA·优惠），
  两个视图共享 `Pool.PagedRows` 与 `Pool.SelectedRow`，选区切换复用同一个 `SelectionChanged` 处理器，
  因此勾选与选中语义完全一致。

#### 验收
1. 1220px（最小窗宽）下不出现横向滚动即可完成"A 级判定"（状态/邮箱/AT/RT/2FA 全可见）。
2. 打开「精简列」后列数 12→7，表格宽度回收 ≥ 400px。
3. 排序、多选、右键菜单、`AccountGridPresentation` 行为不变（`AccountGridPresentationTests` 全绿）。

### Phase 4 · Chrome 与布局

#### 改动
- 侧栏/标题栏用 `Surface.Chrome` + 与内容之间的 1px `onSurface 14%` 分界；标题栏底部同一条线。
- 控制台 320px 固定 → `GridSplitter` 可拖（160–520），比例在会话内保持。
- 「取消批次」在运行中升为危险色（真实状态驱动）。
- 主题/语言切换从侧栏移入标题栏（侧栏只留业务动作）。

#### 验收
1. 拖动分隔条可改控制台高度，且上下限生效；表格区域随之伸缩。
2. 批次运行中「取消批次」为危险色，空闲时中性。
3. 标题栏在光/暗下都有分界线，Mica 生效。

### Phase 5 · 焦点与动效收尾

#### 改动
- `FocusVisualStyle`：全站 2px primary 焦点环（现为 `{x:Null}`，键盘用户无落点）。
- 交互态统一：hover 填充 120ms、press 透明度 0.9；侧栏折叠沿用既有动画曲线。
- 图标：19 处内联几何收敛到 `Themes/Glyphs.xaml`（`Geometry` 资源键），视图只引用键。

#### 验收（实测）
1. 焦点环：`FocusRingStyle`（2px `FocusRing`）接入 Button、侧栏行与卡片项；输入框改为 hover/focus 换描边色。
2. 图标字表：**23 个内联字形全部收敛**到 `Themes/Glyphs.xaml`（32 个键；同一几何复用同一个键，
   例如侧栏与右键菜单共用 `Glyph.BatchPay`/`Glyph.ChangeEmail`/`Glyph.ImportMail`/`Glyph.PayLink`）。
   `grep -rc 'Data="M' Views/*.xaml` = **0**（AccountFilterBar / AccountGridView / TitleBar / WorkbenchConsole 各 0）。
3. 取消行状态染色：`IconNavButton.IsAlert` ← `NavCancelAlertConverter`（`CommandParameter == cancelbatch`
   **且** `ActiveKey` 非空）→ 文字与字形转 `Danger`。空闲时不染色（颜色只表状态）。
4. WPF 无隐式过渡，"统一缓动"降级为统一交互态（hover 填充 / press 0.9 / 焦点环），已在 §4 记录原因。

---

## 4. 风险与约束

- **`dotnet_warning_ratchet` 基线是 0**：删 C# 主题代码不能留未使用成员。
- **测试钉住的资源键与模板部件**：`SettingsSelectionBg/Border`、`ItemChrome`、`PART_ContentHost`、`ResultsGrid`、`SidebarToggleButton`、`SidebarBrandTitle` 必须保留原名。
- **WPF 没有隐式过渡动画**：`Setter` 触发是瞬时的；动效只能靠 `Storyboard` 或既有 `DoubleAnimation`（侧栏折叠）。Phase 5 因此把"统一缓动"降级为"统一交互态"，并在本文件记录原因。
- **`StaticResource` 跨字典**：`Tokens.xaml` 必须合并顺序在前；顺序错了会在第一次解析时**硬失败**（不是静默降级），这是可接受的失败模式。

---

## 5. 不做

- Material 3 控件移植、Liquid Glass、Monet 取色、超椭圆圆角。
- 侧栏"当前页"选中态（单页工作台，无此状态；改为运行中标记）。
- 表格卡片化自适应视图（需要 ViewModel 层改动，风险高于收益，留待独立立项）。

---

## 6. 落地记录

| 阶段 | 结果 |
| --- | --- |
| Phase 0 | `Themes/Tokens.xaml`（间距/圆角/字阶/动效）+ `Colors.Light.xaml` + `Colors.Dark.xaml`（同键集，含 5 级表面与 3 级文字）；删除 `Palette.xaml`；`MainWindow.Theme.cs` 120 行 `SetBrush` → 一次字典替换（pack URI，测试宿主也能解析） |
| Phase 1 | 表面阶梯落地（`Surface.Page/Card/Block/Inset/Chrome`、`Divider` = onSurface 14%）；按钮/输入框/表格/徽章的结构性描边清零，只留输入框、焦点环、chrome 分界；侧栏行透明→hover 填充→**运行中 Card + 3px primary 竖条**（`IconNavButton.IsActive` ← `Sidebar.ActiveKey` ← `ActiveNavKey`，由 `RunBackendCoreAsync` 的 finally 清除）；表格选中由灰块改为行内竖条 |
| Phase 2 | `PrimaryButton` 填充 primary + `OnPrimary`；`DangerButton` 只染文字与描边；5 组徽章颜色落到文字（容器低彩度同色、`BorderThickness=0`）；控制台改用 `Surface.Inset` |
| Phase 3 | 行高 40 / 表头 32（`Type.Caption`）/ cell padding 10；**去掉斑马纹、单元格边框、网格线**；新增「精简列」开关隐藏 5 个 B 级列（按 `SortMemberPath`/绑定路径定位，因 `DataGridColumn` 不在名字域内、`x:Name` 不生成字段）；开列重排、行高锁定；**窄窗卡片视图**（内容区 < 960px 自动切换，`NarrowWidthConverter`，两视图共用同一数据源/选区/勾选语义） |
| Phase 4 | 侧栏 `Surface.Chrome` + 与内容之间的 1px `Divider`；标题栏底边同一条线；控制台 320px 固定 → `GridSplitter` 可拖（160–560） |
| Phase 5 | 焦点环接入；**23 个内联字形全部收敛**到 `Themes/Glyphs.xaml`（32 键）；取消行在批次运行中染 `Danger`（`NavCancelAlertConverter`） |

实测：`dotnet build`（Debug / Release / Release-win-x64）0 warning / 0 error，`dotnet test` **622 passed / 0 failed**（含 `DesktopWindowSmokeTests`）。
两个由本阶段引入并当场修掉的缺陷，记在此处以免重犯：
① `CornerRadius` 用了 `Radius.*`（`double`）而非 `Corner.*`（`CornerRadius`）令牌 → 模板加载抛
`XamlParseException`，被 `SettingsPaymentAndSharedControlsLoadOnStaThread` 抓到；
② 颜色字典用相对 URI 加载，在 `dotnet test` 宿主里解析失败，改为显式命名 assembly 的 pack URI；
③ `NarrowWidthConverter` 误用 `IMultiValueConverter` 的 `ConvertBack` 签名（`object[]`），编译器直接拦下。

验证命令见 §7。

## 7. 复现

```powershell
dotnet build GPTRegisterTool.slnx -c Release      # 0 warning
dotnet test GPTRegisterTool.slnx -c Release       # 桌面冒烟 + 契约
python scripts/dotnet_warning_ratchet.py
python -m pytest -q tests/workbench               # C#→Python flag 漂移
```
