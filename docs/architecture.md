# Project Architecture and Boundaries

This document defines the responsibilities of each module so a fresh clone can be configured and run on any Windows machine without hardcoded local paths. For the physical repository classification, see [`directory-map.md`](directory-map.md).

## Runtime Flow

```text
WPF or CLI
  -> mailbox source selection
  -> ChatGPT email registration
  -> auth session/access token fetch
  -> PayPal/GoPay payment-link generation or explicit protocol payment
  -> session JSON + SQLite index
  -> status display and maintenance actions
```

## Repository Layout

```text
chatgpt_phone_reg.py        Compatibility entrypoint; delegates to sms_tool.cli.
config.example.json         Portable config template. Copy to config.json locally.
README.md                   Setup and operations guide.
requirements.txt            Only Python dependency manifest.

sms_tool/
  __main__.py               `python -m sms_tool` entrypoint; no import-time side effects.
  cli.py                    CLI parsing, high-level orchestration, process exit codes.
  config.py                 Config loading only.
  paths.py                  Project-relative path resolution.
  account_seed.py           Shared account/session seed lookup and access-token extraction.
  mailbox.py                Mailbox pool parsing and OTP retrieval.
  providers/                External provider clients.
  http_client.py            curl_cffi retry/transport handling.
  registration.py           ChatGPT registration protocol and batch worker control.
  gen_pp_link.py            PayPal/Stripe hosted payment-link generation.
  paypal_links.py           Regenerate PayPal links without clobbering old links.
  paypal_browser_auto.py    Default PayPal one-click adapter using saved links.
  paypal_auto.py            Project-local PayPal browser page automation helper.
  paypal_nocard.py          Legacy explicit PayPal no-card agreement flow.
  grpcurl_client.py         Shared grpcurl subprocess boundary.
  gopay_payment.py          GoPay link/provider/WA-rebind payment entrypoint.
  gopay_wa_rebind.py        WA-channel GoPay app auth and change-phone orchestration.
  session_refresh.py        Refresh auth session after manual login/payment.
  codex_export.py           Build Codex/CPA-compatible token JSON from session data.
  codex_oauth.py            Codex OAuth authorization-code + PKCE login orchestration.
  codex_sentinel.py         Sentinel/cache cookie helpers for auth.openai.com requests.
  codex_phone.py            Optional add-phone SMS verification boundary.
  cpa_import.py             CPA API upload boundary; imports AT-only JSON and uploads normalized CPA payloads.
  storage.py                SQLite and session index persistence.

SmsWorkbench/               WPF desktop UI.
services/
  gopay-flow/               Project-local GoPay PaymentService wrapper and protocol.
  gopay-app/proto/          GoPay App gRPC contract used by WA rebind mode.
  gopay-adb/                ADB HTTP sidecar for OTP notification polling and unlink.
  mail-otp-web/             Standalone Microsoft Graph inbox/OTP diagnostic UI.
tests/                      Offline unit tests; see tests/README.md.
sessions/                   Generated session JSON, ignored by Git.
runtime/                    SQLite, debug output, caches, ignored by Git.
```

## Ownership Matrix

| Feature surface | Owning module | May call | Must not own |
| --- | --- | --- | --- |
| Desktop buttons/dialogs | `SmsWorkbench/` | `chatgpt_phone_reg.py`, SQLite/session read-only display helpers | ChatGPT protocol, payment protocol, mailbox polling loops |
| CLI command routing | `sms_tool.cli` | Focused command modules | Provider protocol internals or long-lived state mutation outside handlers |
| Mailbox parsing/polling | `sms_tool.mailbox`, `sms_tool.providers/*` | Microsoft Graph, mailbox provider clients | Registration success persistence, payment state |
| Phone inventory | `sms_tool.phone_reuse`, `sms_tool.smsbower`, `sms_tool.nextsms` | SMS provider APIs | ChatGPT account state, payment state |
| ChatGPT registration | `sms_tool.registration` | mailbox/phone seams, storage through result writers | Payment execution, CPA upload |
| Auth/session refresh | `sms_tool.codex_oauth`, `sms_tool.session_refresh` | mailbox OTP seam, phone seam when explicitly enabled | Phone inventory purchasing outside configured provider seam |
| Payment link generation | `sms_tool.gen_pp_link`, `sms_tool.paypal_links` | account seed, ChatGPT checkout, Stripe init | PayPal account signup, final payment authorization |
| Payment execution | `sms_tool.paypal_browser_auto`, `sms_tool.paypal_auto`, `sms_tool.gopay_payment` | account seed, saved payment links, provider services | Registration, mailbox pool edits, link regeneration as a side effect |
| Account persistence | `sms_tool.storage` | session JSON and SQLite | Vendor protocol calls |
| Local helper services | `services/*` | Their own provider/runtime APIs | Direct account SQLite writes unless routed through CLI contracts |

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

`SmsWorkbench/App.xaml` owns the fixed white-first minimalist visual system for the desktop app, with black and gray used for text, borders, navigation, and log surfaces. App icon assets live under `SmsWorkbench/Assets/`.

`SmsWorkbench/build_dotnet.ps1` publishes the only supported runnable desktop artifact to `dist/net10/SmsWorkbench.exe` and calls `SmsWorkbench/clean_dotnet_workspaces.ps1` after publish so `SmsWorkbench/bin/Debug/net10.0-windows`, `SmsWorkbench/bin/Release/net10.0-windows`, and nested runtime folders such as `win-x64` are not treated as second app distribution directories.

### CLI

`sms_tool/cli.py` is the orchestration boundary. It may:

- Parse arguments.
- Load mailbox sources.
- Choose single vs batch registration.
- Persist results through `storage.py`.
- Return meaningful exit codes.

It must not silently replace an explicit empty mailbox file with a new provider purchase. If the user passed a mailbox file and no mailbox was parsed, it exits with code `2`.

Optional command modules are lazy seams. Codex export, CPA import, PayPal/GoPay payment, PayPal link regeneration, and session refresh modules are imported only inside the command handler that needs them. Importing `sms_tool.cli` or `sms_tool.__main__` must not start a command or import optional payment/browser dependencies as a side effect.

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


### Account Seed Layer

`sms_tool.account_seed` owns the shared lookup of account/session seed data. It may:

- Load an explicit `session_*.json` file.
- Load the SQLite account row for an email.
- Merge persisted raw JSON with the session file.
- Expose normalized `email`, `access_token`, `cookie_header`, and refresh-token fields.
- Extract a ChatGPT access token from flat or `auth_session` shaped data.

Payment adapters may call this seam, but must not duplicate SQLite/session merging logic or import private helpers from each other. This keeps PayPal link regeneration, PayPal browser payment, and legacy PayPal automation independent from one another.

### PayPal Link Layer

`sms_tool/gen_pp_link.py` only generates the hosted Stripe/PayPal redirect URL from an access token. It does not perform PayPal account signup, card entry, SMS verification, or final payment authorization.

`paypal.billing_regions` controls checkout billing country/currency, and `paypal.stage_proxies` can route stages independently:

```json
{
  "billing_regions": ["DE"],
  "stage_proxies": {
    "checkout": "socks5h://127.0.0.1:7897",
    "stripe_init": "socks5h://127.0.0.1:7897",
    "payment_method": "socks5h://127.0.0.1:7897",
    "confirm": "direct"
  }
}
```

`paypal.billing_regions` controls the Checkout billing country/currency, not the proxy exit. The current PayPal regeneration path follows the standalone long-link script logic with `paypal.link_mode=chatgpt_checkout` and `paypal.checkout_ui_mode=hosted`: it posts ChatGPT checkout for the configured billing region, calls Stripe `/v1/payment_pages/{cs_id}/init`, reads `stripe_hosted_url`, and stores the resulting hosted long URL (`checkout.stripe.com/c/pay/...` normalized to `pay.openai.com/c/pay/...`). It deliberately does not enter Stripe payment-method creation, confirm, or ChatGPT checkout approve, so it avoids the BA-specific `confirm returned no redirect` / `approve blocked` path. `paypal.resolve_ba_redirect=false` and `paypal.require_ba_token=false` are expected in this mode. With `paypal.explicit_proxy_overrides_stage_proxies=false`, a UI/CLI `--proxy` is used as the default candidate proxy but does not override stage-specific routing. Batch regeneration is intentionally conservative: `paypal.max_regenerate_workers` defaults to `1`, and `paypal.regenerate_delay_seconds` staggers accounts so a UI request with `--workers 4` does not fan out four simultaneous checkout creations and trigger `429`. With `paypal.require_zero_due=true`, non-zero checkout totals fail immediately.

### Payment Responsibility Boundary

Payment is split into three independent responsibilities:

1. **Create checkout/link**: `sms_tool.gen_pp_link` and `sms_tool.paypal_links`.
   They read an access token and return/store a hosted checkout URL or explicit
   failure details. They do not complete payment.
2. **Execute an explicit payment command**: `sms_tool.paypal_browser_auto`,
   `sms_tool.paypal_auto`, and `sms_tool.gopay_payment`. They only run when the
   user requests `--one-click-pay` or a matching UI action. They use existing
   account seed data and payment links rather than registering accounts.
3. **Persist/display payment state**: `sms_tool.storage` and `SmsWorkbench`.
   Storage normalizes status fields; the UI displays and launches commands. The
   UI must not infer success from a URL alone.

Registration, mailbox refresh, CPA import, account scan, SQLite rebuild, and
session refresh must not implicitly run payment execution. Link regeneration may
update `paypal_url` only through the payment-link seam, and failed regeneration
must preserve useful existing URLs unless the caller explicitly clears them.

### PayPal Payment Layer

`sms_tool/paypal_browser_auto.py` is the default PayPal boundary for `--one-click-pay`. It uses `sms_tool.account_seed` for account/session seed loading, generates payment persona data locally, and delegates browser page automation to the project-local `sms_tool.paypal_auto` module. It may:

- Use the existing SQLite/session `paypal_url` directly.
- Refuse PayPal browser payment when no saved `paypal_url` exists.
- Leave link creation to the explicit `--regenerate-paypal-link` command.
- Run the configured project-local browser engine from `paypal_browser.browser_engine`, defaulting to Camoufox.
- Detect PayPal human-verification pages and either fail with `paypal_human_verification_required` or wait for manual completion when visible-browser manual verification is enabled.
- Generate random PayPal signup identity, billing address, and card data inside this repository.
- Consume one configured phone/SMS endpoint from `paypal_browser.phone_pool`, falling back to `paypal_nocard.phone_pool` for compatibility.
- Store only redacted browser-payment metadata such as alias email, card last4, phone last4, callback URL, country, and engine.
- Mark the account `completed` only after the backend reports success.

`sms_tool/paypal_auto.py` owns browser page mechanics only: form filling, PayPal challenge detection, SMS polling hooks, and browser-engine fallback. It must not regenerate links, select accounts, or persist SQLite rows directly except through the result passed back to the adapter.

`sms_tool/paypal_nocard.py` remains available as the older explicit no-card agreement implementation, but the CLI no longer selects it for PayPal one-click payment. PayPal browser automation must not run as an implicit side effect of registration, SQLite rebuild, link regeneration, or CPA import. Automated tests for this layer are offline by default.

### GoPay Link And Provider Layer

`sms_tool.gopay_payment` is the only GoPay payment entrypoint used by `--one-click-pay --payment-method gopay`. It may:

- Generate a hosted GoPay link through `sms_tool.gen_pp_link` when `gopay.one_click_mode=link`.
- Call a local `payment.PaymentService` through `sms_tool.grpcurl_client` when `gopay.one_click_mode=provider`.
- Switch to WA-channel provider payment when `gopay.one_click_mode=wa_rebind`.
- Persist GoPay status into the existing `paypal_*` SQLite/session columns for UI compatibility, while recording richer GoPay details under `paypal` and `gopay_wa_rebind`.

It must not implement ChatGPT/Midtrans protocol details inline. SMSBower GoPay account bootstrap is owned by `services/gopay-flow`: it uses the project-local Python pure-protocol client for GoPay Android signup/login/PIN setup, not `services/gopay-app` gRPC and not the old `gopay-deploy` `opai` client.

`sms_tool.grpcurl_client` owns grpcurl process execution and proto path resolution only. Payment modules pass method names, request bodies, service names, and provider configuration into it.

### GoPay WA Rebind Layer

`sms_tool.gopay_wa_rebind` adapts the byte-v-forge WA flow to this project without importing its Temporal/orchestrator stack. It may:

- Resolve the WA payment phone from CLI/config.
- Use `GopayAppService.GetGoPayState` and `UpsertGoPayState` for app state.
- Start or complete WA login with `AuthStart/AuthComplete`.
- Start or complete post-payment phone change with `ChangePhoneStart/ChangePhoneComplete`.
- Return explicit pending states when required OTPs are missing.

It must not acquire SMS numbers, poll third-party SMS providers, or run background workflows. In this project, OTPs are explicit CLI/UI inputs or are read by a separately configured sidecar. This keeps the current desktop tool deterministic and avoids silently running the upstream orchestrator's long-lived workflow model.

### Local Provider Services

`services/gopay-flow` is the project-local PaymentService. It owns the ChatGPT checkout, Stripe/Midtrans GoPay linking, OTP handoff, PIN charge, ChatGPT verify, optional unlink trigger, and SMSBower GoPay signup/bootstrap. Its SMSBower bootstrap path imports `gopay_pure_protocol.py` directly and must not call `GopayAppService` or import `opai.core.gojek_client`.

`services/gopay-app/proto` stores the GoPay App service contract used by WA rebind mode. The app-service implementation is a provider boundary: it may be supplied by a local project service or compatible binary, but callers inside `sms_tool` only depend on the proto-level RPC surface.

`services/gopay-adb` owns emulator/ADB HTTP endpoints such as `/health`, `/otp`, `/otp/clear`, and `/gopay/unlink`. It must not know about ChatGPT accounts, SQLite rows, or CPA import.

`services/mail-otp-web` is a standalone operator diagnostic surface for Microsoft Graph inbox/OTP extraction. It accepts the same mailbox account-line formats as `sms_tool.mailbox`, refreshes Microsoft access tokens, displays recent messages, and may return a rotated mailbox refresh token to the operator. It is not the main registration mailbox owner: registration still uses `sms_tool.mailbox`, and this helper service must not edit `hotmail.txt`, session JSON, or SQLite rows directly.


### Removed / Deprecated Surfaces

- `browser_extensions/paypal_autofill/` is retired. The maintained PayPal browser path is the project-local Python adapter.
- `tests/test_paypal_autofill_*.py` are retired with that extension; PayPal browser coverage lives in `tests/test_paypal_browser_auto.py`.
- Runtime debug artifacts and `__pycache__` folders are not source surfaces and should be deleted or ignored.

### Test Layer

`tests/` is the only test directory. Tests should stay offline by default and target module seams rather than live vendor systems.

Run all tests with:

```powershell
python -m unittest discover -s tests
```

### Storage Layer

`sms_tool/storage.py` owns:

- SQLite schema creation and migrations.
- Case-insensitive account deduplication.
- Email normalization before upsert.
- PayPal status and refresh-token status persistence.
- Payment method persistence for GoPay/PayPal compatibility.
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
services/mail-otp-web/config.json
mailbox_tokens.txt
sessions/
runtime/
dist/
.dotnet/
```
