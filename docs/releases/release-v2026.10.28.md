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

- `pytest tests/{workbench,proxy,config,registration}`：3457 passed，4 skipped，427 subtests。
- `pytest tests/governance`：774 passed，5 skipped，252 subtests。
- `dotnet build`（Debug / Release）0 warning / 0 error；`dotnet test` **622 passed**。
- `SmsWorkbench/build_dotnet.ps1` 发布成功 → `dist/net10/SmsWorkbench.exe`。
- `architecture_scan` / `function_length` / `import_layer` / `delayed_import` / `config_key` /
  `dotnet_warning` / `format_guard` / `line_ending_guard` / `docs_consistency` /
  `refresh_doc_ratchet_numbers` 全绿。
- `pyright`（本轮改动的 config / desktop_read / proxy / registration 模块）0 errors。

## 资产

- `dist/net10/SmsWorkbench.exe` —— 可运行桌面产物（`SmsWorkbench/build_dotnet.ps1` 输出）。
