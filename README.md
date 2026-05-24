# GPT-Register-Tool

Email-based ChatGPT registration workflow with session persistence and PayPal payment-link generation.

The active path is:

```text
mailbox source -> ChatGPT email OTP registration -> /api/auth/session access token
-> PayPal/Stripe hosted payment link -> session JSON + SQLite index -> WPF management UI
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
- `cpa_mode.api_url` / `cpa_mode.api_token`: CPA management API target for one-click import.
- `codex_oauth.allow_passwordless_takeover`: default `false`; only affects manual Codex export/refresh. CPA import now consumes existing AT-only JSON and no longer depends on RT refresh.
- `codex_oauth.auto_phone_verification`: default `false`; phone verification is attempted only when explicitly enabled.

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
- One-click PayPal payment is an explicit action. It launches the Python no-card agreement workflow for selected rows and marks a row `completed` only after the backend returns success.

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
