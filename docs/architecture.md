# Project Architecture and Boundaries

This document defines the responsibilities of each module so a fresh clone can be configured and run on any Windows machine without hardcoded local paths.

## Runtime Flow

```text
WPF or CLI
  -> mailbox source selection
  -> ChatGPT email registration
  -> auth session/access token fetch
  -> PayPal/Stripe hosted payment-link generation
  -> session JSON + SQLite index
  -> status display and maintenance actions
```

## Repository Layout

```text
chatgpt_phone_reg.py        Compatibility entrypoint; delegates to sms_tool.cli.
config.example.json         Portable config template. Copy to config.json locally.
README.md                   Setup and operations guide.

sms_tool/
  cli.py                    CLI parsing, high-level orchestration, process exit codes.
  config.py                 Config loading only.
  paths.py                  Project-relative path resolution.
  mailbox.py                Mailbox pool parsing and OTP retrieval.
  providers/                External provider clients.
  http_client.py            curl_cffi retry/transport handling.
  registration.py           ChatGPT registration protocol and batch worker control.
  gen_pp_link.py            PayPal/Stripe hosted payment-link generation.
  paypal_links.py           Regenerate PayPal links without clobbering old links.
  session_refresh.py        Refresh auth session after manual login/payment.
  codex_export.py           Build Codex/CPA-compatible token JSON from session data.
  codex_oauth.py            Codex OAuth authorization-code + PKCE login orchestration.
  codex_sentinel.py         Sentinel/cache cookie helpers for auth.openai.com requests.
  codex_phone.py            Optional add-phone SMS verification boundary.
  cpa_import.py             CPA API upload boundary; imports AT-only JSON and uploads normalized CPA payloads.
  storage.py                SQLite and session index persistence.

SmsWorkbench/               WPF desktop UI.
browser_extensions/         Optional Chrome checkout helpers.
  paypal_autofill/          Popup, content script, and background fetch boundary with one-click fill, OTP polling, and pool rotation.
tests/                      Unit tests for non-network behavior.
sessions/                   Generated session JSON, ignored by Git.
runtime/                    SQLite, debug output, caches, ignored by Git.
requirements.txt            Primary Python dependency list.
requirement.txt             Compatibility alias for tooling that expects the singular filename.
```

## Boundary Rules

### WPF UI

`SmsWorkbench/MainWindow.xaml.cs` may:

- Read `config.json`.
- Create temporary mailbox selection files.
- Start `chatgpt_phone_reg.py`.
- Display SQLite/session/mailbox state.
- Open PayPal links in Chrome incognito.
- Render custom account and inbox popups.
- Copy verification codes from already-fetched mailbox previews.

It must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or direct SQLite business rules beyond display and deletion.

Payment and CPA operations stay separated in the UI: marking payment complete only updates PayPal status, while CPA import is launched by the explicit CPA action.

### CLI

`sms_tool/cli.py` is the orchestration boundary. It may:

- Parse arguments.
- Load mailbox sources.
- Choose single vs batch registration.
- Persist results through `storage.py`.
- Return meaningful exit codes.

It must not silently replace an explicit empty mailbox file with a new provider purchase. If the user passed a mailbox file and no mailbox was parsed, it exits with code `2`.

### Mailbox Layer

`sms_tool/mailbox.py` owns:

- Chatai file parsing.
- Standard OAuth mailbox file parsing.
- LuckMail purchase/token mailbox handling.
- Microsoft refresh-token exchange.
- OTP polling.
- Email normalization for mailbox inputs.

It must not write registration results or modify mailbox pool files during registration.

### Registration Layer

`sms_tool/registration.py` owns:

- Sentinel token extraction/cache usage.
- ChatGPT auth/signup flow.
- OTP validation.
- Auth session access-token retrieval.
- Batch worker limits.

Batch registration uses each loaded mailbox at most once. If `--count` exceeds loaded unique mailboxes, the batch is capped instead of wrapping with modulo and reusing a mailbox concurrently.

### PayPal Link Layer

`sms_tool/gen_pp_link.py` only generates the hosted Stripe/PayPal redirect URL from an access token. It does not perform PayPal account signup, card entry, SMS verification, or final payment authorization.

`paypal.billing_regions` controls checkout billing country/currency, and `paypal.stage_proxies` can route stages independently:

```json
{
  "billing_regions": ["US"],
  "stage_proxies": {
    "checkout": "socks5h://127.0.0.1:7897",
    "stripe_init": "socks5h://127.0.0.1:7897",
    "payment_method": "socks5h://127.0.0.1:7897",
    "confirm": "direct"
  }
}
```

`paypal.billing_regions` controls the Checkout billing country/currency, not the proxy exit. The original PayPal-capable flow uses `["US"]`; Japan/JPY checkout may return only `["card"]` as the available Stripe payment method. When the UI or CLI supplies `--proxy`, regeneration treats that value as authoritative for the current run.

### Storage Layer

`sms_tool/storage.py` owns:

- SQLite schema creation and migrations.
- Case-insensitive account deduplication.
- Email normalization before upsert.
- PayPal status and refresh-token status persistence.
- Rebuilding SQLite from `sessions/session_*.json`.

`accounts.email` is treated as a normalized logical key. Updates should modify an existing row for the same email instead of creating a new row with different casing or a repaired alias spelling.

### Codex OAuth and CPA Layer

`sms_tool/codex_oauth.py` owns only the Codex OAuth authorization-code + PKCE sequence:

- Build the OAuth authorize URL.
- Reuse existing auth cookies when they already produce a callback code.
- Continue username login.
- Complete email OTP when OpenAI routes the flow to an email OTP page or when takeover is explicitly enabled.
- Exchange the callback code for OpenAI `access_token`, `id_token`, and `refresh_token`.

It deliberately does not upload to CPA and does not own phone-number inventory.

`sms_tool/codex_sentinel.py` owns auth.openai.com sentinel cookie/header helpers. Cached Cloudflare/auth cookies may be reused, but the cached `oai-did` is stripped before import so one global browser fingerprint is not assigned to every account.

`sms_tool/codex_phone.py` owns add-phone completion. It is disabled by default. If OpenAI requests `/add-phone`, the OAuth layer reports `add_phone_required` unless `codex_oauth.auto_phone_verification` is true.

`sms_tool/codex_export.py` converts session JSON into the compact Codex JSON shape. `sms_tool/cpa_import.py` accepts existing AT-only session JSON, normalizes it into the CPA payload shape, and uploads it without requiring RT.

Important behavior:

- `codex_oauth.allow_passwordless_takeover=true` is an explicit escape hatch for manual export/refresh paths.
- Forced email OTP may still require add-phone for some accounts. Phone SMS handling remains a separate opt-in boundary via `codex_oauth.auto_phone_verification`.

## Portable Configuration

All paths in `config.example.json` are relative by default:

```json
{
  "email_registration": {
    "token_file": "mailbox_tokens.txt"
  },
  "runtime": {
    "directory": "runtime"
  },
  "storage": {
    "sqlite_path": "runtime/accounts.sqlite3"
  },
  "codex_oauth": {
    "allow_passwordless_takeover": false,
    "auto_phone_verification": false
  },
  "output": {
    "directory": "sessions"
  }
}
```

Relative paths are resolved from the repository root via `sms_tool/paths.py` or WPF `rootDir` detection. A user may still use absolute paths in local `config.json`, but committed config templates and docs should not depend on one developer's machine.

## Status and Dedup Semantics

The WPF list may load the same logical account from:

- mailbox pool text file,
- SQLite,
- session JSON fallback.

Rows are deduplicated by normalized email for display. SQLite/session rows have higher priority than mailbox-pool rows because they represent updated registration/payment state.

## Exit Codes

```text
0  command completed normally
2  explicit mailbox source was empty or malformed
3  registration succeeded but PayPal link generation failed
```

## Local Files That Must Stay Out of Git

```text
config.json
sms_tool/config.json
mailbox_tokens.txt
sessions/
runtime/
dist/
.dotnet/
```
