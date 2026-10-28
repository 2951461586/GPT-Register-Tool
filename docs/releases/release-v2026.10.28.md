# v2026.10.28

自 v2026.10.27 以来的收口：按优先级清理上一轮扫描（协议注册 / 代理池与指纹库 / WPF 前端）
留下的 P0–P3 项，并补齐 WPF 视觉系统计划里**实际未达标**的验收条目。

## 新增功能

- **账号详情页「身份」分组** —— 详情面板新增出口（`scheme://host:port`）、出口国家与
  指纹档案三行。这些数据每个账号早就存在 `sessions/*.json` 的 `identity_context` 里
  （`proxy_affinity` + `fingerprint_key`），但桌面端从未展示，导致「两个账号有何不同」
  这件产品核心事实在管理它们的界面里不可见。后端 `desktop_read` 新增 `identity` 块，
  **白名单字段**（`bound` / `proxy_host` / `proxy_port` / `proxy_country` /
  `proxy_pool_index` / `fingerprint_profile` / `device_id` /
  `auth_session_logging_id` / `registration_country`），不含 `session_id`，
  也不转发未来新增字段。无出口的账号**省略该行**而不是显示破折号——那是真实状态。
- **lane 隔离可见、可声明** —— `proxy.registry.lane_isolation()` 把
  「mailbox / payment_qualification / payment 是否复用注册出口」变成结构化结果；
  桌面代理池窗口顶部新增告警条（冲突染 `Danger`，切主题不失效）。复用有时是刻意的容量
  决定，因此新增 `proxy.allow_lane_overlap`：声明过的 lane 不再算**冲突**
  （`lane_isolation.ok` 转真），但**仍留在 `overlaps` 里**并在 census 中标注
  `[declared in proxy.allow_lane_overlap]`，决定本身保持可复核。
  `validate_config` 校验该键（未知 lane 名直接报错，理由同 `proxy.lanes`：读取方会忽略
  不认识的 lane，拼错就等于「以为声明了、census 仍在报警」）。
- **代理池窗口的 lane 隔离摘要** —— `admin.list_pool` 现在携带 `lane_isolation`
  （仅配置解析，无网络、无支付方法目录），桌面端不必再跑 `proxy_census.py` 才能看到。

- **标题栏版本号与自动检查更新** —— 标题栏左侧新增 `GRT → 版本号 → 更新胶囊`：
  版本号可点（立即检查），有上游新版本时出现胶囊（点开 GitHub release 页）。
  版本号读 `AssemblyInformationalVersion`（`Directory.Build.props` 的 `Version`，
  发布时由 `build_dotnet.ps1 -Version` 盖戳），未盖戳构建显示 `dev` 而非 `v0.0.0`。
  检查走 GitHub Releases API，6 小时节流并缓存到 `runtime/update_check.json`
  （未认证配额 60 次/小时）；失败一律返回结果对象，自动检查静默、手动检查才提示；
  永不自动替换文件。仓库可用 `GRT_UPDATE_REPO` 覆盖。
- **侧栏与全站图标换用 Tabler** —— `Themes/Glyphs.xaml` 的 34 个功能图标改为
  [tabler-icons](https://github.com/tabler/tabler-icons) v3.50.0（MIT）outline 集的真实
  路径数据，逐条照抄 `d` 属性并在键上方标注来源图标名。此前注册表混用 Feather 风格
  24×24 轮廓与手绘小 viewBox 图形，同一 `StrokeThickness` 下粗细不一；现在整站同一字形
  家族（窗口最小化/最大化两个 OS chrome 字形仍手绘，1px 描边对齐像素栅格）。

## 修复问题

- **Phase 1 的验收标准当时并未成立** —— `plan-2026-10-28-wpf-visual-system` 写「任一视图
  不再有结构性 1px 描边」，实测 `Views/` 还剩 9 处：`AccountFilterBar`（4 个统计卡 +
  筛选条）、`WorkbenchConsole`（任务表外框 + 日志井外框）、`SettingsWindow`（分类栏 +
  内容卡）。现已全部改为 `Surface.Card`/`Block`/`Inset` 明度差，`Views/` 结构性描边归零。
- **同一别名指向两个档位** —— 暗色 `LogBg` 是 `#2B2B2B`（= `Surface.Block`），光色是
  `#EAEAEA`（= `Surface.Inset`）。新测试按「别名必须等于它所在档位」当场抓到，暗色改为
  `#333333`。
- **对话框标题条圆角不足** —— 标题条压在 `Corner.Xl`(12) 卡片上却写死 `10,10,0,0`，
  每边留下 2px 卡片色残边。新增 `Corner.TopXl` 并改用之（外圆角 = 内圆角 + inset，
  此处 inset 为 0）。
- **`ProxyPoolWindow` 拿不到隐式样式** —— 217 行代码建窗改为 `Views/ProxyPoolWindow.xaml`
  + 6 行 code-behind：此前它自行重设 `DataGrid` 的 Background/BorderBrush/RowHeight，
  因为隐式样式从未生效；字号也是字面量。现在继承 `Themes/Controls.xaml`。
- **`max_retries` 是死参数** —— `Socks5Server.__init__` 存了从不读取，实际重试上界是
  「出口数量」；`start_proxy_pool.py` 还硬编码传 `2`。已删除参数并在失败重试循环处写明
  为何上界就该是出口数（十来个 session 共用同一 `host:port` 的池型依赖逐条尝试）。
- **健康探针首轮缺失** —— `_health_check_loop` 原为「先睡后探」，而 `UpstreamProxy.healthy`
  默认 `True`、`start()` 立刻起任务，于是带着死出口的池子会用一整个 `health_check_interval`
  （默认 30s）的「健康」判定服务真实流量。改为先探后睡，并给睡眠期取消单独的处理臂。
- **identity 阶段的隐式契约** —— `prepare_identity` 是 `runtime.context` 的唯一写入方，
  之后每个 handler 收到它；在此之前（以及跳过 identity 的 post-create resume）收到的是可变
  runtime。现有阶段体都闭包 `self`、忽略该参数，所以漂移今天不可见——第一个真正**读**它的
  handler 会拿到与注解不符的对象。新增 `identity_stages.verify_identity_context`
  （由 `_run_stage` 在 `IDENTITY_READY` 之后调用），缺失或没有 `device_id` 时发
  `registration_identity_context_missing` /
  `registration_identity_context_without_device_id` 告警。**刻意是告警而非中止**：
  `IDENTITY_READY` 被幂等空 handler 驱动的路径是合法的（时序测试就这么跑），
  为一个 stub 没建 context 而杀掉真实注册是更坏的交换。

- **支付资格徽章不再恒为灰色** —— 该列原先把展示文案交给 `StatusSeverityConverter`
  做子串匹配，实测只有 `AT失效` / `检测失败` 命中，`可用` / `无可用方式` / `无0元优惠` /
  `币种不符` / `出口被拒` / `未确定` 全部落回中性灰 `#57606A`。现改由
  `payment_qualification_state` 六态机器码决定（`eligible`→success、`ineligible`/
  `egress_denied`→warn、`auth_invalid`/`probe_failed`→danger、`unknown`→info），
  旧行用文案回退；顺带补上 `风控拦截` / `地区不符` 的英文词条（英文界面此前显示中文）。
- **滚动条与分隔条主题化** —— 滚动条轨道/滑块尺寸进入令牌（`Size.ScrollBar.*`），
  滑块 hover 变粗 1px 且加深，拖拽再加深；账户表与控制台之间的 `GridSplitter`
  原先是一条不可见的 6px 条纹（只有光标提示），现改为 hover 才显形的 32×3 药丸手柄，
  拖拽时染 `Primary`。
- **日志区字体** —— 日志几乎全是中文而 `Consolas` 无中文字形，WPF 逐字回退导致一行内
  混用两套字宽；改用 `Font.Mono`（`Cascadia Mono → Consolas → Microsoft YaHei UI`）
  并开启 Display/ClearType。
- **账号表格字号** —— 行数据 13→14（新令牌 `Type.Data`）、行高 40→44、列头 11→13、
  列头高 32→36、行内徽章 11→13，另修两处逃过扫描的 `13.5` 字面量。

- **主题/语言选择现在真的持久化** —— 之前偏好确实写进了
  `runtime/desktop_theme.txt`，但启动时 `MainWindow` 用
  `ApplicationThemeManager.GetAppTheme()` 决定色板，而该 API 文档写明失败时返回
  `ApplicationTheme.Unknown`；Unknown 不等于 Dark，于是暗色安装**每次启动都被刷回浅色板**。
  现在 `App.OnStartup` 读偏好 → `Apply(theme)` → `ApplyStartupTheme(theme)` 覆盖构造函数，
  并在日志里记录 `启动偏好: theme=… language=…`（实测 Dark/EN 均可恢复）。
- **滚动条/分隔条不再是透镜形** —— `CornerRadius=999` 在 8px 宽的滑块上会让 WPF 的四角
  弧线重叠，画成"两头细中间粗"的透镜。滑块改 `Corner.Xs`(4)、3px 细条改新增的
  `Corner.Hairline`(1.5)，并在 `Tokens.xaml` 写明"半径不得超过短边一半"这条约束。
- **状态徽章降饱和** —— AT / RT / 2FA / 优惠状态 / 支付资格 / 状态六列的 `Badge*Bg/Fg`
  统一下调饱和度（底色为同色相淡洗、字色为中等饱和），五态靠色相区分；徽章内文字对比度
  最差 4.84:1，仍满足 ≥4.5。设置弹窗两个 `ScrollBar` 上写死的 `Width/Height="10"` 也已移除，
  三处滚动条现在共用同一组令牌。

- **日志区滑块底部被裁** —— 隐式 `TextBox` 样式为单行输入写死 `Height=32`，而日志框只设了
  `MinHeight="220"`；`MinHeight` 不能取消样式高度、只会抬高它，所以日志框其实是**固定
  220px**。控制台行被拖小时它就溢出，底部连同滑块一起被裁。新增 `MultilineTextBox` 样式
  把 `Height` 复位为 `NaN`（=Auto），高度交给 Grid 行。
- **邮箱列表滑块底部"被遮挡"** —— 两个叠加原因，都是实测出来的：
  ① 框架模板用 `SystemParameters` 决定滚动条宽度，`DataGrid` 内部是 **17px**（WPF 默认），
  而全站样式是 12px，8px 的圆角滑块被迫坐在更宽的轨道里，末端看起来像被切；
  ② 两条滚动条之间的方块取自 `SystemColors.ControlBrushKey`（不透明、不理色板），小窗口
  出现横向滚动条时正好压在纵向滑块末端。现在应用级把
  `SystemParameters.VerticalScrollBarWidthKey` / `HorizontalScrollBarHeightKey` 钉为 12px
  （实测 17 → 12），`AccountGridView` 再把角落键覆盖为 `Transparent`。
- **暗色模式 Tooltip 不可读** —— WPF 的提示框底色来自 `SystemColors.Info*`（米色），色板
  替换碰不到它，而文字颜色从被悬停控件继承 `TextMain`（暗色下 `#E6E6E6`）——浅灰字压米色底。
  新增隐式 `ToolTip` 样式（`Surface.Card` 底 / `TextMain` 字 / `LineStrong` 边 / 圆角 + 轻投影）。
- **侧栏 Logo 模糊** —— 原先是 22px 目标尺寸上缩放 `app-icon.ico`（ICO 帧选择与 22px 无关）
  且用默认双线性采样；现改为 1024×1024 的 `black-kitten.png` +
  `RenderOptions.BitmapScalingMode="HighQuality"` + 40px 底板内取偶数 24px。

## 工程收口

- **令牌纪律延伸到代码构建的那一半 UI** —— 3,499 行（`Dialogs/` + 6 个 `MainWindow.*`
  分部类 + `ProxyPoolWindow`）此前完全绕过设计令牌。本轮清掉 22 处 XAML + 16 处 C#
  `FontSize`、17 处 XAML + 6 处 C# `CornerRadius`、以及最后 1 处硬编码 `#FFFFFF`；
  现在 `Views/` + `Themes/` 的裸字号/圆角字面量与硬编码色值均为 **0**。
- **重写 `test_theme_palette_consistency.py`** —— 它仍在 pin 早在 Phase 0 被删的
  `Palette.xaml`（`FileNotFoundError`），而它原本的用途（C# `SetBrush` 与 XAML 取值比对）
  已随 `SetBrush` 一起消失。改为钉住今天真正的不变量：光源**键集必须相同**、
  `Surface.*` 阶梯方向（光交替 / 暗只变亮）、每个别名等于所在档位、三级文字互异、
  每个 `{DynamicResource}` 目标必须有声明、`FieldLabel` 全应用只声明一次。
- **补齐两条英文词条** —— `精简列：…`（Phase 3 新增）与 `拖动调整控制台高度`（Phase 4 新增）
  之前没有 `StringsEn` 条目，英文界面会静默回落中文。

## 验证

- `pytest tests/workbench`：233 passed，2 skipped；`pytest tests/governance`：774 passed，5 skipped。
- `dotnet build`（Debug / Release）0 warning / 0 error；`dotnet test` **676 passed**。
- `SmsWorkbench/build_dotnet.ps1` 发布成功 → `dist/net10/SmsWorkbench.exe`。
- `architecture_scan` / `function_length` / `import_layer` / `delayed_import` / `config_key` /
  `dotnet_warning` / `format_guard` / `line_ending_guard` / `docs_consistency` /
  `refresh_doc_ratchet_numbers` 全绿。
- `pyright`（本轮改动的 config / desktop_read / proxy / registration 模块）0 errors。

## 资产

- `dist/net10/SmsWorkbench.exe` —— 可运行桌面产物（`SmsWorkbench/build_dotnet.ps1` 输出）。
