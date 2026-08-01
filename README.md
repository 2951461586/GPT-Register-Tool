# GPT-Register-Tool

面向 Windows 的 ChatGPT 账号注册、邮箱 OTP、账号管理与协议支付链接提取工具。

项目采用 **WPF 桌面端 + Python 业务核心**：桌面端负责操作入口、配置和结果展示，Python 模块负责邮箱、注册、会话、支付、代理与外部服务协议。运行数据默认保存在本机，不写入 Git。

## 项目说明

### 主流程

```text
邮箱源
  -> ChatGPT 邮箱 OTP 注册
  -> 获取 Access Token / Session，并以稳定 HTTP 200 AT 作为入库边界
  -> 可选手机验证与 Codex OAuth
  -> JIT AT 探测/刷新与可选协议支付链接提取
  -> Session JSON + SQLite 索引
  -> WPF 桌面端统一管理
```

### 适用场景

- 从邮箱池、ReMail 或 CFWorker 执行批量邮箱注册。
- 统一轮询 Microsoft、Gmail、ReMail、CFWorker 等邮箱的 OTP。
- 管理本地账号、Session、额度状态和支付链接。
- 按阶段选择代理出口并提取 PayPal 或其他本地支付方式链接。
- 将账号数据导出为 Codex、CPA、SUB2API 等目标格式。

### 技术栈

| 层级 | 技术 |
| --- | --- |
| 桌面端 | WPF、.NET 10、C# |
| 业务核心 | Python 3、curl_cffi、requests、httpx、PyNaCl（Ed25519） |
| 数据存储 | JSON、JSONL、SQLite |
| 邮箱协议 | ReMail API、CFWorker、Microsoft Graph/OAuth、IMAP、Gmail IMAP |
| 支付协议 | Stripe Checkout、PayPal、GoPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、直卡 Checkout、MoMo |
| 浏览器辅助 | Playwright、Camoufox、CloakBrowser |

## 安装部署方式

### 环境要求

- Windows 10/11 x64。
- Python 3.10 或更高版本。
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

1. 在 **网络与支付** 中分别配置注册代理池、邮箱收件代理和协议支付代理池。
2. 在 **邮箱与收信** 中配置 ReMail、CFWorker 或其他邮箱源。
3. 按需配置 SMSBower、CPA、SUB2API 和各协议支付参数。
4. 保存后重新打开对应功能即可使用新配置。

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
- 支持仅注册并保存 AT，或继续生成支付链接。
- 注册成功判定以 AT 探测 HTTP 200 为准；稳定探测窗口内未持续返回 200 的候选不会进入 active 账号库。
- 注册流程不再执行 Agent Identity 阶段；需要 Agent Identity 时必须通过显式 SUB2API 导入路径处理。
- 选中邮箱记录时优先注册所选邮箱；未选中邮箱时显示邮箱源选择器。
- 注册、OTP、Session 获取、Codex OAuth 和支付提链分别记录阶段结果，避免把中间状态误报为成功。

### ReMail 邮箱源

- 一键注册来源中提供 `ReMail 长效邮箱`，统一使用 `purchase` 长效邮箱模式。
- 支持单笔或批量创建邮箱订单。
- 百账号批量下单默认按每个邮箱 2 秒扩展 HTTP 等待时间（至少 30 秒），可通过 `email_registration.remail.batch_timeout` 覆盖。
- 支持 `private_first`、`public_only` 库存策略。
- 支持指定项目、产品和邮箱后缀。
- 使用 `Idempotency-Key` 防止重试导致重复订单。
- 订单创建使用 API Key；收件使用独立的邮箱地址与 Service Token。
- Service Token 返回 401 时会用 API Key 查询所属订单；如服务端返回新 Token，会保存到 Session JSON 和 SQLite 后重试一次。
- `code` 订单只能在 `receiveUntil` 前收件，API Key 不能代替过期的 Service Token；需要后续持续查看收件箱时请选择 `purchase`。
- 邮件摘要无验证码时自动读取邮件详情，并执行时间、收件人、消息 ID 和已排除验证码过滤。
- 桌面端可从 ReMail 注册记录打开收件箱；查看模式会读取邮件完整正文和验证码。
- 日志会脱敏 API Key 和 Service Token。
- 自适应 OTP 轮询：初始延迟 1s，渐进退避（1s → 1.5s → 3s），根据邮件到达状态和服务器限流建议动态调整轮询间隔，减少无效请求。
- ReMail 收件时间允许默认 90s 的服务端时钟偏差；消息 ID 快照仍会阻止旧验证码被重复使用。
- ReMail 在 30s 内仍未收到验证码时会重发一次，剩余时间继续接受本次事务中的最新验证码。
- 已有 ReMail 订单可按 `remail://email---serviceToken---orderNo---purchaseId` 写入邮箱 Token 文件恢复使用，无需重复购买。
- 批量购买遇到超时或可重试 5xx 时，会先按请求时间窗、项目、产品和数量严格匹配新订单；仅在恰好匹配时自动恢复，避免响应丢失后重复购买。
- `ReMail 长效邮箱` 会按注册数量补足稳定 HTTP 200 AT，受 `--max-mailbox-purchases`、`--max-remail-cost` 和供应商死号比例熔断约束；注册批次默认启用 SMSBower 手机验证。

### 统一邮箱与 OTP

统一 mailbox seam 支持：

- ReMail。
- CFWorker 域名邮箱。
- Microsoft Graph/OAuth。
- Outlook/Hotmail IMAP 回退。
- Gmail IMAP 与 SMTP。
- Chatai、token 文件及历史邮箱池格式。

OTP 解析支持主题匹配、发件人过滤、收件人精确匹配、服务端时间戳过滤和候选排序。

### 协议支付提链

- 支持 PayPal、GoPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、直卡 Checkout、MoMo。
- BLIK 会提交一次性六位码并直接执行支付，只在单账号协议支付弹窗/命令中提供，不进入注册后自动提链或批量支付选择器。
- 直卡 Checkout（菲律宾 PH/PHP）：走 US 下单 → TR 刷优惠 → 校验 0 元，产出 `chatgpt.com/checkout/<entity>/<cs_id>` 直卡结账长链。
- MoMo（越南 VN/VND）：下单 → Stripe init → 强制 ₫0 → 建 MoMo PM → Confirm → Approve → 跟跳转，产出可扫的 `payment.momo.vn` 二维码（自动解码为 PNG，供“打开二维码”使用）。
- PayPal 支持 Hosted 长链接、PP 直链和强制 0 元试用模式。
- 支持 `checkout`、`approve`、`update` 分段代理。
- 动态代理会按支付方法自动改写国家与 Session，支持 US、JP、VN、ID、IN、NL、BR、KR、PL、CH、PH 等目标出口。
- 协议支付代理池按顺序探测，当前代理不可用或出口国家不匹配时自动切换下一条。
- 地区和代理选择保存为历史记录。
- 支持实际测试代理出口 IP、国家及预期地区是否匹配。
- 严格区分 Checkout、PM 创建、Confirm、首次 Poll、最终 Provider Redirect 等阶段。
- 批量提链应先用本地额度接口筛出非 401 账号，再执行支付协议；报告必须分别统计 AT 可用、套餐/试用资格、支付方式可见、Approve 成功和最终链接/二维码产物。
- MoMo 只有在返回 `ready_with_qr` 且产出 `payment.momo.vn` URL 或二维码文件时才算成功；`account_trial_ineligible`、`card_only_full_price` 和 `approve_result_blocked` 都是明确失败状态。
- 批量支付执行器支持 JIT AT、HTTP 401 邮箱 OTP OAuth 新 AT、资格探测、Canary 暂停、方法级并发、瞬态重试、原子断点和同批次续跑。
- MoMo 按 Checkout、Promotion、Stripe Provider、Approve、Redirect 分阶段使用代理；Kakao 输出结构化结果，只有明确的 Kakao/Nicepay Redirect 才算链接成功。

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
- 左侧栏“账号测活”负责 AT/额度健康检查；HTTP 401 会在支付 JIT 流程中尝试邮箱 OTP OAuth 刷新。
- 支持复制 AT、查看邮箱、重新注册和重新生成支付链接。
- 支持 Codex JSON、CPA、SUB2API 等导入导出流程。
- 账号列表保留注册地区、注册批次和入库状态，便于按 cohort 选择批量支付账号。
- 本地数据默认保存在 `sessions/` 和 `runtime/`，两者均被 Git 忽略。

### 桌面端批量支付操作

1. 在账号列表勾选要处理的账号，打开左侧“批量协议支付”或右键同名菜单。
2. 选择 MoMo/Kakao 等支付方式，设置并发、瞬态重试、Canary 数量、批次 ID 和代理 Seed。
3. 默认开启“401 时邮箱 OTP OAuth 新 AT”；需要只验证 AT 和注册地区矩阵时勾选“仅探测资格”，该模式不会调用任何支付适配器。
4. 通过“账号地区 / 支付资格矩阵”确认注册区、Checkout、Promotion、Provider、Approve 和 Redirect 的地区组合。
5. 相同模式、矩阵、代理与重试参数下重复使用同一批次 ID，可读取 `runtime/payment_batches/` 的原子断点并继续执行；运行参数变化时签名失配会重新执行，探测结果不会被正式支付复用。报告会分开显示 AT 200、JIT 刷新、资格、链接、二维码和失败计数。

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
  -> 配置、弹窗、列表、任务启动、状态展示

sms_tool/cli.py
  CLI 与任务编排
  -> 参数解析、批量任务、进程退出状态

sms_tool/registration.py
  注册主流程
  -> 邮箱 OTP、账号创建、Session、Codex OAuth

sms_tool/payment_auth.py / payment_batch.py
  JIT AT 门禁与批量协议支付
  -> 401 OAuth 刷新、资格矩阵、Canary、重试、断点报告

sms_tool/mailbox.py
  邮箱统一路由
  -> ReMail / CFWorker / Graph / IMAP / Gmail

sms_tool/payment_link_manager.py
  协议支付管理器
  -> 方法注册、分段代理、运行状态、统一结果

sms_tool/storage.py
  数据持久化
  -> Session JSON、SQLite、状态与去重

services/
  可选本地协议服务
  -> GoPay、ADB、邮件诊断、其他支付提取器
```

### 核心模块

| 模块 | 职责 |
| --- | --- |
| `SmsWorkbench/` | WPF 桌面界面、设置页、任务入口和本地状态展示 |
| `sms_tool/cli.py` | CLI 参数与高层任务编排 |
| `sms_tool/registration.py` | ChatGPT 注册、OTP、Session 和后续验证 |
| `sms_tool/mailbox.py` | 邮箱 provider 路由与统一 OTP 轮询 |
| `sms_tool/mailbox_remail.py` | ReMail 下单、收件、详情读取和 OTP 提取 |
| `sms_tool/mailbox_cfworker.py` | CFWorker 邮箱创建与收件 |
| `sms_tool/mailbox_graph.py` | Microsoft OAuth 与 Graph 边界 |
| `sms_tool/mailbox_gmail.py` | Gmail IMAP/SMTP 与 OAuth |
| `sms_tool/payment_link_manager.py` | 支付方法注册、状态机与统一结果 |
| `sms_tool/gen_pp_link.py` | PayPal/Stripe Checkout 与链接生成 |
| `sms_tool/paypal_proxy.py` | 分段代理、地区轮换和出口探测 |
| `sms_tool/storage.py` | SQLite、Session 索引和状态持久化 |
| `sms_tool/agent_identity.py` | 显式 SUB2API Agent Identity 凭据转换、Ed25519 密钥生成与持久化 |
| `sms_tool/sub2api_import.py` | SUB2API 导入（多认证模式） |
| `sms_tool/session_converter.py` | 多格式账号与 Session 转换 |
| `sms_tool/payment_auth.py` | 支付前 AT 探测、401 邮箱 OTP OAuth 刷新与安全遥测 |
| `sms_tool/payment_batch.py` | 批量协议支付、资格矩阵、Canary、重试与原子断点 |
| `sms_tool/registration_progress.py` | 注册阶段进度跟踪与持久化 |
| `sms_tool/error_classification.py` | 错误类型分类与重试/报告规范化 |

更详细的边界说明参见 [docs/architecture.md](docs/architecture.md)，目录职责参见 [docs/directory-map.md](docs/directory-map.md)。

## 核心配置

### ReMail

```json
{
  "email_registration": {
    "remail": {
      "enabled": true,
      "base_url": "https://remail.aishop6.com",
      "api_key": "",
      "project_id": 2,
      "product_id": 5,
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

注册流量走 JP 动态代理（`proxy.registration` / `proxy.pool`），worker 会刷新动态 Session 使各并发出口 IP 不同；邮箱 OTP 收取固定走 `mailbox_proxy`（默认 `http://127.0.0.1:7897`），不会继承注册代理；支付流量走独立的 `paypal.stage_proxies` / `protocol_payments.proxy_pool`。三者互不覆盖，详情可在桌面端 **设置 → 网络与支付** 的网络代理配置中查看和修改。

### 协议支付代理池

```json
{
  "protocol_payments": {
    "proxy_pool": [
      "http://user-region-JP-sid-session-t-5:pass@gateway-a:port",
      "http://user-region-JP-sid-session-t-10:pass@gateway-b:port"
    ]
  }
}
```

协议支付池与注册代理池相互独立。提链时会按支付地区改写 `region-XX` 或密码中的国家和动态 Session；显式传入 `--proxy` 或分段代理时才覆盖协议池。

### JIT AT 与批量支付

```json
{
  "registration": {
    "at_stability_probe_count": 2,
    "at_stability_probe_delay_seconds": 10,
    "at_probe_timeout_seconds": 30,
    "stage_concurrency": { "network": 4, "at_probe": 4, "payment": 2 }
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

HTTP 401 的支付账号默认直接进入邮箱 OTP OAuth 新 AT 流程，候选 AT 只有再次探测为 HTTP 200 才会持久化。`account_deactivated` 归类为永久失败，不会反复重登。

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

启动前可运行 `python scripts/preflight_env.py` 检出 Node.js、Playwright Chromium 与关键 Python 包是否就绪。

## 常用操作

### ReMail 短效接码注册（仅 CLI）

```powershell
python chatgpt_phone_reg.py --remail-service-mode code --count 1 --workers 1 --registration-at-only --no-phone-reuse
```

### ReMail 长效邮箱注册并进行 SMSBower 手机验证

```powershell
python chatgpt_phone_reg.py --buy-remail-mailbox --remail-service-mode purchase --target-at200 40 --max-mailbox-purchases 80 --workers 10 --phone-reuse --phone-source smsbower --skip-paypal-link
```

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

只执行 JIT AT 与注册地区矩阵校验，不调用支付适配器或创建支付方式：

```powershell
python chatgpt_phone_reg.py --extract-payment-link --payment-method momo --email-file runtime\eligible.txt --payment-probe-only --payment-batch-id momo_probe_20260731 --workers 2
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
```

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
2. 执行全量测试。
3. 使用唯一支持的编译脚本更新 `dist/net10`。
4. 构建安装器、便携包和校验文件。
5. 创建版本标签并上传 Release 资产。

当前发布使用 `vYYYY.MM.DD`；同日文档或构建修订使用 `vYYYY.MM.DD.1` 等补丁标签。安装器、便携 ZIP 和 SHA-256 文件必须来自同一次 `scripts/build_installer.ps1` 构建，并在上传前校验摘要。

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
- [最新发布说明](docs/release-v2026.07.31.1.md)
- [代理指南](PROXY_GUIDE.md)

## 许可证与使用责任

请仅在获得授权并符合相关服务条款、地区法规及组织政策的场景中使用本项目。使用者需要自行承担第三方服务费用、账号安全和数据合规责任。
