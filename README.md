<div align="center">
  <img src="./SmsWorkbench/Assets/black-kitten.png" width="140" alt="GPT-Register-Tool logo" />
  <h1>GPT-Register-Tool</h1>
  <p><strong>面向 Windows 的 ChatGPT 账号注册、邮箱 OTP、账号管理与支付工作台</strong><br>
  <em>A Windows desktop workbench for ChatGPT account registration, email OTP, account management, and payment workflows.</em></p>
  <p>
    <a href="./README.md">简体中文</a> · <a href="./README_EN.md">English</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white" alt="Windows 10/11" />
    <img src="https://img.shields.io/badge/.NET-10-512BD4?logo=dotnet&logoColor=white" alt=".NET 10" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  </p>
</div>

## 简介

GPT-Register-Tool 采用 **WPF 桌面端 + Python 业务核心**，提供邮箱 OTP 注册、账号与 Session 管理、代理配置、协议支付链接提取和账号导出能力。运行数据默认保存在本机，不写入 Git。

当前维护约定：[注册架构](docs/current/registration-architecture.md)、
[配置分片优先级](docs/current/configuration.md)、
[日志关联与只读运行目录清单](docs/current/telemetry-and-runtime.md)。
已有分片时只读取分片，不合并旧 `config.json`。排查优先使用
`python -m sms_tool --help` 和 `python scripts/registration_inventory.py`，
后者不会联网、注册账号或清理文件。
当前发布说明：[release-v2026.10.08.md](docs/releases/release-v2026.10.08.md)。

## 赞助商
<img width="5728" height="672" alt="F31720B0BE73735E400C05B8F165FF1C" src="https://github.com/user-attachments/assets/5f3b5b22-5132-4bc4-b8b8-3a0e92b47f37" />

[IPWO](https://www.ipwo.net)全球住宅代理为 ChatGPT 自动化工具提供全球住宅代理资源，支持多地区 IP 选择及灵活的代理配置。<br>
适用于注册代理、独立网络环境及自动化任务等场景，帮助开发者根据项目需求配置合适的网络出口。<br>
包含动态静态IP资源，支持免费测试。[IPWO测试入口](https://www.ipwo.net/?ref=githubGPT)

## 功能亮点

### 一键注册

- 支持邮箱池、ReMail 短效接码、CFWorker 域名邮箱及多供应商手机号接码（含 SMSBower、NexSMS）。
- 支持单账号与并发批量注册。
- 每个注册账号独立提取 Sentinel Token 与 `oai-did`，不跨账号复用认证事务；`_extract_sentinel` 默认允许 2 路并发提取（`sentinel_max_concurrency`，上限 4），兼顾批次速度与 Sentinel 限流风险。
- 注册流程只负责账号认证并保存 AT/Session，不再生成支付链接。
- 注册成功判定以 AT 探测 HTTP 200 为准；稳定探测窗口内未持续返回 200 的候选不会进入 active 账号库。
- 注册流程不再执行 Agent Identity 阶段；需要 Agent Identity 时必须通过显式 SUB2API 导入路径处理。
- 选中邮箱记录时优先注册所选邮箱；未选中邮箱时显示邮箱源选择器。
- 注册、OTP、Session 获取和 AT HTTP 200 探活分别记录阶段结果，避免把中间状态误报为成功；支付提链只由独立支付操作触发。

### 协议一致性与恢复

- CLI 在领取或购买邮箱前依次预检 ChatGPT、Auth 和 Sentinel 三段网络，并从注册代理池选择首条可用线路；TLS、代理或 profile 不满足要求时不会继续消耗邮箱。
- 每个账号绑定独立代理会话；批量注册只在网络或认证状态故障时切换代理并创建新会话，不把同一事务拆到不同出口。
- NextAuth、Auth API、ChatGPT 使用各自的 Header 模板，并共享稳定的 `oai-did`、`oai-session-id`、调用 ID、UA 和 client hints。
- Fingerprint 的语言与时区根据代理 GeoIP 生成；Sentinel QuickJS 使用同一份 UA、平台、时区、屏幕、内存和 client hints。
- Sentinel 分别生成 `username_password_create`、`authorize_continue`、`oauth_create_account` token。Token、Cookie 和 Header 中的 DID 不一致时按 `sentinel_extract_failed` 终止。
- 单会话收到 403/429 后打开熔断，冷却期间不继续请求；生产注册不允许纯 HTTP PoW 降级。
- 创建账号并获得 AT 后立即持久化候选和断点，再执行 AT HTTP 200 探活。代理/TLS 探活失败可以从断点恢复，不重复邮箱 OTP 和账号创建。

### ReMail 邮箱源

- 一键注册来源中提供 `ReMail 邮箱`，默认统一使用 `purchase` 长效邮箱模式。
- 支持单笔或批量创建邮箱订单。
- 百账号批量下单默认按每个邮箱 2 秒扩展 HTTP 等待时间（至少 30 秒），可通过 `email_registration.remail.batch_timeout` 覆盖。
- 支持 `private_first`、`public_only` 库存策略。
- 支持指定项目、产品和邮箱后缀。
- 使用 `Idempotency-Key` 防止重试导致重复订单。
- 订单创建使用 API Key；收件使用独立的邮箱地址与 Service Token。
- Service Token 返回 401 时会用 API Key 查询所属订单；如服务端返回新 Token，会保存到 Session JSON 和 SQLite 后重试一次。
- `code` 订单只能在 `receiveUntil` 前收件，API Key 不能代替过期的 Service Token；需要后续持续查看收件箱时请选择 `purchase`。
- 邮件摘要无验证码时自动读取邮件详情，并执行时间、收件人、消息 ID 和已排除验证码过滤。
- ReMail 返回可信 OpenAI 发件人的结构化六位验证码时，会校验精确收件人和时间戳；即使本地化主题发生乱码，也不会误等到超时。
- 桌面端可从 ReMail 注册记录打开收件箱；查看模式会读取邮件完整正文和验证码。
- 日志会脱敏 API Key 和 Service Token。
- 自适应 OTP 轮询：初始延迟 1s，渐进退避（1s → 1.5s → 3s），根据邮件到达状态和服务器限流建议动态调整轮询间隔，减少无效请求。
- ReMail 收件时间允许默认 90s 的服务端时钟偏差；消息 ID 快照仍会阻止旧验证码被重复使用。
- ReMail 在 30s 内仍未收到验证码时会重发一次，剩余时间继续接受本次事务中的最新验证码。
- 已有 ReMail 订单可按 `remail://email---serviceToken---orderNo---purchaseId` 写入邮箱 Token 文件恢复使用，无需重复购买。
- 批量购买遇到超时或可重试 5xx 时，会先按请求时间窗、项目、邮箱后缀和数量严格匹配新订单；仅在恰好匹配时自动恢复，避免响应丢失后重复购买。
- `ReMail 邮箱` 默认使用长效 `purchase` 模式，并按注册数量补足稳定 HTTP 200 AT。CLI 仍可通过 `--max-mailbox-purchases` 和 `--max-remail-cost` 设置额外限制。

### 统一邮箱与 OTP

统一 mailbox seam 支持：

- ReMail。
- Smailr（`smailr.com`、`loc.cc`、`mail.nodeloc.cc`、`nodeloc.cc`），支持受等级限制时复用已有未绑定邮箱、详情正文补取和 10 秒服务端时钟偏差。
- CFWorker 域名邮箱。
- Microsoft Graph/OAuth。
- Outlook/Hotmail IMAP 回退。
- Gmail IMAP 与 SMTP。
- iCloud 接码链接，桌面端“导入邮箱”和后端均支持 `email----接码URL` 和 `email---接码URL`。
- Chatai、token 文件及历史邮箱池格式。

OTP 解析支持主题匹配、发件人过滤、收件人精确匹配、服务端时间戳过滤和候选排序。

### 协议支付提链

- 支持 PayPal、GoPay、GCash、GrabPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、直卡 Checkout、MoMo、QRIS、Bizum、Naver Pay（共 15 种；QRIS/Bizum/Naver Pay 为 canary，未开放注册入口，仅 CLI 可选）。
- BLIK 会提交一次性六位码并直接执行支付，只在单账号协议支付弹窗/命令中提供，不进入注册后自动提链或批量支付选择器。
- 直卡 Checkout（菲律宾 PH/PHP）：走 US 下单 → TR 刷优惠 → 校验 0 元，产出 `chatgpt.com/checkout/<entity>/<cs_id>` 直卡结账长链。
- MoMo（越南 VN/VND）：下单 → Stripe init → 强制 ₫0 → 建 MoMo PM → Confirm → Approve → 跟跳转，产出可扫的 `payment.momo.vn` 二维码（自动解码为 PNG，供“打开二维码”使用）。
- GoPay（印尼 ID/IDR）和 GrabPay（菲律宾 PH/PHP）复用钱包适配器；GoPay 在 Checkout 后通过独立 Promotion/Update 阶段校验 0 元，再执行 Stripe init → 建钱包 PM → Confirm → Approve → Poll → Provider Redirect。GCash 使用独立的 custom-payment-method adapter 和 transport，不走共享钱包 Provider。
- PayPal 支持 Hosted 长链接、PP 直链和强制 0 元试用模式。PP 直链使用标准 `Checkout -> confirm -> approve -> 同 Checkout 应用优惠` 顺序；approval 明确返回 `blocked` 时会重建整个 Checkout，不在原提交上重复 approve。
- PayPal 正式提链前执行支付方式能力与零元资格探测；探测报告和正式提链报告分开保存，非零报价归入资格/报价失败。
- PayPal BA 提取成功后可进入持久化后续授权队列；该队列只属于 PayPal，不在其他支付方式界面显示。
- PayPal 回跳对账由独立 `paypal_reconciliation.py` 处理，只跟踪白名单内的 Stripe Return → OpenAI Pay → Checkout Verify，并输出脱敏的 `conclusive`/`unknown`/`failed` 证据；它不改变提链接口，也不生成或覆盖支付链接。
- 批量协议支付使用两个相互独立的支付出口池：Checkout 池默认跟随账单区，Approve / Update 与 Checkout 共用完整账单地区目录，不再限制为 JP/TR；Promotion、Provider、Confirm 和 Redirect 继续使用各适配器的内部阶段国家契约。
- 动态代理会按支付方法自动改写国家与 Session，支持 `payment_methods.json` 中 `checkout_countries` 列出的 13 个目标出口：US、ID、JP、TR、TH、VN、PH、IN、GB、DE、ES、KR、BR（以配置文件为准，不要照抄旧文档的 NL/PL/CH——那三者不在结账国列表里，配不出结果）。
- 协议支付代理池按顺序探测，当前代理不可用或出口国家不匹配时自动切换下一条。
- 地区和代理选择保存为历史记录。
- 支持实际测试代理出口 IP、国家及预期地区是否匹配。
- 严格区分 Checkout、Promotion/Update、PM 创建、Confirm、首次 Poll、最终 Provider Redirect 等阶段。
- 通用提链终态为 `completed`、`failed`、`cancelled`、`unknown`、`timed_out`，每条结果都有 `retryable` 和 `error_stage`。`unknown` 会额外标记 `requires_reconciliation=true`，在完成对账前禁止自动重试；`cancelled` 不重试，普通 `timed_out` 可按策略重试。
- 批量提链应先用本地额度接口筛出非 401 账号，再执行支付协议；报告必须分别统计 AT 可用、套餐/试用资格、支付方式可见、Approve 成功和最终链接/二维码产物。
- MoMo 只有在返回 `ready_with_qr` 且产出 `payment.momo.vn` URL 或二维码文件时才算成功；`account_trial_ineligible`、`card_only_full_price` 和 `approve_result_blocked` 都是明确失败状态。

- 批量支付执行器支持 JIT AT、HTTP 401 分层恢复（RT、Cookie、隔离浏览器邮箱 OTP、Codex OAuth）、资格探测、Canary 暂停、方法级并发、默认 3 次重试、原子断点和显式续跑。默认点击会创建新批次，只有勾选“恢复已有断点”才复用旧批次 ID。
- MoMo 按 Checkout、Promotion、Stripe Provider、Approve、Redirect 分阶段使用代理；Kakao 输出结构化结果，只有明确的 Kakao/Nicepay Redirect 才算链接成功。
- `oaics_*` 是原生 ChatGPT Checkout，会直接返回 Checkout 链接且不请求 Stripe；`cs_*` 继续进入 Stripe/PayPal 协议链。
- iDEAL、BLIK、TWINT 通过公共 `ProtocolResultReporter` 输出一次且仅一次的脱敏 `protocol_payment.v1` 终态，并统一处理已支付和缺失输出兜底。

### Agent Identity 与 SUB2API 导入边界

- 注册主流程已移除 Agent Identity/task 阶段，不会因为 Agent Identity 失败改变 AT 200 注册结果。
- 已存在的 Agent Identity JSON 仍可由显式 SUB2API 导入路径读取；新建/重建也只能通过该导入流程触发。
- Agent Identity 使用 Ed25519 PKCS#8 私钥，独立保存到 `sessions/agent_identities/`，私钥不写入日志。
- 支持通过 `--register-and-import` 在注册完成后自动导入 SUB2API。
- SUB2API 导入支持 `auto`、`oauth`、`agent_identity` 三种凭据模式；它们只影响导入边界，不会重新插入注册阶段。
- SUB2API 导出格式兼容 Go 后端，`expires_at` 字段使用 Unix 时间戳（int64）。
- 可通过 `--sub2api-no-verify` 跳过导入后的连通性验证。

### 账号与数据管理

- Session JSON 与 SQLite 双层索引。
- 账号状态、AT（已获取/未获取/401失效）、RT、支付链接和手机号验证结果集中展示。
- 左侧栏“账号测活”负责 AT/额度健康检查；HTTP 401 会在显式恢复或支付 JIT 流程中依次尝试 RT、Cookie、隔离浏览器邮箱 OTP 和 Codex OAuth。
- 支持复制 AT、查看邮箱和重新注册；协议支付链接统一从协议提链入口生成。
- 支持 Codex JSON、CPA、SUB2API 等导入导出流程。
- 账号列表展示优惠状态；“可试用 Plus”使用绿色成功状态，并支持在筛选后的完整账号集合上排序后再分页。查优惠默认只查套餐和试用，不创建 Checkout。独立“查支付资格”入口（CLI `--check-payment-eligibility --email <邮箱>`，批量用 `--email-file <文件>`）或在 CLI 查优惠时显式加 `--payment-eligibility` 并指定账号，才会创建一次性 Checkout 并读取明确支付方式证据（`cs_*` 的 Stripe init/可选 Elements，`oaics_*` 的自定义 Checkout 字段）。出口须匹配账单国家，未验证或失败时显示“支付资格未知”；不会默认猜 `card`，也不会创建支付方式、确认或扣款，但可能触发风控或限流。探测结果以 ` · ` 拼接在优惠状态之后。
- 本地数据默认保存在 `sessions/` 和 `runtime/`，两者均被 Git 忽略。

### 桌面端批量支付操作

1. 在账号列表勾选要处理的账号，打开左侧“批量协议支付”或右键同名菜单。
2. 选择支付方式，设置并发、瞬态重试、Canary 数量，并分别填写 Checkout / Approve 代理池（每行一条）。默认是“新执行”并自动生成批次 ID；需要续跑时显式勾选“恢复已有断点”并填写旧 ID。可在窗口底部保存当前支付方式的池和出口国家配置。
3. 默认开启“401 自动恢复”；勾选“仅探测资格”后会完成 JIT AT、注册地区矩阵、ChatGPT Checkout 和 Stripe init，然后在创建 PM、Confirm、Approve 和 Provider Redirect 前停止。结果会明确记录金额、币种、支付方式可见性和 `eligible`/`ineligible`/`unknown` 分类。
4. 通过“账号地区 / 支付资格矩阵”确认注册区、账单区（Checkout）和优惠区（Approve）；Promotion、Provider、Redirect 等内部阶段仍会按适配器配置执行。
5. 只有显式选择断点恢复时，相同模式、矩阵、代理与重试参数才会读取 `runtime/payment_batches/` 的原子断点；运行参数变化时签名失配会重新执行，探测结果不会被正式支付复用。系统性的 `unknown` Canary 会暂停该方法后续完整批次，明确的支付方式不可用或非零报价不会误判为协议故障。报告会分开显示 AT 200、JIT 刷新、能力探测、资格、链接、二维码、失败计数、阶段耗时和最后失败阶段。

注册批次每次输出 `Saved session:` 后，桌面端会防抖异步刷新账号池，无需等整批结束。账号列表的多选删除会合并为一个后端批量命令并在后端并发处理，不再逐账号串行启动 Python。

### 手机接码

- 支持 SMSBower、HeroSMS、Grizzly、NexSMS 的供应商选择及在线目录查询（可用性依供应商协议而定）。
- 支持发送重试、等待超时和轮询间隔配置。
- 支持 Codex OAuth 手机验证和账号刷新流程。
- 批量操作保持邮箱与手机号结果映射，便于排查单账号失败。
- 一键接码的配置、失败结果与持久化说明见 [当前接码契约](docs/current/one-click-sms.md)。

### 架构总览（精简）

```text
SmsWorkbench/            WPF 桌面端：Generic Host/DI → MVVM 页面、设置页、任务入口、状态展示
        │  IBackendClient（ArgumentList + 取消/超时/进程树终止，@@SMSWORKBENCH_V2@@ 版本化结果信封）
        ▼
sms_tool/cli.py          CLI 与任务编排：参数解析、批量任务、进程退出状态
        │
        ├─ registration_handlers.py  协议注册阶段顺序（auth_flow → user/register → OTP → create_account → session → AT 探测）
        │     ├─ auth_flow/          步骤函数：signin / authorize / continue / OTP / TOTP
        │     ├─ sentinel/           Sentinel Token 签发（Node sdk.js runner）
        │     └─ accounts/           账号创建、存活探测与恢复
        ├─ mailbox.py                邮箱统一路由：ReMail / CFWorker / Graph / Gmail / iCloud 链接
        ├─ payment_*                 协议支付：Checkout 契约、能力探测、钱包/GCash、批量与 JIT AT
        └─ storage.py                SQLite + Session JSON 持久化
services/                可选本地协议服务：邮件诊断、协议支付提取器（子进程边界）
```

更详细的边界说明见 [架构说明](docs/architecture.md)，目录职责见 [目录职责](docs/directory-map.md)。

## 部署方式

### 环境要求

- Windows 10/11 x64。
- Python 3.10 或更高版本。
- `curl_cffi==0.16.0`。注册预检会校验安装版本和 `chrome146` profile；旧版本不会进入邮箱采购或注册阶段。
- .NET 10 Desktop Runtime；从源码编译时需要 .NET 10 SDK。
- **Node.js 18+**（`node` 需在 PATH）：Sentinel Token 的 quickjs 提取器用 `node` 运行 OpenAI 真实 `sdk.js`，缺失会导致注册阶段 OTP 静默丢失。
- **Playwright Chromium**：MoMo/直卡等协议支付的 Stripe init 走 Chromium 网络栈完成 TLS，需执行 `python -m playwright install chromium`。
- 可正常访问目标邮箱、ChatGPT 和支付服务的网络环境。
- 注册代理、邮箱收件代理和协议支付代理彼此独立；邮箱收件默认使用本地 `http://127.0.0.1:7897`。

安装依赖后，可运行环境预检确认 Node.js、Playwright Chromium 和关键 Python 包就绪：

```powershell
python scripts/preflight_env.py
```

### 方式一：安装包

从 GitHub Releases 下载最新的：

```text
GPT-Register-Tool-Setup-vYYYY.MM.DD.exe
```

运行安装器并选择安装目录。首次启动前仍需安装 Python 依赖，并创建本地配置：

```powershell
python -m pip install -r requirements.txt -c constraints.txt
copy config.example.json config.json
```

### 方式二：便携压缩包

下载并解压：

```text
GPT-Register-Tool-win-x64-vYYYY.MM.DD.zip
```

在解压目录执行：

```powershell
python -m pip install -r requirements.txt -c constraints.txt
copy config.example.json config.json
.\dist\net10\SmsWorkbench.exe
```

### 方式三：从源码运行

```powershell
git clone https://github.com/2951461586/GPT-Register-Tool.git
cd GPT-Register-Tool
python -m pip install -r requirements.txt -c constraints.txt
copy config.example.json config.json
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
.\dist\net10\SmsWorkbench.exe
```

桌面程序只能通过 `SmsWorkbench/build_dotnet.ps1` 编译。不要直接运行 `dotnet build`，因为它只产生中间文件，不会更新标准工作区 `dist/net10`。

### 首次配置

打开桌面端的 **设置** 页面，至少完成以下配置：

1. 在 **网络与支付** 中配置注册代理池和邮箱收件代理；协议支付的 Checkout / Approve 两个代理池在“批量协议支付”窗口中按支付方式保存。
2. 在 **邮箱与收信** 中配置 ReMail、CFWorker 或其他邮箱源。
3. 按需配置 SMSBower、CPA、SUB2API 和各协议支付参数。
4. 保存后重新打开对应功能即可使用新配置。

注册驱动在 **设置 -> 注册与接码 -> 注册驱动** 中选择，默认仍为 `protocol`。选择浏览器驱动后，注册会复用同一套邮箱 OTP、Session 提取、AT HTTP 200 探活和本地持久化边界：

- `playwright`：本机 Chromium，通过 Playwright 启动。
- `roxy`：连接本机 RoxyBrowser API 创建/打开 Profile，再通过 CDP 接管。
- `cloak`：调用已安装的 CloakBrowser Python SDK。
- `camoufox`：调用已安装的 Camoufox 反检测浏览器（**当前默认浏览器驱动**）。

RoxyBrowser 的 API/会话配置位于同一设置页的独立分区；CloakBrowser 的 License Key、持久化目录和指纹参数也在那里配置。未配置所选驱动的必需字段时，任务会返回脱敏的配置错误，不会回退到协议注册。浏览器驱动不绕过 CAPTCHA；遇到人工挑战会以 `manual_challenge_required` 结束，保留现有账号状态。

浏览器注册默认启用 **脉冲调度**（`registration.pulse`，波次间隔 + OTP-ban 暂停）与 **浏览器进程池**（`registration.browser_process_pool`，按进程复用浏览器上下文、按健康度回收）。二者与「账号 ↔ 代理槽绑定」协同：每个账号在整个生命周期内固定走同一个注册出口，重试时只刷新会话 sid，不切换代理成员——避免出口轮换被注册方判定为代理抖动而封禁。进程池哈希键按 `(driver, headless, timeout)` 缓存，使同一出口、同一头部配置的账号复用同一浏览器进程，降低冷启动开销。

ReMail API Key 也可以通过环境变量提供：

```powershell
$env:REMAIL_API_KEY = "rk-your-key"
```

环境变量优先于 `config.json`。桌面设置页保存的 API Key 仅写入本地且被 Git 忽略的 `config.json`。

## 许可证与使用责任

本项目以 **MIT License** 发布，全文见 [LICENSE](LICENSE)：

```text
MIT License

Copyright (c) 2026 GPT-Register-Tool contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

请仅在获得授权并符合相关服务条款、地区法规及组织政策的场景中使用本项目。使用者需要自行承担第三方邮箱、代理、接码与支付服务的费用、账号安全和数据合规责任；示例配置不包含任何真实凭据。
