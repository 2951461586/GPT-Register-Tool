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

3. Create local config.

```powershell
copy config.example.json config.json
```

4. Edit `config.json`.

Required choices:

- `proxy.default`: local HTTP/SOCKS proxy, or `direct`.
- `email_registration.token_file`: relative mailbox pool path such as `mailbox_tokens.txt`, or leave empty and use LuckMail.
- `email_registration.luckmail_api_key`: required only for LuckMail purchase/token flows.
- `paypal.stage_proxies`: optional stage-specific routing for PayPal link generation.
- `cpa_mode.api_url` / `cpa_mode.api_token`: CPA management API target for one-click import.
- `codex_oauth.allow_passwordless_takeover`: default `false`; only affects manual Codex export/refresh. CPA import forces email OTP login for password-page accounts.
- `codex_oauth.auto_phone_verification`: default `false`; phone verification is attempted only when explicitly enabled.

5. Run one registration.

```powershell
python chatgpt_phone_reg.py --count 1
```

6. Start the WPF app.

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

Mark a paid account and export Codex JSON:

```powershell
python chatgpt_phone_reg.py --email user@example.com --mark-paypal-status completed --export-codex-json
```

Import paid accounts into CPA:

```powershell
python chatgpt_phone_reg.py --import-cpa --email-file paid_emails.txt
```

CPA import requires a real OpenAI OAuth `refresh_token` beginning with `rt_` and a real `id_token`.
For `--import-cpa`, if OpenAI routes an account to `/log-in/password`, the tool still uses the
email OTP login path and does not submit the account password. If OpenAI later requests `/add-phone`,
the default flow reports `add_phone_required`; phone SMS handling is attempted only when
`codex_oauth.auto_phone_verification` is explicitly enabled.

## WPF Behavior

`SmsWorkbench` is a launcher and management UI. It reads `config.json`, starts the Python CLI, displays mailbox/session/SQLite state, and exposes maintenance actions.

PayPal link buttons open Google Chrome with:

```text
chrome.exe --new-window --incognito <paypal_url>
```

If Chrome is not installed in a standard location, the app falls back to the system default browser.

The account list deduplicates rows by normalized email. When a mailbox pool entry later gains SQLite/session status, the SQLite/session row is shown instead of a second duplicate mailbox row.

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
