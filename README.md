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

## 中文简介

GPT-Register-Tool 采用 **WPF 桌面端 + Python 业务核心**，提供邮箱 OTP 注册、账号与 Session 管理、代理配置、协议支付链接提取和账号导出能力。运行数据默认保存在本机，不写入 Git。

## 赞助商
<img width="5728" height="672" alt="F31720B0BE73735E400C05B8F165FF1C" src="https://github.com/user-attachments/assets/5f3b5b22-5132-4bc4-b8b8-3a0e92b47f37" />

[IPWO](https://www.ipwo.net)全球住宅代理为 ChatGPT 自动化工具提供全球住宅代理资源，支持多地区 IP 选择及灵活的代理配置。<br>
适用于注册代理、独立网络环境及自动化任务等场景，帮助开发者根据项目需求配置合适的网络出口。<br>
包含动态静态IP资源，支持免费测试。[IPWO测试入口](https://www.ipwo.net/?ref=githubGPT)

## 项目说明

### 主流程

```text
邮箱源
  -> ChatGPT 邮箱 OTP 注册
  -> 获取 Access Token / Session，并以稳定 HTTP 200 AT 作为入库边界
  -> 可选手机验证与优惠资格查询；协议注册保持 AT-only
  -> JIT AT 探测/刷新与可选协议支付链接提取
  -> Session JSON + SQLite 索引
  -> WPF 桌面端统一管理
```

### 适用场景

- 从邮箱池、ReMail 或 CFWorker 执行批量邮箱注册。
- 统一轮询 Microsoft、Gmail、iCloud 接码链接、ReMail、CFWorker 等邮箱的 OTP。
- 管理本地账号、Session、额度状态和支付链接。
- 按阶段选择代理出口并提取 PayPal 或其他本地支付方式链接。
- 将账号数据导出为 Codex、CPA、SUB2API 等目标格式。

### 技术栈

| 层级 | 技术 |
| --- | --- |
| 桌面端 | WPF、.NET 10、C#、Generic Host、CommunityToolkit.Mvvm、WPF-UI |
| 业务核心 | Python 3、curl_cffi、requests、httpx、PyNaCl（Ed25519） |
| 数据存储 | JSON、JSONL、SQLite |
| 邮箱协议 | ReMail API、CFWorker、iCloud 接码链接、Microsoft Graph/OAuth、IMAP、Gmail IMAP |
| 支付协议 | Stripe Checkout、PayPal、GoPay、GCash、GrabPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、直卡 Checkout、MoMo |
| 浏览器辅助 | Playwright、Camoufox、CloakBrowser、RoxyBrowser、AdsPower |

## 安装部署方式

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
python -m pip install -r requirements.txt
copy config.example.json config.json
```

### 方式二：便携压缩包

下载并解压：

```text
GPT-Register-Tool-win-x64-vYYYY.MM.DD.zip
```

在解压目录执行：

```powershell
python -m pip install -r requirements.txt
copy config.example.json config.json
.\dist\net10\SmsWorkbench.exe
```

### 方式三：从源码运行

```powershell
git clone https://github.com/2951461586/GPT-Register-Tool.git
cd GPT-Register-Tool
python -m pip install -r requirements.txt
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
- `adspower`：连接本机 AdsPower API 创建/打开环境，再通过 CDP 接管。

RoxyBrowser 与 AdsPower 的 API/会话配置位于同一设置页的独立分区；CloakBrowser 的 License Key、持久化目录和指纹参数也在那里配置。未配置所选驱动的必需字段时，任务会返回脱敏的配置错误，不会回退到协议注册。浏览器驱动不绕过 CAPTCHA；遇到人工挑战会以 `manual_challenge_required` 结束，保留现有账号状态。

浏览器注册默认启用 **脉冲调度**（`registration.pulse`，波次间隔 + OTP-ban 暂停）与 **浏览器进程池**（`registration.browser_process_pool`，按进程复用浏览器上下文、按健康度回收）。二者与「账号 ↔ 代理槽绑定」协同：每个账号在整个生命周期内固定走同一个注册出口，重试时只刷新会话 sid，不切换代理成员——避免出口轮换被注册方判定为代理抖动而封禁。进程池哈希键按 `(driver, headless, timeout)` 缓存，使同一出口、同一头部配置的账号复用同一浏览器进程，降低冷启动开销。

ReMail API Key 也可以通过环境变量提供：

```powershell
$env:REMAIL_API_KEY = "rk-your-key"
```

环境变量优先于 `config.json`。桌面设置页保存的 API Key 仅写入本地且被 Git 忽略的 `config.json`。

## 项目功能亮点

### 一键注册

- 支持邮箱池、ReMail 短效接码、CFWorker 域名邮箱和 SMSBower 手机号注册。
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
- 账号列表展示优惠状态；“可试用 Plus”使用绿色成功状态，并支持在筛选后的完整账号集合上排序后再分页。
- 本地数据默认保存在 `sessions/` 和 `runtime/`，两者均被 Git 忽略。

### 桌面端批量支付操作

1. 在账号列表勾选要处理的账号，打开左侧“批量协议支付”或右键同名菜单。
2. 选择支付方式，设置并发、瞬态重试、Canary 数量，并分别填写 Checkout / Approve 代理池（每行一条）。默认是“新执行”并自动生成批次 ID；需要续跑时显式勾选“恢复已有断点”并填写旧 ID。可在窗口底部保存当前支付方式的池和出口国家配置。
3. 默认开启“401 自动恢复”；勾选“仅探测资格”后会完成 JIT AT、注册地区矩阵、ChatGPT Checkout 和 Stripe init，然后在创建 PM、Confirm、Approve 和 Provider Redirect 前停止。结果会明确记录金额、币种、支付方式可见性和 `eligible`/`ineligible`/`unknown` 分类。
4. 通过“账号地区 / 支付资格矩阵”确认注册区、账单区（Checkout）和优惠区（Approve）；Promotion、Provider、Redirect 等内部阶段仍会按适配器配置执行。
5. 只有显式选择断点恢复时，相同模式、矩阵、代理与重试参数才会读取 `runtime/payment_batches/` 的原子断点；运行参数变化时签名失配会重新执行，探测结果不会被正式支付复用。系统性的 `unknown` Canary 会暂停该方法后续完整批次，明确的支付方式不可用或非零报价不会误判为协议故障。报告会分开显示 AT 200、JIT 刷新、能力探测、资格、链接、二维码、失败计数、阶段耗时和最后失败阶段。

注册批次每次输出 `Saved session:` 后，桌面端会防抖异步刷新账号池，无需等整批结束。账号列表的多选删除会合并为一个后端批量命令并在后端并发处理，不再逐账号串行启动 Python。

### 手机接码

- 支持 SMSBower 国家与价格档位查询。
- 支持发送重试、等待超时和轮询间隔配置。
- 支持 Codex OAuth 手机验证和账号刷新流程。
- 批量操作保持邮箱与手机号结果映射，便于排查单账号失败。

## 项目架构

### 分层结构

```text
SmsWorkbench/
  WPF 桌面端
  -> Generic Host / DI 组合根
  -> 渐进式 MVVM 页面、配置、列表、任务启动、状态展示

IBackendClient
  -> ArgumentList + 取消/超时/进程树终止
  -> @@SMSWORKBENCH_IPC_V1@@ 单行版本化结果信封

sms_tool/cli.py
  CLI 与任务编排
  -> 参数解析、批量任务、进程退出状态

sms_tool/registration.py
  注册主流程
  -> 邮箱 OTP、账号创建、AT-only Session、AT HTTP 200 验证

sms_tool/registration_concurrency.py
  注册阶段资源门控
  -> 网络、AT 探测和支付阶段并发上限与等待指标

sms_tool/account_liveness.py / account_recovery.py
  账号存活与恢复
  -> 无副作用额度探测、显式 OAuth 恢复和状态持久化

sms_tool/payment_auth.py / payment_batch.py
  JIT AT 门禁与批量协议支付
  -> 401 分层恢复、Checkout/Stripe 能力探测、资格矩阵、Canary、重试、断点报告

sms_tool/checkout_contract.py / payment_capability.py
  统一 Checkout 契约与支付方式能力探测
  -> 地区/币种/locale、Stripe init、金额与支付方式目录归一化

sms_tool/wallet_provider.py / wallet_transport.py
  GoPay、GrabPay 共用钱包适配器
  -> GoPay Promotion/Update、PM、Confirm、Approve、Poll、Provider Redirect 与分阶段代理

sms_tool/gcash_provider.py / gcash_transport.py
  GCash 独立 custom-payment-method 适配器
  -> Checkout 更新、Custom PM 创建、确认和 Provider Redirect

sms_tool/mailbox.py
  邮箱统一路由
  -> ReMail / CFWorker / Graph / IMAP / Gmail

sms_tool/payment_link_manager.py
  协议支付管理器
  -> 方法注册、分段代理、五种终态、retryable/error_stage 统一结果

sms_tool/payment_flow.py / payment_routing.py / payment_executor.py
  公共协议支付执行层
  -> 阶段词汇、方法流程、代理计划、状态机与统一终态

sms_tool/paypal_reconciliation.py
  独立 PayPal 回跳对账
  -> 白名单跳转状态机、秘密脱敏、结论/未知分类

sms_tool/storage.py
  数据持久化
  -> Session JSON、SQLite、状态与去重

services/
  可选本地协议服务
  -> 邮件诊断、其他支付提取器
```

### 核心模块

| 模块 | 职责 |
| --- | --- |
| `SmsWorkbench/` | WPF 桌面界面、设置页、任务入口和本地状态展示 |
| `SmsWorkbench/AccountGridPresentation.cs` | 账号列表优惠状态颜色、全量排序和分页前排序规则 |
| `sms_tool/cli.py` | CLI 参数与高层任务编排 |
| `sms_tool/registration.py` | ChatGPT 注册、OTP、Session 和后续验证 |
| `sms_tool/registration_concurrency.py` | 注册阶段资源组、并发门控与等待指标 |
| `sms_tool/account_liveness.py` | `/backend-api/wham/usage` 存活探测、响应分类与额度解析 |
| `sms_tool/account_recovery.py` | 本地额度刷新、401 分层恢复、候选 AT 验证与停用账号持久化 |
| `sms_tool/mailbox.py` | 邮箱 provider 路由与统一 OTP 轮询 |
| `sms_tool/mailbox_remail.py` | ReMail 下单、收件、详情读取和 OTP 提取 |
| `sms_tool/mailbox_cfworker.py` | CFWorker 邮箱创建与收件 |
| `sms_tool/mailbox_graph.py` | Microsoft OAuth 与 Graph 边界 |
| `sms_tool/mailbox_gmail.py` | Gmail IMAP/SMTP 与 OAuth |
| `sms_tool/mailbox_icloud_url.py` | iCloud 接码链接收件、HTML/API 正文解析与 OTP 归一化 |
| `sms_tool/payment_link_manager.py` | 支付方法注册、状态机与统一结果 |
| `sms_tool/payment_flow.py` | 支付阶段词汇与各支付方式流程 profile |
| `sms_tool/payment_routing.py` | 支付方式独立代理池、阶段路由和脱敏执行计划 |
| `sms_tool/payment_executor.py` | 公共执行状态机、取消/未知终态和结果归一化 |
| `sms_tool/checkout_contract.py` | ChatGPT Checkout、Stripe init 请求/响应与支付方式能力证据契约 |
| `sms_tool/payment_capability.py` | 只到 Checkout + Stripe init 的通用能力探测 |
| `sms_tool/wallet_provider.py` | GoPay、GrabPay 共用编排与结构化结果 |
| `sms_tool/wallet_transport.py` | GoPay、GrabPay HTTP、分阶段代理和 Provider Redirect 校验 |
| `sms_tool/gcash_provider.py` / `gcash_transport.py` | GCash custom-payment-method 编排与 HTTP transport |
| `sms_tool/gen_pp_link.py` | PayPal/Stripe Checkout 与链接生成 |
| `sms_tool/paypal_proxy.py` | 分段代理、地区轮换和出口探测 |
| `sms_tool/paypal_reconciliation.py` | 与提链独立的 PayPal 商户回跳对账和脱敏证据 |
| `sms_tool/storage.py` | SQLite、Session 索引和状态持久化 |
| `sms_tool/agent_identity.py` | 显式 SUB2API Agent Identity 凭据转换、Ed25519 密钥生成与持久化 |
| `sms_tool/sub2api_import.py` | SUB2API 导入（多认证模式） |
| `sms_tool/session_converter.py` | 多格式账号与 Session 转换 |
| `sms_tool/payment_auth.py` | 支付前 AT 探测、401 分层恢复与安全遥测 |
| `sms_tool/payment_batch.py` | 批量协议支付、资格矩阵、Canary、重试与原子断点 |
| `sms_tool/registration_progress.py` | 注册阶段进度跟踪与持久化 |
| `sms_tool/error_classification.py` | 错误类型分类与重试/报告规范化 |

更详细的边界说明参见 [docs/architecture.md](docs/architecture.md)，目录职责参见 [docs/directory-map.md](docs/directory-map.md)。

## 核心配置

### 配置分片（config sharding）

`config.json` 在 08-30 的改造中拆成了 **3 个分片文件**，全部被 Git 忽略、只存本地：

| 分片文件 | 归属的顶层键（共 22 个） |
|---|---|
| `proxy.json` | `proxy`、`mailbox_proxy`、`phone_reuse`、`paypal_browser` |
| `runtime.json` | `runtime`、`timeouts`、`storage`、`output`、`account_health`、`registration`、`chatgpt`、`email_registration`、`codex_oauth` |
| `payment.json` | `paypal`、`paypal_nocard`、`upi`、`omakse`、`protocol_payments`、`kakao`、`momo`、`cpa_mode`、`sub2api` |

写入时按 `SHARD_OWNERSHIP`（`sms_tool/config.py`）路由到正确的分片；未列出的键默认进 `runtime.json`。
**配置模板是 `config.example.json`**（入库、安全），三个分片文件**不要**提交到仓库（含代理口令 / API Key / 支付凭据）。
桌面「设置」页保存、CLI 启动都会先合并这三个分片再使用，分片缺失会自动从 `config.example.json` 派生默认值。

### ReMail

```json
{
  "email_registration": {
    "remail": {
      "enabled": true,
      "base_url": "https://remail.aishop6.com",
      "api_key": "",
      "project_id": 2,
      "service_mode": "purchase",
      "supply": "private_first",
      "email_suffix": "outlook.com",
      "otp_poll_interval": 1,
      "batch_timeout": 200
    },
    "sentinel_max_concurrency": 2,
    "remail_otp_issued_after_grace_seconds": 90,
    "remail_otp_resend_after_seconds": 30
  }
}
```

### 注册与收件代理

```json
{
  "mailbox_proxy": "http://127.0.0.1:7897",
  "proxy": {
    "registration": "http://user:pass-JP-session-5m@gateway:port",
    "default": "http://user:pass-JP-session-5m@gateway:port",
    "pool": ["http://user:pass-JP-session-5m@gateway:port"]
  }
}
```

注册流量走 JP 动态代理（`proxy.registration` / `proxy.pool`），worker 会刷新动态 Session 使各并发出口 IP 不同；邮箱 OTP 收取固定走 `mailbox_proxy`（默认 `http://127.0.0.1:7897`），不会继承注册代理；支付流量走各支付方式自己的 Checkout / Approve 池。三者互不覆盖，支付池在桌面端“批量协议支付”窗口显示和保存。

### 协议支付代理池

```json
{
  "protocol_payments": {
    "methods": {
      "gopay": {
        "checkout_proxy_pool": [
          "http://user-region-ID-sid-session-t-5:pass@gateway-a:port",
          "http://user-region-ID-sid-session-t-10:pass@gateway-b:port"
        ],
        "approve_proxy_pool": [
          "http://user-region-JP-sid-session-t-5:pass@gateway-c:port",
          "http://user-region-TR-sid-session-t-10:pass@gateway-d:port"
        ],
        "stage_proxy_countries": { "checkout": "ID", "approve": "JP" }
      }
    }
  }
}
```

`checkout_proxy_pool` 与 `approve_proxy_pool` 按支付方式独立保存，值为代理数组；批量 CLI 对应 `--checkout-proxy-pool` 和 `--approve-proxy-pool`（换行分隔）。旧的 `protocol_payments.proxy_pool` 仍可被读取作兼容回退，但不再出现在设置弹窗，也不会覆盖已配置的方法池。提链时会按阶段国家改写 `region-XX` 或密码中的国家和动态 Session。

### JIT AT 与批量支付

```json
{
  "registration": {
    "at_stability_probe_count": 2,
    "at_stability_probe_delay_seconds": 10,
    "at_probe_timeout_seconds": 30,
    "stage_concurrency": { "network": 4, "at_probe": 4 }
  },
  "protocol_payments": {
    "batch": {
      "method_workers": { "momo": 2, "kakao": 2 },
      "pause_on_canary_failure": true,
      "canary_pause_seconds": 21600
    },
    "matrix": {
      "cells": [
        { "name": "vn_sticky", "payment_method": "momo", "registration_country": "VN", "checkout_country": "VN", "promotion_country": "VN", "provider_country": "VN", "approve_country": "VN", "redirect_country": "VN", "strategy": "custom_promo", "sample_size": 5 }
      ]
    }
  }
}
```

HTTP 401 的支付账号按 OAuth Refresh Token、现有 Cookie `/api/auth/session`、隔离浏览器邮箱 OTP、Codex OAuth 的顺序恢复。每个候选 AT 只有再次探测为 HTTP 200 才会写入 Session JSON 和 SQLite。浏览器上下文按账号隔离并校验登录邮箱；`account_deactivated` 归类为永久失败，不会反复重登。

### SUB2API 导入

```json
{
  "sub2api": {
    "auth_mode": "auto",
    "verify_after_import": true
  }
}
```

`auth_mode` 可选 `auto`、`oauth`、`agent_identity`；Agent Identity 仅在显式 SUB2API 导入边界使用。`verify_after_import` 控制导入后是否执行连通性验证。

### 应急环境变量覆盖

当 OpenAI 轮换 Stripe publishable key 或 Sentinel SDK 版本、导致支付提链或注册 OTP 失败时，可用环境变量临时覆盖，无需改代码：

- `PP_STRIPE_PUBLISHABLE_KEY`：统一覆盖协议支付回退用的 Stripe publishable key（`sms_tool/gen_pp_link.py` 与 `services/protocol-payment/momo/ac_paylink_core.py` 两处共用）。checkout 响应通常自带该 key，仅在响应缺失时用到回退值；回退时会打印 WARN 日志。
- `OPENAI_SENTINEL_VERSION`：覆盖 Sentinel SDK 版本（默认值内置于 `sms_tool/sentinel_quickjs.py`）。SDK 下载返回 403/404 通常表示当前版本已被轮换失效，更新此变量或 config 的 `sentinel_version` 即可。
- `OPENAI_SENTINEL_DISABLE_QUICKJS`：设任意值可显式禁用真实 SDK 路径；生产注册默认不建议设置，
  因为纯 HTTP PoW 无法通过 Sentinel 深层校验。

启动前可运行 `python scripts/preflight_env.py` 检出 Node.js、Playwright Chromium 与关键 Python 包是否就绪。

## 常用操作

### ReMail 短效接码注册（仅 CLI）

```powershell
python chatgpt_phone_reg.py --remail-service-mode code --count 1 --workers 1 --registration-at-only --no-phone-reuse
```

### ReMail 长效邮箱注册并进行 SMSBower 手机验证

```powershell
python chatgpt_phone_reg.py --buy-remail-mailbox --remail-service-mode purchase --target-at200 40 --max-mailbox-purchases 80 --workers 10 --phone-reuse --phone-source smsbower
```

### ReMail 长效邮箱 AT-Only 协议注册

```powershell
python chatgpt_phone_reg.py --buy-remail-mailbox --remail-service-mode purchase --count 1 --workers 1 --registration-at-only --no-phone-reuse
```

该模式跳过 Codex OAuth RT 和手机验证，只在 Session 已落盘且 AT 探活返回 HTTP 200 后计为成功。

### CFWorker 邮箱注册

```powershell
python chatgpt_phone_reg.py --buy-cfworker-mailbox --cfworker-domain example.com --count 1 --workers 1
```

### 从邮箱文件注册

```powershell
python chatgpt_phone_reg.py --chatai-mailbox-file hotmail.txt --count 4 --workers 4
```

### 测试支付代理出口

```powershell
python chatgpt_phone_reg.py --test-payment-proxies --checkout-proxy-country GB --approve-proxy-country JP --update-proxy-country BR
```

### 批量协议支付（可断点续跑）

```powershell
python chatgpt_phone_reg.py --extract-payment-link --payment-method momo --email-file runtime\eligible.txt --workers 2 --payment-batch-id momo_vn_20260731 --payment-canary 5 --payment-retries 1
```

### 直卡 Checkout 提链

提取 PH/PHP 零金额 Checkout 链接：

```powershell
python chatgpt_phone_reg.py --extract-payment-link --payment-method direct_card --email user@example.com --proxy "http://proxy"
```

以 GoPay 单账号 Canary 执行 JIT AT、ID 矩阵、Checkout、TH Promotion/Update 和 Stripe init
能力探测；不会创建支付方式或发送 Confirm/Approve：

```powershell
python chatgpt_phone_reg.py --extract-payment-link --payment-method gopay --email-file runtime\canary.txt --payment-probe-only --payment-canary 1 --payment-batch-id gopay_id_probe --workers 1 --checkout-proxy-pool "http://checkout" --approve-proxy-pool "http://approve-jp`nhttp://approve-tr"
```

### 注册并自动导入 SUB2API

```powershell
python chatgpt_phone_reg.py --buy-remail-mailbox --count 1 --workers 1 --register-and-import --sub2api-auth-mode auto
```

### 查看 CLI 参数

```powershell
python chatgpt_phone_reg.py --help
```

## 测试、构建与发布

### 运行测试

```powershell
python -m pytest -q
python -m compileall -q sms_tool services/protocol-payment
dotnet test .\GPTRegisterTool.slnx -c Release
```

`global.json` 固定仓库 SDK，`Directory.Packages.props` 集中管理 NuGet 版本，标准 xUnit 工程位于 `tests/SmsWorkbench.Tests`。CI 同时执行 Python、C# 测试和规范桌面发布。

### 编译桌面端

```powershell
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
```

标准输出目录：

```text
dist/net10/SmsWorkbench.exe
```

### 构建安装器与便携包

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD
```

发布文件输出到 `dist/release/`：

- Windows 图形安装器。
- 便携 ZIP 包。
- SHA-256 校验文件。

内部签名构建可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD -SelfSign
```

### 发布检查

1. 确认 `config.json`、邮箱凭据、代理密码、API Key 和 Token 未进入 Git。
2. 执行全量测试、示例配置解析、Python 编译检查和 `git diff --check`。
3. 使用唯一支持的编译脚本更新 `dist/net10`。
4. 构建安装器、便携包和校验文件，并复核校验清单中的 SHA-256。
5. 确认待发布提交已推送，且 `git status --short` 为空；本地 `runtime/`、`sessions/` 等忽略数据不进入发布提交。
6. 在该提交上创建版本标签并上传同一次构建生成的 Release 资产。
7. GitHub Release 标题和正文统一使用中文；命令、文件名和错误码保持原始格式。

当前发布使用 `vYYYY.MM.DD`；同日文档或构建修订使用 `vYYYY.MM.DD.1` 等补丁标签。安装器、便携 ZIP 和 SHA-256 文件必须来自同一次 `scripts/build_installer.ps1` 构建，并在上传前校验摘要。发布资产固定为 `GPT-Register-Tool-Setup-<version>.exe`、`GPT-Register-Tool-win-x64-<version>.zip` 和 `GPT-Register-Tool-<version>.sha256.txt`。

## 数据与安全

- `config.json`、`sessions/`、`runtime/`、邮箱池和 Token 文件默认被 Git 忽略。
- 示例配置不包含真实 API Key、邮箱凭据或代理密码。
- ReMail API Key 与 Service Token 在异常和日志中会被脱敏。
- 支付链接、BA Token、账号 AT/RT 和邮箱凭据都属于敏感数据，不应公开分享。
- 第三方邮箱、支付、代理和接码服务的可用性及费用由对应服务商决定。

## 文档索引

- [架构说明](docs/architecture.md)
- [目录职责](docs/directory-map.md)
- [PayPal 0 元链接说明](docs/paypal-zero-due-link.md)
- [最新发布说明](docs/release-v2026.08.22.md)
- [代理指南](PROXY_GUIDE.md)

## 许可证与使用责任

请仅在获得授权并符合相关服务条款、地区法规及组织政策的场景中使用本项目。使用者需要自行承担第三方服务费用、账号安全和数据合规责任。
