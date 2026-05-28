# GPT-Register-Tool

Email-based ChatGPT registration workflow with session persistence and PayPal/GoPay payment automation.

The active path is:

```text
mailbox source -> ChatGPT email OTP registration -> /api/auth/session access token
-> PayPal/GoPay payment link or protocol payment -> session JSON + SQLite index -> WPF management UI
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
- `paypal.billing_regions`: Checkout billing country/currency order. Use `["US"]` for the original PayPal-capable flow; this is independent from the proxy exit.
- `paypal.stage_proxies`: optional stage-specific routing for PayPal link generation.
- `--regenerate-paypal-link --proxy ...`: forces PayPal/Stripe link regeneration through the selected proxy and overrides stage proxy routing for that run.
- `paypal_auto.cards` / `paypal_nocard.phone_pool`: card and SMS-phone pools used only by explicit PayPal no-card payment commands.
- `gopay.one_click_mode`: `link`, `provider`, or `wa_rebind`. `provider` uses the local `PaymentService` on `gopay.payment_service_addr`; `wa_rebind` additionally routes GoPay payment OTP through the WA channel and can call a GoPay App service to change phone after payment.
- `gopay.payment_service_addr`: local GoPay payment gRPC endpoint, default `127.0.0.1:50051`.
- `gopay.wa_rebind`: optional WA-channel app-state/rebind settings. `gopay_app_service_addr` points to the GoPay App gRPC provider, `wa_phone` is the WA payment phone, and `rebind_phone` is the phone to bind after payment.
- `cpa_mode.api_url` / `cpa_mode.api_token`: CPA management API target for one-click import.
- `codex_oauth.allow_passwordless_takeover`: default `false`; only affects manual Codex export/refresh. CPA import now consumes existing AT-only JSON and no longer depends on RT refresh.
- `codex_oauth.require_registration_refresh_token`: default `true`; a new registration is not counted as successful until Codex OAuth returns a refresh token.
- `codex_oauth.require_registration_phone_verification`: default `true`; when a phone pool is configured, registration must complete SMS verification before the session is saved.
- `--registration-at-only`: UI default for "one-click registration + payment link"; skips Codex OAuth/phone SMS and stores the ChatGPT access token only.
- `--one-click-sms`: runs Codex OAuth for selected existing accounts, completes phone SMS verification via the phone pool, and stores the OAuth refresh token.
- `phone_reuse.smsbower`: SMSBower OpenAI/Ghana (`service=dr`, `country=38`, `+233`) phone pool. One acquired activation is reused up to `phone_reuse.max_reuse_count` times, default `3`. For single-phone batch registration, the phone verification and OAuth token exchange run in one serialized lane; use `phone_reuse.send_cooldown_seconds` or `--phone-send-cooldown` to slow repeated add-phone sends to the same number. `phone_reuse.send_retry_attempts` handles recoverable add-phone rate limits without immediately canceling the SMSBower activation.

5. Run one registration.

```powershell
python chatgpt_phone_reg.py --count 1
```

6. Build and start the WPF app. The canonical executable output is `dist/net10/SmsWorkbench.exe`; the build script removes intermediate `SmsWorkbench/bin/Debug/net10.0-windows` and `SmsWorkbench/bin/Release/net10.0-windows` workspaces after publishing.

```powershell
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
.\dist\net10\SmsWorkbench.exe
```

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

Regenerate a PayPal link for one account:

```powershell
python chatgpt_phone_reg.py --email user@example.com --regenerate-paypal-link
```

Refresh an auth session after manual payment/login:

```powershell
python chatgpt_phone_reg.py --email user@example.com --refresh-session
```

Mark a paid account as paid:

```powershell
python chatgpt_phone_reg.py --email user@example.com --mark-paypal-status completed
```

Run PayPal no-card agreement payment for an existing account with a saved payment link:

```powershell
python chatgpt_phone_reg.py --email user@example.com --one-click-pay --proxy socks5h://127.0.0.1:7897
```

Batch mode accepts one email per line:

```powershell
python chatgpt_phone_reg.py --one-click-pay --email-file pending_emails.txt --workers 4 --proxy socks5h://127.0.0.1:7897
```

The no-card payment path uses the existing SQLite/session `access_token`, resolves the BA token immediately, then consumes one card from `paypal_auto.cards` and one SMS endpoint from `paypal_nocard.phone_pool`. `--regenerate-paypal-link` now follows the Stripe redirect immediately and stores the resolved PayPal approve URL when a BA token is available. A just-regenerated SQLite/session `paypal_url` is reused when `paypal_status=link_ready` and `paypal_updated_at` is within `paypal_nocard.saved_url_max_age_seconds` (default 1800). Older saved URLs are regenerated by default. `paypal_nocard.reuse_saved_url=true` forces saved URL reuse, and `paypal_nocard.fallback_to_saved_url=true` allows saved URL fallback when regeneration fails. The pure HTTP PayPal signup path uses `paypal_nocard.impersonate` (default `chrome136`) for curl-cffi TLS/browser fingerprinting. It is not part of the default registration command and should be run only when those pools are intentionally configured.

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
`service=dr,country=38` values for GoPay.

```json
{
  "gopay": {
    "one_click_mode": "protocol",
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
        "claim_envelope_on_low_balance": true,
        "envelope_url": "https://app.gopay.co.id/NF8p/ajgvlrts",
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
8. For `otp_source=smsbower`, acquire a GoPay phone number from SMSBower, register/init the GoPay wallet, set PIN, then require `/v1/payment-options/balances` to be at least `min_balance_rp` before checkout. If balance is too low and `claim_envelope_on_low_balance=true`, resolve `envelope_url`, claim the GoPay red packet through `/v1/festivals/envelope-requests`, then re-check balance. Otherwise use configured `gopay.phone`.
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
- Desktop and browser-extension icons are generated from the same kitten mark: `SmsWorkbench/Assets/app-icon.ico`, `SmsWorkbench/Assets/black-kitten.png`, and `browser_extensions/paypal_autofill/icons/`.
- The browser helper ships a compact popup with one-click fill, OTP polling, and card/phone pool rotation.
- One-click payment is an explicit action. PayPal launches the Python no-card agreement workflow; GoPay launches the provider workflow selected in `gopay.one_click_mode`; rows are marked `completed` only after the backend returns success.

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
- `sms_tool.gen_pp_link` / `sms_tool.paypal_links`: hosted Stripe/PayPal link generation and safe persisted-link regeneration.
- `sms_tool.paypal_nocard`: explicit no-card PayPal agreement payment flow. It is not part of default registration.
- `sms_tool.gopay_payment`: GoPay payment entrypoint. It selects link/provider/WA-rebind mode and owns session/SQLite state updates.
- `sms_tool.gopay_wa_rebind`: WA-channel GoPay app auth and change-phone orchestration after a successful provider payment.
- `sms_tool.grpcurl_client`: shared boundary for optional local gRPC provider services.
- `services/gopay-flow`: project-local GoPay PaymentService and protocol implementation.
- `services/gopay-app/proto`: GoPay App gRPC protocol contract used by WA rebind mode.
- `services/gopay-adb`: local ADB HTTP sidecar for OTP notification polling and unlink actions.
- `sms_tool.codex_oauth`, `sms_tool.codex_export`, `sms_tool.cpa_import`: Codex OAuth/export and CPA upload boundaries.
- `sms_tool.storage`: SQLite schema, migrations, deduplication, status updates, and session-index rebuilds.
- `SmsWorkbench`: WPF launcher and management UI. It starts CLI commands and displays local state; protocol details stay in Python modules.
- `browser_extensions/paypal_autofill`: optional Chrome helper for checkout form filling, OTP polling, and pool rotation.

The same split is maintained in [docs/architecture.md](docs/architecture.md).

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
- `mailbox_tokens.txt`
- `sessions/`
- `runtime/`
- `dist/`
- `.dotnet/`

Do not commit tokens, mailbox refresh tokens, access tokens, cookies, card data, or generated session files.

## Module Boundaries

See [docs/architecture.md](docs/architecture.md) for the responsibility split between UI, CLI orchestration, mailbox providers, registration protocol, PayPal link generation, session refresh, and storage.
