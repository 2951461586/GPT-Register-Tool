# GPT-Register-Tool

Email-based ChatGPT registration workflow with session persistence and PayPal/GoPay/UPI payment automation.

The active path is:

```text
mailbox source -> ChatGPT email OTP registration -> /api/auth/session access token
-> PayPal/GoPay/UPI payment link or protocol payment -> session JSON + SQLite index -> WPF management UI
```

The project does not require machine-specific absolute paths. Runtime data is kept under `sessions/` and `runtime/` by default and is ignored by Git.

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
- `paypal.billing_regions`: Checkout billing country/currency order. Current hosted long-link mode uses the configured region order; the default example is `["DE"]` for Germany/EUR. The desktop `[配置] -> [代理/支付] -> 订单生成地区` dropdown supports Japan, United States, Australia, Germany, France, United Kingdom, India, and Brazil.
- `paypal.link_generation_type`: Desktop `[配置] -> [代理/支付] -> PayPal生成类型` selector. `hosted_long_url`（长链） runs `checkout -> stripe init -> stripe_hosted_url` and stores a `pay.openai.com/c/pay/...` hosted long URL. `paypal_direct`（PP直链） runs `checkout -> stripe init -> pm create(type=paypal) -> confirm`, follows the Stripe `pm-redirects` URL, and stores a `paypal.com/agreements/approve?ba_token=...` approval URL without logging the full token. `paypal_direct_zero_due`（PP直链-强制0元试用） uses the same PP直链 flow but keeps `require_zero_due=true`; if Stripe init does not return `amount_due=0`, generation fails with `checkout_not_zero_due` instead of outputting a non-trial BA link. In this strict mode a failed regeneration does not fall back to hosted long-link mode and does not reuse an older saved BA link.
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
- `upi.billing_regions`: UPI hosted long-link generation defaults to `["IN"]` and uses `checkout_ui_mode=hosted` + `link_mode=chatgpt_checkout` so the stored URL is the Stripe/OpenAI hosted checkout long link for India/INR. UPI is link-generation only; `--one-click-pay --payment-method upi` is intentionally not implemented.
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

6. Build and start the WPF app. The canonical executable output is `dist/net10/SmsWorkbench.exe`; the build script removes intermediate `SmsWorkbench/bin/Debug/net10.0-windows` and `SmsWorkbench/bin/Release/net10.0-windows` workspaces after publishing.

```powershell
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
.\dist\net10\SmsWorkbench.exe
```

7. Build release installers when publishing a Windows build. The installer script rebuilds the desktop app, packages only tracked project files plus the fresh `dist/net10` publish output, and writes assets under `dist/release/`. The generated setup executable is a graphical Windows installer using the app icon, and it lets users choose the install path; `/S /DIR=...` remains available for silent installs.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD
```

For internal distribution, build with a reusable self-signed Authenticode certificate and publish the exported `.cer` next to the installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1 -Version vYYYY.MM.DD -SelfSign
```

Internal users must import `GPT-Register-Tool-Internal-CodeSigning.cer` into both `Trusted Publishers` and `Trusted Root Certification Authorities` before running the installer, otherwise Windows will still treat the self-signed publisher as untrusted. The release also includes `trust_internal_certificate.ps1`, which imports the certificate into the current user certificate stores.

## Mailbox Inputs

Standard Microsoft Graph/OAuth pool:

```text
email---password---refresh_token---access_token---0
```

Chatai mailbox pool:

```text
email----password----client_id----refresh_token
```

The parser accepts UTF-8 with or without BOM. It also repairs the known malformed Chatai alias form:

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

Rebuild SQLite index from existing session JSON files:

```powershell
python chatgpt_phone_reg.py --rebuild-sqlite
```

List saved PayPal links:

```powershell
python chatgpt_phone_reg.py --list-paypal-links
```

Regenerate a PayPal/GoPay/UPI link for one account:

```powershell
python chatgpt_phone_reg.py --email user@example.com --regenerate-paypal-link
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

The PayPal one-click path now uses `sms_tool.paypal_browser_auto`, generates PayPal signup identity/address/card data inside this project, consumes one SMS endpoint from `paypal_browser.phone_pool` (falling back to `paypal_nocard.phone_pool`), then runs the project-local `sms_tool.paypal_auto` browser flow against the already saved SQLite/session `paypal_url`. It does not regenerate PayPal links; run `--regenerate-paypal-link` explicitly before one-click payment when an account has no saved link. The older pure HTTP no-card module remains in `sms_tool.paypal_nocard` but is no longer the default PayPal `--one-click-pay` path.

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

## WPF Behavior

`SmsWorkbench` is a launcher and management UI. It reads `config.json`, starts the Python CLI, displays mailbox/session/SQLite state, and exposes maintenance actions.

UI responsibilities are intentionally thin:

- The account list supports row selection plus checkbox-backed batch actions; double-clicking a row no longer opens details.
- Account details are opened from the explicit detail button.
- The inbox view uses an in-app mail detail popup and can copy recognized 5-8 digit verification codes.
- Marking payment complete updates PayPal status only. CPA import is a separate operation.
- The desktop UI uses a fixed gray-dominant minimalist dark theme; black is reserved for the sidebar, log console, and other low-emphasis surfaces.
- Desktop icons are generated from the same kitten mark: `SmsWorkbench/Assets/app-icon.ico` and `SmsWorkbench/Assets/black-kitten.png`.
- One-click payment is an explicit action. PayPal launches the project-local browser adapter against an already saved payment link; GoPay launches the provider/protocol workflow selected in `gopay.one_click_mode`; rows are marked `completed` only after the backend returns success.

PayPal link buttons open Google Chrome with:

```text
chrome.exe --new-window --incognito <paypal_url>
```

If Chrome is not installed in a standard location, the app falls back to the system default browser.

The account list deduplicates rows by normalized email. When a mailbox pool entry later gains SQLite/session status, the SQLite/session row is shown instead of a second duplicate mailbox row.

## Project Modules

The project is split into explicit responsibility seams:

- `chatgpt_phone_reg.py`: compatibility entrypoint that only delegates into `sms_tool.cli`.
- `sms_tool.cli`: argument parsing and command orchestration. Optional Codex, CPA, PayPal payment, and session-refresh modules are imported lazily only by the command that needs them.
- `sms_tool.mailbox`: mailbox pool parsing, LuckMail/token mailbox handling, Microsoft token exchange, and OTP polling.
- `sms_tool.registration`: ChatGPT signup protocol, email OTP validation, access-token retrieval, and batch worker limits.
- `sms_tool.account_seed`: shared seam for loading session JSON/SQLite account seed data and extracting access tokens.
- `sms_tool.gen_pp_link` / `sms_tool.paypal_links`: hosted Stripe/PayPal/GoPay/UPI link generation and safe persisted-link regeneration.
- `sms_tool.paypal_browser_auto`: default PayPal one-click payment adapter. It uses existing saved payment links and delegates page automation to `sms_tool.paypal_auto`.
- `sms_tool.paypal_auto`: project-local browser automation helper. It does not own account lookup or link regeneration.
- `sms_tool.paypal_nocard`: older explicit no-card PayPal agreement module, kept as an opt-in compatibility path. It is not selected by default registration or one-click PayPal.
- `sms_tool.gopay_payment`: GoPay payment entrypoint. It selects link/provider/WA-rebind mode and owns session/SQLite state updates.
- `sms_tool.gopay_wa_rebind`: WA-channel GoPay app auth and change-phone orchestration after a successful provider payment.
- `sms_tool.grpcurl_client`: shared boundary for optional local gRPC provider services.
- `services/gopay-flow`: project-local GoPay PaymentService and protocol implementation.
- `services/gopay-app/proto`: GoPay App gRPC protocol contract used by WA rebind mode.
- `services/gopay-adb`: local ADB HTTP sidecar for OTP notification polling and unlink actions.
- `services/mail-otp-web`: standalone Microsoft Graph inbox/OTP diagnostic UI. It does not own registration persistence.
- `sms_tool.codex_oauth`, `sms_tool.codex_export`, `sms_tool.cpa_import`: Codex OAuth/export and CPA upload boundaries.
- `sms_tool.storage`: SQLite schema, migrations, deduplication, status updates, and session-index rebuilds.
- `SmsWorkbench`: WPF launcher and management UI. It starts CLI commands and displays local state; protocol details stay in Python modules.

The same split is maintained in [docs/architecture.md](docs/architecture.md). Physical directory classification is maintained in [docs/directory-map.md](docs/directory-map.md).


## Cleanup and Ownership Rules

- The old `browser_extensions/paypal_autofill` extension and its tests were removed. PayPal browser payment now lives behind `sms_tool.paypal_browser_auto` and `sms_tool.paypal_auto`; do not add extension-side code back unless it becomes a separately documented adapter.
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
