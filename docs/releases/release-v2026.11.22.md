# v2026.11.22

自 v2026.11.20 以来的收口：协议注册认证头与区域支付进程内迁移、二级代理链与桌面端新开关、
Python / WPF 架构收口，以及一轮覆盖窗口动效、单窗收件箱、低饱和双主题、字体排布与崩溃修复的桌面端 UI 升级。

## 新增功能

- **协议支付区域方式进程内化与认证头对齐** —— 支付传输内核支持 `oai-web-deployment-attestation`；
  iDEAL、PIX、Kakao Pay、BLIK、TWINT、MoMo 迁入进程内 `oaics_inprocess` 提供器。
- **二级代理链（Two-Hop Proxy）** —— 本地代理桥支持 `hop_proxy` / `GPT_PROXY_CHAIN_HOP` 二级跳板串联。
- **注册效率与离线巡检** —— 支持按目标成功数调度（`--target-success` + `--max-attempts`）、浏览器静态资源省流拦截
  （`--block-assets` / `browser_data_saver`）、离线 JWT 轻量巡检（`--inspect-accounts` 及桌面端复检优惠开关）；
  桌面端设置同步接通 `protocol_http_backend`、`browser_data_saver` 与 `sentinel_sdk_auto_update`。
- **账号详情页与精简列重构** —— 账号详情弹窗移除底部原始文本滚动区与支付状态/金额卡片，
  将「出口 / 出口国家 / 指纹」合并为单张全宽三栏模块，弹窗自适应紧凑高度；邮箱列表勾选「精简列」时继续保留展示「优惠状态」列。
- **收件箱列表与邮件详情合一** —— 单窗口内直接切换收件箱列表与邮件正文详情，支持顶部返回按钮、`Esc` / `Alt+Left` /
  鼠标侧键返回列表、回车打开详情与验证码一键复制，不再叠加二级模态弹窗。
- **标题栏与左侧栏交互重塑** —— 移除标题栏左上角 `GRT` 字样，将版本号与检查更新/下载入口收纳到左侧栏顶部 Logo 点击弹层；
  右上角最小化 / 最大化还原 / 关闭三按钮统一尺寸、光学字形与淡入/按压缩放动效，并随窗口状态同步最大化/还原图标；
  左侧栏模块标题规范为 5 组（快捷操作 / 支付管理 / 邮箱管理 / 账号管理 / 系统设置）并增加横线分隔。
- **DeepAI 风格桌面端重构（字体 / 图标 / 壳层配色 / 模块布局）** ——
  - **排版与清晰渲染**：随包内嵌开源 `Atkinson Hyperlegible Next`（SIL OFL 1.1，`400/500/600`）与 `Microsoft YaHei UI` 组成复合字体栈，全站改用 `Ideal` + `ClearType` + `Fixed` 2D 对称平滑渲染消除无微调字形毛刺，KPI / 表格 / 状态栏全面启用 `Tabular` 等宽数字。
  - **SVG 图标统一**：收敛全仓描边至光学刻度令牌，常态使用 `IconMuted` 退后色，清零 `Segoe MDL2 Assets` 与内联路径，新增 16 个 Tabler v3.50.0 线框图标解除语义复用。
  - **壳层-悬浮画布配色**：外框与通顶侧栏共用 `Surface.Chrome`（光 `#EEEEEE` / 暗 `#202023`），主工作区呈圆角悬浮画布（光 `#F5F5F6` / 暗 `#141416` + `WorkspaceFrameOutline`），日志井独立为深邃 `Surface.Terminal`（光 `#FAFAFA` / 暗 `#111113`），主动作/焦点环采用克制紫罗兰高光（`#6A20D0` / `#7C3AED`），邮箱列表 6 列状态徽章收柔为低饱和矿物灰调（≥ 5.28:1），下拉框/弹层/右键菜单采用同心圆角。
  - **模块布局减重**：通顶侧栏（240px / 折叠 64px，折叠态按钮与图标中心严格对齐）拆分 Sticky Footer 常驻「取消批次 / 设置 / 外壳·主题·语言」；顶栏收敛至 38px 并展示批次运行脉冲胶囊；顶部 4 个 KPI 卡片（总记录 / 有试用 / 已注册 / 异常待处理）压至 48px 且全部支持点击筛选；创建时间/更新时间列增宽至 176px；`WorkbenchConsole` 重构为「实时日志 / 任务队列」单栏分段切换 + 一键折叠抽屉；滚动条收细至 8px 轨道。
- **Python / WPF 架构收拢** —— Python 根模块按领域归入子包并退役旧兼容壳、K12 探测与死导出；
  WPF 按角色命名空间隔离，11 个剩余手搭对话框全部迁为 XAML，拆分 `ShellViewModel`、5 个 Coordinator 与分域样式字典，
  并升级 5 项 GitHub Actions / .NET 测试依赖。

## 修复问题

- **设置弹窗按 `Esc` 报错崩溃** —— 按下 `Esc` / `Tab` 触发键盘焦点框时抛出
  `StaticResourceHolder` 异常（找不到 `Radius.Sm`）。原因是 `FocusRingStyle` 引用了标量圆角键 `Radius.Sm`
  但令牌表只声明了 `Corner.Sm`；现已补全 `Radius.*` 令牌并增加全仓 `StaticResource` 静态解析单测阻断回归。
- **弹窗主按钮文字被覆盖与悬停隐身** —— 全局隐式 `TextBlock` 样式强制覆盖 `Foreground=TextMain` 且 `PrimaryButton`
  复用普通按钮悬停模板，导致深/浅主题下按钮文字与底色撞色或悬停变白字灰底。现为 `Button` / `PrimaryButton` /
  `DangerButton` / `CheckBox` 在 `ContentPresenter` 内绑定自身前景色并独立配置悬停/按压态，标题栏关闭按钮改用独立 `OnDanger` 令牌。
- **关闭收件箱弹窗后主窗口被隐藏/压入后台** —— 收件箱原以非模态 `.Show()` 打开，销毁时 Win32 未将焦点交还主窗口。
  现改为标准模态打开并自动取消在途拉信，同时所有子弹窗关闭前统一主动激活可见父窗口。
- **浅色主题下左侧栏悬停无动效与深色子弹窗闪白** —— 浅色侧栏原先把 `#F3F3F3` 悬停色画在 `#F3F3F3` 底面上完全不可见，
  现为侧栏补齐 `Surface.Chrome` 底面、专属 `SidebarHover` / `SidebarPressed` 令牌与平滑动效；子弹窗改为在
  `SourceInitialized` 首帧绘制前写入 DWM 深色标题栏，消除黑夜模式开窗闪白。
- **认证请求头、2FA 重认证、换绑与 Codex OAuth 对齐** —— 从 auth / nextauth 阶段移除仅限 chatgpt 域的
  `oai-device-id` / `oai-session-id` 头；修正 2FA 重认证主机与 MFA 语言头；邮箱换绑与 Codex OAuth
  统一走 `http_backend` / `tls_fingerprint` 并按次签发新鲜 Sentinel 证明。
- **一键注册弹窗「关闭 2FA」默认值偏差** —— 修正一键注册与选中邮箱注册弹窗中「关闭 2FA」默认勾选为 `True` 的偏差，
  恢复默认不勾选（即默认开启 TOTP 2FA），与模型定义保持一致。
