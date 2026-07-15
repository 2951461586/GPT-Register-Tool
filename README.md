# GPT-Register-Tool

Email-based ChatGPT registration workflow with session persistence and unified protocol payment-link extraction.

The active path is:

```text
mailbox source -> ChatGPT email OTP registration -> /api/auth/session access token
-> PayPal/GoPay/UPI/iDEAL/PIX/Kakao Pay/BLIK/TWINT link extraction
-> session JSON + SQLite index -> WPF management UI
```

The project does not require machine-specific absolute paths. Runtime data is kept under `sessions/` and `runtime/` by default and is ignored by Git.

## 中文运行要点（近期改造后）

本项目当前以桌面端 `SmsWorkbench` + Python CLI 为主线：桌面端只负责选择账号、弹窗配置、启动命令和展示状态；注册、邮箱收信、导出、支付链接等协议逻辑都在 `sms_tool/` 内维护。

近期重点改造：

- **一键注册**：默认走 HAR 对齐后的 `login_or_signup / passwordless_signup` 邮箱 OTP 流程；支持 `--registration-at-only` 只保存 AT，不强制生成支付链接；桌面端可选择“跳过支付链接/不生成支付链接”。
- **Sentinel**：优先使用 QuickJS Sentinel SDK 生成 `username_password_create` / `oauth_create_account` 所需 token；失败时再按旧逻辑回退。
- **邮箱 OTP**：CFWorker 域名邮箱、LuckMail、Microsoft Graph/OAuth、Outlook IMAP、**Gmail IMAP** 都通过统一 mailbox seam 轮询；CFWorker 已兼容 `verification code` 与 `login code` 两类主题，并为邮件服务端时间戳提供小幅容差，避免刚发码就被 `issued_after` 误过滤。
- **Gmail Provider**：`gmail` provider 仍支持 Gmail 导入格式、Gmail IMAP 收信和 Gmail SMTP 发信；设置页不再提供独立 Gmail 模块，高级参数直接编辑 `config.json`。每条 Gmail 记录只代表导入时填写的精确邮箱地址，不生成、映射或复用点号/`+tag` 别名。
- **协议支付管理器**：`sms_tool.payment_link_manager` 统一 PayPal、GoPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT 的方法注册、分段代理、运行状态和结果格式。桌面左侧栏使用“协议支付提链”，设置页使用单一“协议支付”分类，不再显示独立 PayPal 浏览器或 GoPay 分类。
- **额度查询**：桌面额度查询弹窗固定为 `600px` 宽，只调用本地额度接口，不再自动 relogin；接口返回 `401` 时直接显示为“401 / AT失效”。
- **账号详情**：账号详情页提供“一键复制AT”，仅在账号存在 ChatGPT Access Token 时启用。
- **CFWorker 域名邮箱**：导入格式支持 `cfworker://user@domain`；可用 `--buy-cfworker-mailbox --cfworker-domain <domain>` 购买/创建。若 OTP 验证通过但最终 `create_account` 返回 `registration_disallowed`，说明服务端拒绝该邮箱/注册上下文，不是收件失败。
- **导出账号 / CPA JSON**：`sms_tool/session_converter.py` 引入多格式转换核心，导出逻辑可以识别更宽松的 session 对象形态。
- **模块拆分**：`registration.py`、`mailbox.py` 已拆成更细的协议/adapter 模块，原文件保留兼容 wrapper，旧 CLI/WPF/测试 patch seam 仍可用。

## Quick Start

1. Clone the repository.

```powershell
git clone <repo-url>
cd GPT-Register-Tool
```

2. Install Python dependencies.

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` is the only dependency manifest kept in the repository.

3. Create local config.

```powershell
copy config.example.json config.json
```

4. Edit `config.json`.

Required choices:

- `proxy.default`: local HTTP/SOCKS proxy, or `direct`.
- `email_registration.token_file`: relative mailbox pool path such as `mailbox_tokens.txt`, or leave empty and use LuckMail.
- `email_registration.luckmail_api_key`: required only for LuckMail purchase/token flows.
- `email_registration.cfworker_*`: CFWorker domain mailbox settings. `cfworker_poll_proxy` controls whether inbox polling uses the selected proxy, `cfworker_direct_fallback` allows direct retry after proxy failure, and `cfworker_otp_issued_after_grace_seconds` controls the timestamp grace window used for fresh OTP filtering.
- `email_registration.gmail.*`: Gmail provider settings. `enabled + email + app_password` is the simplest receive/send setup. `auth_mode=oauth_refresh` additionally requires `client_id`, `client_secret`, and `refresh_token`. `imap_folders` defaults to `INBOX,[Gmail]/Spam,[Gmail]/All Mail`, and `smtp_*` controls local Gmail SMTP sending.
- `k12.workspace_ids`: default workspace ID (used by workspace scan fallback).
- `paypal.billing_regions`: Checkout billing country/currency order. Current hosted long-link mode uses the configured region order; the default example is `["DE"]` for Germany/EUR. The desktop `[配置] -> [协议支付] -> 订单生成地区` dropdown supports Japan, United States, Australia, Germany, France, United Kingdom, India, and Brazil.
- `paypal.link_generation_type`: Desktop `[配置] -> [协议支付] -> PayPal生成类型` selector. `hosted_long_url`（长链） runs `checkout -> stripe init -> stripe_hosted_url` and stores a `pay.openai.com/c/pay/...` hosted long URL. `paypal_direct`（PP直链） runs `checkout -> stripe init -> pm create(type=paypal) -> confirm`, follows the Stripe `pm-redirects` URL, and stores a `paypal.com/agreements/approve?ba_token=...` approval URL without logging the full token. `paypal_direct_zero_due`（PP直链-强制0元试用） uses the same PP直链 flow but keeps `require_zero_due=true`; if Stripe init does not return `amount_due=0`, generation fails with `checkout_not_zero_due` instead of outputting a non-trial BA link. In this strict mode a failed regeneration does not fall back to hosted long-link mode and does not reuse an older saved BA link.
- `paypal.stage_proxies`: 分段代理路由配置，支持三段式代理池:
  ```json
  "stage_proxies": {
    "checkout": "http://user:pass-JP@gate:1000",
    "provider": "http://user:pass-GB@gate:1000",
    "approve": "http://user:pass-GB@gate:1000"
  }
  ```
  - `checkout`: Stage 1 代理 (JP/TH 出口)，用于 ChatGPT checkout 创建
  - `provider`: Stage 2 代理 (目标国出口)，用于 Stripe init/PM/confirm
  - `approve`: Stage 3 代理 (目标国出口)，用于 ChatGPT approve + 轮询 redirect
  - 如果 `approve` 未配置，降级使用 `provider`
  - CLI 参数 `--checkout-proxy` / `--provider-proxy` / `--approve-proxy` 可覆盖配置文件
- `protocol_payments`: 统一协议提链配置。`enabled_methods` 控制可用方式，`reference_root` 默认指向 `services/protocol-payment`，`state_file` 记录状态机结果，`methods.<method>.proxy` 为 iDEAL/PIX/Kakao Pay/BLIK/TWINT 的 sticky 代理 Seed。BLIK 还需要 `methods.blik.blik_code` 或 CLI `--blik-code`。
- `paypal.target_country`: 目标国家代码 (如 `GB`, `DE`, `AU`)，默认 `GB`。决定 Stripe checkout 的账单国家和 PayPal BA 链的区域。
- `paypal.require_zero_due`: 是否要求 0 元金额，默认 `true`。设为 `false` 允许非零金额 (无 promo 时)。
- `paypal.link_mode`: current default is `chatgpt_checkout`, which stores the hosted long checkout URL from Stripe init instead of attempting BA extraction.
- `paypal.redirect_url_format`: ignored by the hosted long-link path; kept only for compatibility with the older BA/Stripe redirect path.
- `paypal.use_elements_session`: current default is `true`; it requests Stripe Elements session data before tax refresh, payment method creation, and confirm.
- `paypal.resolve_ba_redirect`: current default is `false` for hosted long-link mode.
- `paypal.require_ba_token`: current default is `false` for hosted long-link mode.
- `paypal.explicit_proxy_overrides_stage_proxies`: current default is `false`, so a UI/CLI `--proxy` is used as the default candidate proxy but does not override `paypal.stage_proxies.confirm=direct`.
- `paypal.checkout_ui_mode`: current default is `hosted`; together with `link_mode=chatgpt_checkout` it now follows `ChatGPT checkout -> Stripe /payment_pages/{cs_id}/init -> stripe_hosted_url`, then normalizes `checkout.stripe.com/c/pay/...` to `pay.openai.com/c/pay/...`. It does not enter Stripe confirm/approve. Keep `paypal.require_zero_due=true` to stay strictly on the 0 yuan/free-trial path.
- `--regenerate-paypal-link --proxy ...`: forces PayPal/Stripe link regeneration through the selected proxy. Batch regeneration is capped by `paypal.max_regenerate_workers` (default `1`) and staggered by `paypal.regenerate_delay_seconds` to avoid checkout `429` rate limits; with `paypal.explicit_proxy_overrides_stage_proxies=false`, `--proxy` still does not override stage-specific routes such as `confirm=direct`.
- `paypal_browser.browser_engine`: project-local PayPal browser engine, default `camoufox` with `cloakbrowser` fallback support from `sms_tool.paypal_auto`.
- `paypal_browser.headless` / `paypal_browser.manual_human_verification`: set `headless=false` and `manual_human_verification=true` when PayPal shows a visible "Confirm you're human" challenge so the browser can wait for manual completion.
- `paypal_browser.phone_pool`: PayPal browser payment SMS-phone pool. If empty, the adapter falls back to `paypal_nocard.phone_pool`.
- `gopay.one_click_mode`: `link`, `provider`, or `wa_rebind`. `provider` uses the local `PaymentService` on `gopay.payment_service_addr`; `wa_rebind` additionally routes GoPay payment OTP through the WA channel and can call a GoPay App service to change phone after payment.
- `upi.checkout_country` / `upi.payment_country`: UPI hosted QR generation now separates checkout billing country/currency from the intended local payment-method country. The current default is `checkout_country="JP"` with JP checkout proxy routing and `payment_country="IN"` with India provider/Stripe routing. CLI overrides are `--checkout-country JP --payment-country IN`; legacy `--target-country` remains a UPI checkout-country alias. If Stripe does not expose `upi` for that checkout, the result fails with `upi_not_available` and reports both countries.
- `upi.billing_regions`: legacy checkout billing-country list kept for compatibility; for UPI it falls behind `upi.checkout_country` / `upi.checkout_billing_country`.
- `gopay.payment_service_addr`: local GoPay payment gRPC endpoint, default `127.0.0.1:50051`.
- `gopay.wa_rebind`: optional WA-channel app-state/rebind settings. `gopay_app_service_addr` points to the GoPay App gRPC provider, `wa_phone` is the WA payment phone, and `rebind_phone` is the phone to bind after payment.
- `cpa_mode.api_url` / `cpa_mode.api_token`: CPA management API target for one-click import.
- `codex_oauth.allow_passwordless_takeover`: default `false`; only affects manual Codex export/refresh. CPA import now consumes existing AT-only JSON and no longer depends on RT refresh.
- `codex_oauth.require_registration_refresh_token`: default `true`; a new registration is not counted as successful until Codex OAuth returns a refresh token.
- `codex_oauth.require_registration_phone_verification`: default `true`; when a phone pool is configured, registration must complete SMS verification before the session is saved.
- Desktop `【一键注册+支付链接】` supports two registration modes. `邮箱注册（跳过手机）` keeps the historical AT-only path and emits `--registration-at-only --no-phone-reuse`. `手机接码注册+绑定邮箱+PP直链0元` keeps the selected/purchased mailbox as the account email, enables SMSBower phone verification during the Codex OAuth step, and forces strict zero-due PayPal direct generation with `--phone-reuse --phone-source smsbower --max-reuse-count 1 --paypal-generation-type paypal_direct_zero_due --payment-method paypal`.
- `--registration-at-only`: UI default for "one-click registration + payment link"; skips Codex OAuth/phone SMS and stores the ChatGPT access token only.
- `--one-click-sms`: runs Codex OAuth for selected existing accounts, completes phone SMS verification via the phone pool, and stores the OAuth refresh token. Batch one-click SMS forces one phone per email account and prints the successful email→phone mapping in the JSON result.
- `phone_reuse.source`: one-click SMS source, `smsbower` for SMSBower platform numbers, `nextsms` for NexSMS/NextSMS (`https://sms.nextactionplus.com/api/`) orders, or `phone_pool` for configured `phone----sms_api_url` entries in `phone_reuse.phone_pool`. SMSBower OpenAI defaults to `service=dr`; NextSMS OpenAI defaults to `service=openai`, `country=US`, and `pricing_option=0`. Outside one-click SMS, one acquired activation or configured number is reused up to `phone_reuse.max_reuse_count` times, default `1`. For single-phone batch registration, the phone verification and OAuth token exchange run in one serialized lane; use `phone_reuse.send_cooldown_seconds` or `--phone-send-cooldown` to slow repeated add-phone sends to the same number. `phone_reuse.send_retry_attempts` handles recoverable add-phone rate limits without immediately canceling the provider activation/order. Provider `number_attempts` controls same-run number replacement for rejected or silent numbers; `phone_send_failed:fraud_guard` keeps replacing provider numbers until a send succeeds or the provider can no longer supply numbers.

5. Run one registration.

```powershell
python chatgpt_phone_reg.py --count 1
```

6. Build and start the WPF app. **Always use `SmsWorkbench/build_dotnet.ps1`** — do **not** run `dotnet build` directly. The script uses `dotnet publish` to emit the single canonical artifact at `dist/net10/SmsWorkbench.exe`, then automatically removes the intermediate `SmsWorkbench/bin/Debug/net10.0-windows` and `SmsWorkbench/bin/Release/net10.0-windows` workspaces so they are never mistaken for distribution output.

   构建并启动 WPF 桌面程序。**必须使用 `SmsWorkbench/build_dotnet.ps1` 脚本编译，禁止直接运行 `dotnet build`**。脚本通过 `dotnet publish` 将唯一的可执行产物输出到 `dist/net10/SmsWorkbench.exe`，发布完成后自动清理 `SmsWorkbench/bin/Debug/net10.0-windows` 和 `SmsWorkbench/bin/Release/net10.0-windows` 等中间目录，防止误用为分发路径。

```powershell
# ✅ 正确方式：使用编译脚本
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
.\dist\net10\SmsWorkbench.exe

# ❌ 错误方式：直接 dotnet build 会输出到 SmsWorkbench\bin\Release\net10.0-windows\，
#    该路径只是中间产物，不是分发目录，且不会自动清理。
#    dotnet build SmsWorkbench\SmsWorkbench.csproj   <-- 不要这样做
```

7. Build release installers when publishing a Windows build. The installer script rebuilds the desktop app, packages only tracked project files plus the fresh `dist/net10` publish output, and writes assets under `dist/release/`. The generated setup executable is a graphical Windows installer using the app icon, and it lets users choose the install path; `/S /DIR=...` remains available for silent installs.

   发布 Windows 版本时构建安装包。安装包脚本会重新构建桌面程序，只打包 Git 已跟踪的项目文件和最新的 `dist/net10` 发布产物，并把发布资产写入 `dist/release/`。生成的安装程序是图形化 Windows 安装向导，使用项目应用图标，支持用户选择安装路径；仍保留 `/S /DIR=...` 静默安装参数。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD
```

For internal distribution, build with a reusable self-signed Authenticode certificate and publish the exported `.cer` next to the installer:

内部发布时，可以使用可复用的自签名 Authenticode 证书构建安装包，并把导出的 `.cer` 证书与安装器一起发布：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD -SelfSign
```

Internal users must import `GPT-Register-Tool-Internal-CodeSigning.cer` into both `Trusted Publishers` and `Trusted Root Certification Authorities` before running the installer; otherwise Windows will still treat the self-signed publisher as untrusted. The release also includes `trust_internal_certificate.ps1`, which imports the certificate into the current user certificate stores.

内部用户运行安装器前，必须先把 `GPT-Register-Tool-Internal-CodeSigning.cer` 导入 `Trusted Publishers` 和 `Trusted Root Certification Authorities`。否则 Windows 仍会把自签名发布者视为不受信任。Release 中同时提供 `trust_internal_certificate.ps1`，可把证书导入当前用户证书存储。

## Mailbox Inputs

Standard Microsoft Graph/OAuth pool:

```text
email---password---refresh_token---access_token---0
```

Chatai mailbox pool:

```text
email----password----client_id----refresh_token
```

CFWorker domain mailbox:

```text
cfworker://oai-xxxx@edu.liziai.cloud
```

CFWorker mailboxes are polled through the configured Worker endpoint. OTP extraction accepts both `Your temporary ChatGPT verification code` and `Your temporary ChatGPT login code`, and `email_registration.cfworker_otp_issued_after_grace_seconds` gives the provider a small timestamp grace window so a message received 1-10 seconds before the local resend-return time is still considered fresh.

Gmail mailbox (app password mode):

```text
gmail://user@gmail.com---abcd efgh ijkl mnop
```

Gmail mailbox (OAuth refresh mode):

```text
gmail://user@gmail.com----client_id.apps.googleusercontent.com----client_secret----refresh_token
```

> 注意：Gmail 的稳定协议接入依赖 **应用专用密码** 或 **OAuth refresh token**。单独的 “邮箱----密码----2FA/TOTP 密钥” 不是当前项目的 Gmail IMAP/SMTP 直接导入格式。

> Gmail alias 功能已移除。邮箱池、OTP 收件人匹配和凭据查找都使用完整邮箱地址进行精确匹配；`user@gmail.com` 的凭据不会自动用于 `u.ser+tag@gmail.com` 或 `user@googlemail.com`。

The parser accepts UTF-8 with or without BOM. It also repairs the known malformed Chatai compact form:

```text
name@+aliasdomain.com -> name+alias@domain.com
```

When `--chatai-mailbox-file` or `--mailbox-file` is explicitly provided and no mailbox can be parsed, the CLI exits with code `2` instead of silently creating a new LuckMail mailbox.

## Common Commands

Register from configured mailbox source:

```powershell
python chatgpt_phone_reg.py --count 4 --workers 4 --proxy socks5h://127.0.0.1:7897
```

Register from Chatai file:

```powershell
python chatgpt_phone_reg.py --chatai-mailbox-file hotmail.txt --count 4 --workers 4
```

Buy LuckMail mailbox and register:

```powershell
python chatgpt_phone_reg.py --buy-luckmail-mailbox --count 1
```

Create a CFWorker domain mailbox and register without generating a payment link:

```powershell
python chatgpt_phone_reg.py --buy-cfworker-mailbox --cfworker-domain edu.liziai.cloud --count 1 --workers 1 --registration-at-only --no-phone-reuse --skip-paypal-link
```

Register again from selected/imported CFWorker mailbox lines:

```powershell
python chatgpt_phone_reg.py --chatai-mailbox-file selected_mailboxes.txt --count 1 --workers 1 --registration-at-only --no-phone-reuse --skip-paypal-link
```

View Gmail inbox through the unified mailbox seam:

```powershell
python chatgpt_phone_reg.py --view-inbox --mailbox-file mailbox_tokens.txt --email user@gmail.com --inbox-limit 20
```

Send a Gmail test message to yourself:

```powershell
python chatgpt_phone_reg.py --gmail-send --gmail-send-self --email user@gmail.com
```

Rebuild SQLite index from existing session JSON files:

```powershell
python chatgpt_phone_reg.py --rebuild-sqlite
```

List saved PayPal links:

```powershell
python chatgpt_phone_reg.py --list-paypal-links
```

Regenerate any supported protocol payment link for one account:

```powershell
python chatgpt_phone_reg.py --email user@example.com --regenerate-paypal-link
```

List unified payment methods and local adapter availability:

```powershell
python chatgpt_phone_reg.py --list-payment-methods
```

Extract directly from an Access Token:

```powershell
python chatgpt_phone_reg.py --extract-payment-link --payment-method pix --at <ACCESS_TOKEN> --proxy <STICKY_PROXY_SEED>
```

For an India UPI hosted long link:

```powershell
python chatgpt_phone_reg.py --email user@example.com --regenerate-paypal-link --payment-method upi
```

Refresh an auth session after manual payment/login:

```powershell
python chatgpt_phone_reg.py --email user@example.com --refresh-session
```

Mark a paid account as paid:

```powershell
python chatgpt_phone_reg.py --email user@example.com --mark-paypal-status completed
```

Run PayPal browser payment automation for an existing account with a saved payment link:

```powershell
python chatgpt_phone_reg.py --email user@example.com --one-click-pay --proxy socks5h://127.0.0.1:7897
```

Batch mode accepts one email per line:

```powershell
python chatgpt_phone_reg.py --one-click-pay --email-file pending_emails.txt --workers 4 --proxy socks5h://127.0.0.1:7897
```

The PayPal one-click path uses `sms_tool.paypal_auto`, generates PayPal signup identity/address/card data inside this project, then runs the project-local browser flow against the already saved SQLite/session `paypal_url`. It does not regenerate PayPal links; run `--regenerate-paypal-link` explicitly before one-click payment when an account has no saved link. The older pure HTTP no-card module remains in `sms_tool.paypal_nocard` but is no longer the default PayPal `--one-click-pay` path.

Start the local GoPay provider services:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_gopay_provider.ps1
```

Run GoPay protocol payment through the project-local PaymentService:

```powershell
python chatgpt_phone_reg.py --email user@example.com --one-click-pay --payment-method gopay
```

GoPay one-click payment uses protocol mode by default when `gopay.one_click_mode=protocol`
or `provider`. This keeps the main project as the owner of ChatGPT account state,
SQLite/session updates, and checkout generation, while using the pure Midtrans/GoPay
HTTP protocol for the actual wallet linking and charge. Compared with the external
`gopay-deploy` worker, it avoids a second inbox/worker queue and can mark the same
account row `otp_required` or `completed`. OTP can come from the local ADB sidecar
or from SMSBower by setting `gopay.otp_source=smsbower`.

SMSBower mode reuses the same secret/endpoint/timeout style as the one-click SMS
configuration, but GoPay needs its own SMSBower service/country code. Configure
either `gopay.otp.smsbower.service/country` or `phone_reuse.smsbower.gopay_service`
and `phone_reuse.smsbower.gopay_country`; do not reuse the OpenAI/Ghana
`service=dr,country=38` values for GoPay. When `register_account=true`, the
provider now runs the GoPay Android 2.10 pure-protocol signup/login/PIN flow in
Python (`services/gopay-flow/gopay_pure_protocol.py`). It does not call
`services/gopay-app` gRPC or the old `gopay-deploy` / `opai` client for
SMSBower account bootstrap.

```json
{
  "gopay": {
    "one_click_mode": "protocol",
    "pure_xe_mode": "enhanced",
    "pure_protocol_timeout_seconds": 35,
    "pure_protocol_debug": false,
    "otp_source": "smsbower",
    "country_code": "62",
    "otp_channel": "sms",
    "pin": "147258",
    "otp": {
      "source": "smsbower",
      "smsbower": {
        "api_key": "$SMSBOWER_API_KEY",
        "service": "<gopay-service-code>",
        "country": "<indonesia-country-code>",
        "min_balance_rp": 1,
        "balance_wait_timeout_seconds": 120,
        "balance_poll_interval_seconds": 5,
        "sms_timeout": 120,
        "sms_poll_interval": 5
      }
    }
  }
}
```

Protocol flow:

1. Load the account session/access token and call `PaymentService.StartGoPay`.
2. Create a ChatGPT checkout session for Plus with IDR billing.
3. Create a Stripe GoPay payment method and confirm the Stripe payment page.
4. Follow the Stripe/Midtrans redirect and resolve the Midtrans snap token.
5. Load the Midtrans transaction and POST `/snap/v3/accounts/{snap}/linking`.
6. If Midtrans reports the wallet is already linked, DELETE `/snap/v3/accounts/{snap}/gopay` and retry linking.
7. POST GoPay `/v1/linking/validate-reference` and `/v1/linking/user-consent`.
8. For `otp_source=smsbower`, acquire a GoPay phone number from SMSBower, register/login the GoPay wallet via pure Python protocol, set PIN through the second CVS OTP flow, then require `/v1/payment-options/balances` to be at least `min_balance_rp` before checkout. If the balance is not ready, the provider waits up to `balance_wait_timeout_seconds` and polls every `balance_poll_interval_seconds`; balance-supplement APIs are intentionally not called because they increase payment risk. Otherwise use configured `gopay.phone`.
9. For `otp_channel=sms`, POST `/v1/linking/resend-otp` to force SMS OTP; WA/default only uses consent delivery.
10. Persist `flow_id`; SMSBower mode immediately calls `CompleteGoPay` and waits for the code, while manual/ADB modes mark `otp_required`.
11. When OTP is available, call `PaymentService.CompleteGoPay`.
12. POST `/v1/linking/validate-otp`, tokenize the PIN, then POST `/v1/linking/validate-pin`.
13. POST Midtrans `/snap/v2/transactions/{snap}/charge`; fraud deny is surfaced as a terminal payment failure.
14. Validate/confirm the GoPay payment challenge, tokenize the PIN again, then POST `/v1/payment/process`.
15. Poll Midtrans transaction status until settlement/capture.
16. Verify the ChatGPT checkout and mark the account `completed`; if configured, call the ADB sidecar to unlink OpenAI from GoPay.

WA-channel rebind mode is intentionally explicit because it spans two providers:

```json
{
  "gopay": {
    "one_click_mode": "wa_rebind",
    "otp_channel": "wa",
    "wa_rebind": {
      "enabled": true,
      "gopay_app_service_addr": "127.0.0.1:50060",
      "user_id": "local",
      "wa_phone": "859xxxxxxxx",
      "rebind_phone": "859yyyyyyyy"
    }
  }
}
```

The adapted local flow uses `PaymentService.StartGoPay/CompleteGoPay` for the ChatGPT + Midtrans charge, then calls `GopayAppService.AuthStart/AuthComplete` and `ChangePhoneStart/ChangePhoneComplete` when payment succeeds. OTPs remain explicit CLI inputs:

```powershell
python chatgpt_phone_reg.py --email user@example.com --one-click-pay --payment-method gopay --gopay-otp 123456 --gopay-rebind-otp 654321
```

If the payment OTP or rebind OTP is not supplied, the account is persisted with the next required state (`otp_required`, `wa_auth_otp_required`, or `wa_rebind_otp_required`) instead of guessing or blocking inside the UI.

Import paid accounts into CPA:

```powershell
python chatgpt_phone_reg.py --import-cpa --email-file paid_emails.txt
```

CPA import now accepts existing session JSON that contains an `access_token` even when `refresh_token`
is missing. If the source file does not already have `id_token`, the tool synthesizes a CPA-compatible
one when possible and uploads the normalized JSON directly to CPA.

Run one-click account scan for normal account health checks and optional AT relogin:

```powershell
# Scan account status; workspace detection/switch is intentionally disabled here.
python chatgpt_phone_reg.py --one-click-scan --email user@example.com --session-file sessions/session_user.json

# Allow relogin on AT/quota 401 to refresh a new AT.
python chatgpt_phone_reg.py --one-click-scan --email user@example.com --quota-auto-relogin
```

账号列表中的“额度情况”来自 CPA/CLIProxyAPI Management API。工具会通过 `GET /v0/management/auth-files` 找到账号的 `auth_index`，再通过 `POST /v0/management/api-call` 代理请求 Codex usage endpoint，并把结果写入 SQLite 的 `quota_status`。如果本地没有配置 `cpa_mode.api_url` / `cpa_mode.api_token`，或该账号尚未导入 CPA/CLIProxyAPI，列表会显示“未知”。可手动刷新：

```powershell
python chatgpt_phone_reg.py --refresh-cpa-quota --email user@example.com
```

## WPF Behavior

`SmsWorkbench` is a launcher and management UI. It reads `config.json`, starts the Python CLI, displays mailbox/session/SQLite state, and exposes maintenance actions.

UI responsibilities are intentionally thin:

- The account list supports row selection plus checkbox-backed batch actions; double-clicking a row no longer opens details.
- Account details are opened from the explicit detail button.
- The inbox view uses an in-app mail detail popup and can copy recognized 5-8 digit verification codes.
- Gmail rows are loaded only from actual mailbox records. The desktop app no longer exposes an alias manager or creates virtual Gmail alias rows.
- Marking payment complete updates PayPal status only. CPA import is a separate operation.
- The desktop UI uses a fixed gray-dominant minimalist dark theme; black is reserved for the sidebar, log console, and other low-emphasis surfaces.
- Desktop icons are generated from the same kitten mark: `SmsWorkbench/Assets/app-icon.ico` and `SmsWorkbench/Assets/black-kitten.png`.
- One-click payment is an explicit action. PayPal launches the project-local browser adapter against an already saved payment link; GoPay launches the provider/protocol workflow selected in `gopay.one_click_mode`; rows are marked `completed` only after the backend returns success.

PayPal link buttons open Google Chrome with:

```text
chrome.exe --new-window --incognito <paypal_url>
```

If Chrome is not installed in a standard location, the app falls back to the system default browser.

The account list deduplicates rows by the exact normalized email address. When a mailbox pool entry later gains SQLite/session status, the SQLite/session row is shown instead of a second duplicate mailbox row.

## Project Modules

The project is split into explicit responsibility seams:

- `chatgpt_phone_reg.py`: compatibility entrypoint that only delegates into `sms_tool.cli`.
- `sms_tool.cli`: argument parsing and command orchestration. Optional Codex, CPA, PayPal payment, and session-refresh modules are imported lazily only by the command that needs them.
- `sms_tool.mailbox`: mailbox provider routing and OTP polling compatibility seam. Format parsing, LuckMail, CFWorker, Microsoft Graph, Outlook IMAP, **Gmail IMAP/SMTP**, and OTP text extraction live in focused modules such as `mailbox_parsers.py`, `mailbox_luckmail.py`, `mailbox_cfworker.py`, `mailbox_graph.py`, `mailbox_gmail.py`, `outlook_imap.py`, and `mail_otp.py`.
- `sms_tool.registration`: ChatGPT signup orchestration compatibility seam. Auth flow, account creation/session fetch, batch runner, Sentinel token extraction, auth-state dump, and OTP strategy live in `auth_flow.py`, `account_creation.py`, `batch_runner.py`, `sentinel_tokens.py`, `auth_state.py`, and `otp_strategy.py`.
- `sms_tool.session_converter`: export-account conversion seam; normalizes supported session JSON shapes into CPA/Codex-compatible payloads.
- `sms_tool.account_seed`: shared seam for loading session JSON/SQLite account seed data and extracting access tokens.
- `sms_tool.payment_link_manager`: unified payment-method registry, state machine, adapter routing, normalized result schema, and JSONL run history.
- `sms_tool.gen_pp_link` / `sms_tool.paypal_links`: native PayPal/UPI generation and safe persisted-link regeneration compatibility seams.
- `sms_tool.paypal_auto`: project-local browser automation helper. It does not own account lookup or link regeneration.
- `sms_tool.paypal_nocard`: older explicit no-card PayPal agreement module, kept as an opt-in compatibility path.
- `sms_tool.gopay_wa_rebind`: WA-channel GoPay app auth and change-phone orchestration after a successful provider payment.
- `sms_tool.grpcurl_client`: shared boundary for optional local gRPC provider services.
- `services/gopay-flow`: project-local GoPay PaymentService and protocol implementation.
- `services/gopay-app/proto`: GoPay App gRPC protocol contract used by WA rebind mode.
- `services/gopay-adb`: local ADB HTTP sidecar for OTP notification polling and unlink actions.
- `services/protocol-payment`: vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT protocol extractors used by the unified manager.
- `services/mail-otp-web`: standalone Microsoft Graph inbox/OTP diagnostic UI. It does not own registration persistence.
- `sms_tool.codex_oauth`, `sms_tool.codex_export`, `sms_tool.cpa_import`: Codex OAuth/export and CPA upload boundaries.
- `sms_tool.storage`: SQLite schema, migrations, deduplication, status updates, and session-index rebuilds.
- `SmsWorkbench`: WPF launcher and management UI. It starts CLI commands and displays local state; protocol details stay in Python modules.

The same split is maintained in [docs/architecture.md](docs/architecture.md). Physical directory classification is maintained in [docs/directory-map.md](docs/directory-map.md).


## Cleanup and Ownership Rules

- The old `browser_extensions/paypal_autofill` extension and its tests were removed. PayPal browser payment now lives behind `sms_tool.paypal_auto`; do not add extension-side code back unless it becomes a separately documented adapter.
- Payment modules must not read SQLite/session files by reimplementing lookup logic. Use `sms_tool.account_seed` for seed loading and access-token extraction.
- Runtime probes, HAR-derived scratch files, browser screenshots, caches, and generated sessions stay under `runtime/`, `sessions/`, or ignored tool caches, not in source modules.
- `config.example.json` is the portable template. Local `config.json` and `sms_tool/config.json` remain machine-local config surfaces and must not be used as documentation substitutes.

## Tests

Tests are offline by default and live under `tests/`.

```powershell
python -m unittest discover -s tests
```

See [tests/README.md](tests/README.md) for file-level test ownership. Live browser, network, and SQLite smoke checks must stay opt-in through explicit commands or environment variables.

## Data and Git Hygiene

Ignored local files:

- `config.json`
- `sms_tool/config.json`
- `services/mail-otp-web/config.json`
- `mailbox_tokens.txt`
- `sessions/`
- `runtime/`
- `dist/`
- `.dotnet/`

Do not commit tokens, mailbox refresh tokens, access tokens, cookies, card data, or generated session files.

## Module Boundaries

See [docs/architecture.md](docs/architecture.md) for the responsibility split between UI, CLI orchestration, mailbox providers, registration protocol, PayPal link generation, session refresh, and storage.

See [docs/directory-map.md](docs/directory-map.md) before adding new modules or moving files. New UI work belongs in `SmsWorkbench/`, command/protocol work belongs in `sms_tool/`, optional local providers belong in `services/`, and generated state belongs in `runtime/` or `sessions/`.
