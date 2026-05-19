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
  storage.py                SQLite and session index persistence.

SmsWorkbench/               WPF desktop UI.
tests/                      Unit tests for non-network behavior.
sessions/                   Generated session JSON, ignored by Git.
runtime/                    SQLite, debug output, caches, ignored by Git.
```

## Boundary Rules

### WPF UI

`SmsWorkbench/MainWindow.xaml.cs` may:

- Read `config.json`.
- Create temporary mailbox selection files.
- Start `chatgpt_phone_reg.py`.
- Display SQLite/session/mailbox state.
- Open PayPal links in Chrome incognito.

It must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or direct SQLite business rules beyond display and deletion.

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

`paypal.stage_proxies` can route stages independently:

```json
{
  "checkout": "socks5h://127.0.0.1:7897",
  "stripe_init": "socks5h://127.0.0.1:7897",
  "payment_method": "socks5h://127.0.0.1:7897",
  "confirm": "direct"
}
```

### Storage Layer

`sms_tool/storage.py` owns:

- SQLite schema creation and migrations.
- Case-insensitive account deduplication.
- Email normalization before upsert.
- PayPal status and refresh-token status persistence.
- Rebuilding SQLite from `sessions/session_*.json`.

`accounts.email` is treated as a normalized logical key. Updates should modify an existing row for the same email instead of creating a new row with different casing or a repaired alias spelling.

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
