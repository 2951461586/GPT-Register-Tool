# Project Architecture and Boundaries

This document defines the responsibilities of each module so a fresh clone can be configured and run on any Windows machine without hardcoded local paths. For the physical repository classification, see [`directory-map.md`](directory-map.md).

## Runtime Flow

```text
WPF or CLI
  -> mailbox source selection
  -> ChatGPT email registration
  -> auth session/access token fetch and stable HTTP-200 AT persistence boundary
  -> JIT AT probe/OAuth refresh when payment starts
  -> unified PayPal/GoPay/UPI/iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo link extraction
  -> session JSON + SQLite index
  -> status display and maintenance actions
```

## Repository Layout

```text
chatgpt_phone_reg.py        Compatibility entrypoint; delegates to sms_tool.cli.
config.example.json         Portable config template. Copy to config.json locally.
README.md                   Setup and operations guide.
requirements.txt            Only Python dependency manifest.
start_proxy_pool.py         Standalone SOCKS5 proxy-pool server entrypoint.
verify_proxy.py             Standalone proxy configuration verification utility.
ppgateway.exe               Optional payment gateway binary (included in release packages if present).

sms_tool/
  __main__.py               `python -m sms_tool` entrypoint; no import-time side effects.
  cli.py                    CLI parsing, high-level orchestration, process exit codes.
  config.py                 Config loading only.
  paths.py                  Project-relative path resolution.
  account_seed.py           Shared account/session seed lookup and access-token extraction.
  mailbox.py                Mailbox provider routing and OTP retrieval compatibility seam.
  mailbox_types.py          Shared mailbox dataclass and type definitions.
  mailbox_parsers.py        Mailbox import format parsing.
  mailbox_remail.py         ReMail order, pickup, adaptive OTP polling, and message normalization.
  mailbox_cfworker.py       CFWorker domain mailbox creation/fetch/OTP polling.
  mailbox_graph.py          Microsoft OAuth refresh boundary.
  mailbox_gmail.py          Gmail IMAP receive + SMTP send adapter.
  mailbox_chongzhi.py       Chongzhi mailbox provider adapter.
  outlook_imap.py           Outlook IMAP fallback fetcher.
  mail_otp.py               Shared OTP extraction/candidate filtering.
  providers/                External provider clients.
    cfworker_mailbox.py     CFWorker mailbox provider implementation.
  commands/                 CLI subcommand helpers.
    helpers.py              Shared command-level utilities.
  http_client.py            curl_cffi retry/transport handling.
  registration.py           ChatGPT registration orchestration compatibility seam.
  registration_progress.py  Registration stage progress tracking and persistence.
  auth_flow.py              OpenAI signin/authorize/continue helpers.
  auth_headers.py           Auth header construction and normalization.
  account_creation.py       Account creation, auth-session fetch, and payment-link helper calls.
  batch_runner.py           Batch registration concurrency and result ordering.
  sentinel_tokens.py        Sentinel token extraction/cache/browser fallback.
  sentinel_quickjs.py       QuickJS SDK path and PoW fallback for Sentinel.
  otp_strategy.py           Registration OTP send/resend strategy.
  auth_state.py             client_auth_session_dump diagnostics.
  error_classification.py   Error type classification and normalization for retry/reporting.
  paypal_protocol.py        Shared PayPal protocol helpers (BA token extraction, Stripe redirect follower).
  paypal_proxy.py           PayPal stage proxy resolution and region rotation.
  paypal_reverse.py         PayPal reverse-engineering helpers for link extraction.
  k12_client.py             Workspace request/accept/leave HTTP adapter (used by workspace_scan).
  k12_identity.py           Account/user/token identity extraction (used by workspace_scan).
  workspace_scan.py         One-click scan Workspace health check and fallback switch adapter.
  gen_pp_link.py            PayPal/Stripe payment-link generation. PayPal supports hosted long URL and PP direct approve URL; GoPay/UPI use hosted link variants.
  payment_link_manager.py   Unified method registry, state machine, adapter routing, and redacted run history.
  paypal_links.py           Regenerate PayPal links without clobbering old links and preserve the configured PayPal generation type.
  paypal_auto.py            Project-local PayPal browser page automation helper.
  paypal_nocard.py          Legacy explicit PayPal no-card agreement flow.
  nodriver_captcha.py       Nodriver-based CAPTCHA solver adapter.
  nodriver_paypal.py        Nodriver-based PayPal browser automation helper.
  captcha_solver.py         CAPTCHA solving abstraction.
  grpcurl_client.py         Shared grpcurl subprocess boundary.
  gopay_wa_rebind.py        WA-channel GoPay app auth and change-phone orchestration.
  omakse_client.py          Omakase provider client adapter.
  phone_proxy.py            Phone verification proxy resolution.
  phone_reuse.py            Phone number reuse and inventory management.
  smsbower.py               SMSBower SMS provider activation and polling.
  sms_provider.py           SMS provider abstraction layer.
  proxy_pool.py             SOCKS5 proxy pool server with health checking.
  session_refresh.py        Refresh auth session after manual login/payment.
  payment_auth.py           JIT payment AT probe, HTTP-401 mailbox OTP OAuth refresh, and token telemetry.
  payment_batch.py          Resumable batch payment executor, eligibility matrix, canary, retries, and atomic reports.
  agent_identity.py         Explicit Agent Identity/SUB2API credential conversion; not part of registration.
  sub2api_import.py         SUB2API import boundary with multi-mode auth.
  codex_export.py           Build Codex/CPA-compatible token JSON from session data.
  session_converter.py      Multi-format session/account export conversion core.
  codex_oauth.py            Codex OAuth authorization-code + PKCE login orchestration.
  codex_sentinel.py         Sentinel/cache cookie helpers for auth.openai.com requests.
  codex_phone.py            Optional add-phone SMS verification boundary.
  cpa_import.py             CPA API upload boundary; imports AT-only JSON and uploads normalized CPA payloads.
  import_targets.py         Import target normalization helpers.
  account_scan.py           Account health/quoter scan adapter.
  storage.py                SQLite and session index persistence.
  utils.py                  Shared utility helpers.

SmsWorkbench/               WPF desktop UI.
services/
  gopay-flow/               Project-local GoPay PaymentService wrapper and protocol.
  gopay-app/proto/          GoPay App gRPC contract used by WA rebind mode.
  gopay-adb/                ADB HTTP sidecar for OTP notification polling and unlink.
  protocol-payment/         Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo protocol extractors.
  mail-otp-web/             Standalone Microsoft Graph inbox/OTP diagnostic UI.
tests/                      Offline unit tests; see tests/README.md.
sessions/                   Generated session JSON, ignored by Git.
runtime/                    SQLite, debug output, caches, ignored by Git.
```

## Ownership Matrix

| Feature surface | Owning module | May call | Must not own |
| --- | --- | --- | --- |
| Desktop buttons/dialogs | `SmsWorkbench/` | `chatgpt_phone_reg.py`, SQLite/session read-only display helpers, read-only SMSBower catalog lookup | ChatGPT activation lifecycle, payment protocol, mailbox polling loops |
| CLI command routing | `sms_tool.cli` | Focused command modules | Provider protocol internals or long-lived state mutation outside handlers |
| Mailbox parsing/polling | `sms_tool.mailbox`, `sms_tool.providers/*` | Microsoft Graph, Gmail IMAP/SMTP, mailbox provider clients | Registration success persistence, payment state |
| Phone inventory | `sms_tool.phone_reuse`, `sms_tool.smsbower` | SMS provider APIs | ChatGPT account state, payment state |
| ChatGPT registration | `sms_tool.registration` | mailbox/phone seams, storage through result writers | Payment execution, CPA upload |
| Auth/session refresh | `sms_tool.codex_oauth`, `sms_tool.session_refresh` | mailbox OTP seam, phone seam when explicitly enabled | Phone inventory purchasing outside configured provider seam |
| JIT payment authentication | `sms_tool.payment_auth` | account seed, mailbox OTP OAuth, quota probe | Registration success classification, payment-method creation |
| Payment link generation | `sms_tool.payment_link_manager`, `sms_tool.gen_pp_link`, `sms_tool.paypal_links` | account seed, ChatGPT checkout, Stripe init, protocol adapters | PayPal account signup, final payment authorization |
| Batch payment execution | `sms_tool.payment_batch` | JIT auth, payment manager, eligibility matrix, proxy stages, atomic reports | Registration/mailbox procurement, token-free public reports |
| Payment execution | `sms_tool.paypal_auto` | account seed, saved payment links, provider services | Registration, mailbox pool edits, link regeneration as a side effect |
| Explicit Agent Identity conversion | `sms_tool.agent_identity` | account seed, Ed25519 key gen, storage | Registration flow, payment execution |
| SUB2API import | `sms_tool.sub2api_import` | agent identity, session converter, SUB2API API | Registration, payment, mailbox polling |
| Account import/export conversion | `sms_tool.session_converter`, `sms_tool.codex_export`, `sms_tool.cpa_import`, `sms_tool.sub2api_import` | session JSON, account seed, CPA/SUB2API API | Registration or payment execution |
| Account persistence | `sms_tool.storage` | session JSON and SQLite | Vendor protocol calls |
| Local helper services | `services/*` | Their own provider/runtime APIs | Direct account SQLite writes unless routed through CLI contracts |

## Boundary Rules

### Proxy Routing Boundary

Registration, mailbox receiving, and payment each have their own proxy owner.
The desktop **设置 → 网络与支付 / 网络代理** page exposes them as three separate
fields (`注册代理（主）` + `注册代理池`, `邮箱收件代理`, `协议支付代理池`); that page
holds the authoritative per-machine values, so consult it for the live routing.

- **Registration traffic → JP dynamic proxy.** Registration workers use
  `proxy.registration` (desktop field `注册代理（主）`) plus the `proxy.pool`
  rotation list (`注册代理池`) — a JP dynamic-session upstream whose sticky-session
  id is refreshed per worker (`phone_proxy.refresh_proxy_sid`) so each concurrent
  worker gets a distinct exit IP. `proxy.default` and the phone-verification
  proxy (`phone_reuse.proxy`) are saved to this same registration value.
- **Mailbox receiving → fixed local route.** OTP polling always resolves through
  `mailbox_proxy` (desktop field `邮箱收件代理`), which defaults to
  `http://127.0.0.1:7897` and never inherits the rotating registration proxy
  (`mailbox._resolve_mailbox_proxy`). This keeps inbox fetches on a stable local
  egress independent of the registration session.
- **Payment traffic → independent stage proxies.** PayPal and protocol-payment
  modules resolve `paypal.stage_proxies` / `protocol_payments.proxy_pool` from
  their own payment configuration. A registration or mailbox proxy is never
  treated as a payment override, so configured checkout/provider/confirm/approve/
  promotion routes keep their existing behavior. Only an operator-provided
  payment proxy may explicitly override those stages where the payment module
  permits it.

### PayPal Generation Type

`config.json` `paypal.link_generation_type` is the single selector exposed by
`[配置] -> [协议支付] -> PayPal生成类型`:

- `hosted_long_url`（长链）: `checkout -> stripe init -> stripe_hosted_url`, then persist a normalized `pay.openai.com/c/pay/...` hosted long URL.
- `paypal_direct`（PP直链）: `checkout -> stripe init -> pm create(type=paypal) -> confirm`, then follow Stripe `pm-redirects` to a PayPal `agreements/approve?ba_token=...` URL. The BA token is treated as sensitive and must not be logged in full.
- `paypal_direct_zero_due`（PP直链-强制0元试用）: same direct PayPal approval flow, but `require_zero_due=true`; if Stripe init shows any non-zero amount, the flow stops with `checkout_not_zero_due` and does not persist a BA approval link. This strict mode also disables hosted-link fallback and old saved-link reuse so UI state cannot show a stale `link_ready` URL after the current zero-due direct generation fails.

#### Promotion-update stage（0元 + PayPal 共存）

Optional segmented stage between checkout creation and Stripe init. When
`paypal.stage_proxies.promotion` is set, the extractor calls
`POST /backend-api/payments/checkout/update` through a promo-eligible region
egress to attach the `plus-1-month-free` promo to the **same** checkout that was
created in a PayPal-supported region. This makes 0-due and PayPal coexist on one
session (PayPal availability is decided by the checkout's billing region; promo
eligibility is decided by the egress IP at update time). The stage is opt-in —
leaving `promotion` empty keeps prior behaviour. See
[`docs/paypal-zero-due-link.md`](paypal-zero-due-link.md) for the full protocol,
config keys, CLI usage, and the `PayPal区 × promotion区` matrix search
(`run_batch(..., promotion_countries=[...])`).

The UI saves the compatible low-level knobs (`checkout_ui_mode`, `link_mode`,
`confirm_style`, `resolve_ba_redirect`, `require_ba_token`, and
`require_zero_due`) so the CLI and saved-link regeneration path use the same
flow without duplicating decision logic in the desktop layer.

### One-click Registration Modes

The desktop `【一键注册+支付链接】` action is only a launcher; mode selection is
translated into CLI flags and the protocol remains in `sms_tool.registration`.

- `邮箱注册（跳过手机）` is the compatibility/default path. It emits
  `--registration-at-only --no-phone-reuse`, registers by mailbox email OTP,
  stores the ChatGPT access token, and then generates the selected payment link.
- `手机接码注册+绑定邮箱+PP直链0元` keeps the mailbox as the account email, does not
  use AT-only mode, forces the phone inventory source to SMSBower with
  `--phone-reuse --phone-source smsbower --max-reuse-count 1`, requires the
  Codex OAuth phone verification step to succeed, and forces
  `--paypal-generation-type paypal_direct_zero_due --payment-method paypal`.

This mode does not move SMSBower purchasing logic into the UI. The UI owns only
the selection and command construction; `sms_tool.phone_reuse` owns SMSBower
activation/state, `sms_tool.codex_oauth` owns the add-phone/login verification
step, and `sms_tool.gen_pp_link` owns strict zero-due PayPal direct generation.

### WPF UI

The desktop has three explicit seams for the SMS and account-selection surface:

- `MainWindow.Navigation.cs` owns selected-email lookup, normalization, and the themed `未选择邮箱` notice. Sidebar handlers call this seam rather than creating their own WPF message boxes.
- `DialogFactory.cs` owns application-themed information and confirmation windows.
- `SmsBowerCatalogClient.cs` owns the read-only `getCountries` / `getPricesV2` catalog lookup and response parsing used by the one-click SMS dialog. It cannot purchase, poll, complete, or cancel an activation; those lifecycle operations remain in `sms_tool.smsbower` and `sms_tool.phone_reuse`.

The desktop settings page exposes SMSBower credentials and advanced timing/retry controls only. OpenAI service, country, and price-tier selection belong to the `一键接码` workflow. Static phone-pool editing is intentionally absent from the desktop surface.

`SmsWorkbench/MainWindow.xaml.cs` may:

- Read `config.json`.
- Apply the configured registration proxy (with local `127.0.0.1:7897` fallback) and the fixed `mailbox_proxy` route when launching non-payment commands.
- Create temporary mailbox selection files.
- Start `chatgpt_phone_reg.py`.
- Display SQLite/session/mailbox state.
- Open PayPal links in Chrome incognito.
- Render custom account and inbox popups.
- Copy verification codes from already-fetched mailbox previews.

It must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or direct SQLite business rules beyond display and deletion.

Payment and CPA operations stay separated in the UI: marking payment complete only updates PayPal status, while CPA import is launched by the explicit CPA action.

`SmsWorkbench/App.xaml` owns the fixed white-first minimalist visual system for the desktop app, with black and gray used for text, borders, navigation, and log surfaces. App icon assets live under `SmsWorkbench/Assets/`.

`SmsWorkbench/build_dotnet.ps1` is the **only** supported build entrypoint. It uses `dotnet publish` (not `dotnet build`) to emit the single canonical runnable desktop artifact to `dist/net10/SmsWorkbench.exe`, then calls `SmsWorkbench/clean_dotnet_workspaces.ps1` to remove intermediate `SmsWorkbench/bin/Debug/net10.0-windows`, `SmsWorkbench/bin/Release/net10.0-windows`, and nested runtime folders such as `win-x64` so they are never treated as second distribution directories.

> **⚠ 禁止直接运行 `dotnet build`**。直接 `dotnet build` 只会输出中间产物到 `SmsWorkbench/bin/Release/net10.0-windows/`，该路径不是分发目录，且不会自动清理。所有编译必须通过 `SmsWorkbench/build_dotnet.ps1` 完成。

```powershell
# 正确
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
# 错误 — 不要这样做
dotnet build SmsWorkbench\SmsWorkbench.csproj
```

### CLI

`sms_tool/cli.py` is the orchestration boundary. It may:

- Parse arguments.
- Load mailbox sources.
- Choose single vs batch registration.
- Persist results through `storage.py`.
- Return meaningful exit codes.

It must not silently replace an explicit empty mailbox file with a new provider purchase. If the user passed a mailbox file and no mailbox was parsed, it exits with code `2`.

Optional command modules are lazy seams. Codex export, CPA import, PayPal/GoPay payment, PayPal/GoPay/UPI link regeneration, and session refresh modules are imported only inside the command handler that needs them. Importing `sms_tool.cli` or `sms_tool.__main__` must not start a command or import optional payment/browser dependencies as a side effect.

### Mailbox Layer

`sms_tool/mailbox.py` is the compatibility seam and high-level router. Focused
provider/parsing modules own the implementation:

- `mailbox_parsers.py`: Chatai, token-file, password-file, CFWorker URI, Gmail provider lines, and mailbox email normalization.
- `mailbox_remail.py`: ReMail short-lived/long-lived ordering, authenticated pickup credentials, message normalization, and OTP polling.
- `mailbox_cfworker.py`: CFWorker mailbox creation, message fetch, proxy/direct fallback, and OTP polling.
- `mailbox_graph.py`: Microsoft OAuth refresh.
- `mailbox_gmail.py`: Gmail IMAP receive, Gmail SMTP send, app-password auth, and OAuth refresh auth.
- `outlook_imap.py`: Outlook IMAP fallback when Graph is unavailable or stale.
- `mail_otp.py`: OTP extraction, recipient filtering, subject matching, issued-after filtering, and candidate ordering.

Registration OTP polling accepts a pipe-separated subject keyword string. The
registration flow uses both `verification code` and `login code`, because the
passwordless signup path can receive either subject even when the auth state is
still a signup transaction. Provider clock-skew normalization belongs to the
mailbox router rather than registration orchestration. CFWorker uses the small
`cfworker_otp_issued_after_grace_seconds` window, while ReMail defaults to
`remail_otp_issued_after_grace_seconds=90` because its observed `receivedAt`
timestamps can trail the local send clock by more than one minute. The pre-send
message ID snapshot still prevents an older OTP from being reused.
ReMail registration also performs one bounded resend after 30 seconds without
resetting the original accepted-message window; this recovers provider delivery
misses without extending the configured total OTP timeout.
ReMail batch creation scales its HTTP timeout with the requested quantity, and
the token-file parser accepts `remail://email---serviceToken---orderNo---purchaseId`
so a completed server-side batch can be recovered after a client timeout without
buying the same mailbox quantity again. Ambiguous timeout/retryable-5xx responses
also trigger a strict recent-order lookup by request window, project, product,
mode, and exact quantity before any failure is returned to the caller.

Gmail is a first-class mailbox provider. The preferred import shapes are
`gmail://email---app_password` for app-password mode and
`gmail://email----client_id----client_secret----refresh_token` for OAuth mode.
Gmail credentials and OTP recipients are matched by the complete normalized
address. Dotted local parts, `+tag` addresses, and `googlemail.com` variants are
not rewritten to another mailbox, and no alias mapping file is read.
It must not write registration results or modify mailbox pool files during registration.

### Registration Layer

`sms_tool/registration.py` is the orchestration seam. Focused modules own the
implementation:

- `auth_flow.py`: signin URL construction, authorize navigation, continue calls, and auth-state URL classification.
- `account_creation.py`: OTP validation, create-account continuation, `/api/auth/session` fetch, and payment-link helper calls.
- `batch_runner.py`: concurrent registration worker scheduling, result ordering, mailbox-count capping, and bounded retry of network/auth-state failures with a fresh proxy session.
- `sentinel_tokens.py` / `sentinel_quickjs.py`: Sentinel extraction, QuickJS SDK path, PoW/browser fallback, and cache.
- `auth_state.py`: `client_auth_session_dump` capture and redacted diagnostic summaries.
- `otp_strategy.py`: OTP send/resend endpoint selection.

Batch registration uses each loaded mailbox at most once. If `--count` exceeds loaded unique mailboxes, the batch is capped instead of wrapping with modulo and reusing a mailbox concurrently.
Each account owns a fresh Sentinel transaction and `oai-did`; batch workers do not return tokens to a shared pool. Fresh per-account and mid-flow refresh tokens are not written into the shared cache, and an OAuth-create refresh retains the account's existing device ID. Fresh extraction is guarded by a configurable bounded semaphore (`sentinel_max_concurrency`, default 2, capped at 4); cache-eligible callers retain single-flight population.
`registration.py` re-exports focused helpers directly; it must not shadow them
with local copies or mutate another module's `CFG`/request globals at runtime.

If OTP validation succeeds but create-account returns
`registration_disallowed`, the failure is treated as a provider/server-side
registration refusal for that mailbox/context, not as a mailbox polling failure.
The local error path must preserve that create-account code instead of masking
it with later auth-session transport errors.

### Account Seed Layer

`sms_tool.account_seed` owns the shared lookup of account/session seed data. It may:

- Load an explicit `session_*.json` file.
- Load the SQLite account row for an email.
- Merge persisted raw JSON with the session file.
- Expose normalized `email`, `access_token`, `cookie_header`, and refresh-token fields.
- Extract a ChatGPT access token from flat or `auth_session` shaped data.

Payment adapters may call this seam, but must not duplicate SQLite/session merging logic or import private helpers from each other. This keeps PayPal link regeneration, PayPal browser payment, and legacy PayPal automation independent from one another.

### PayPal Link Layer

`sms_tool/gen_pp_link.py` only generates the hosted Stripe/PayPal/GoPay/UPI URL from an access token. It does not perform PayPal account signup, card entry, SMS verification, wallet authorization, or final payment authorization.

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

The WPF config dropdown and `sms_tool.gen_pp_link` presets currently support `JP`, `US`, `AU`, `DE`, `FR`, `GB`, `IN`, and `BR` for checkout billing-region generation. UPI QR generation separates `checkout_country` from `payment_country`: the checkout request uses `checkout_country` for `billing_details.country/currency`, while `payment_country` records the intended local payment method country and stage routing still comes from `upi.stage_proxies`. The default split is JP checkout billing plus IN payment/provider routing. Stripe remains authoritative: if a JP checkout does not expose `upi` in `payment_method_types`, generation fails with `upi_not_available` instead of fabricating a QR.

### Payment Responsibility Boundary

Payment is split into three independent responsibilities:

All create-link requests first enter `sms_tool.payment_link_manager`. It moves
each run through `created -> validating -> preparing_proxy -> running ->
extracting -> completed/failed`, dispatches the native or vendored adapter,
normalizes the result, and appends a redacted record to
`runtime/payment_link_runs.jsonl`.

1. **Create checkout/link**: `sms_tool.payment_link_manager`,
   `sms_tool.gen_pp_link`, and `sms_tool.paypal_links`.
   They read an access token and return/store a hosted checkout URL or explicit
   failure details. They do not complete payment. The BLIK adapter is the explicit
   exception: when a six-digit BLIK code is supplied, its operation is
   `execute_payment`, it submits the code, and it returns `status=completed`
   without fabricating a URL. The UI must label this action as payment execution.
2. **Execute an explicit payment command**: `sms_tool.paypal_auto`.
   It only runs when the user requests `--one-click-pay` or a matching UI action.
   It uses existing account seed data and payment links rather than registering
   accounts. UPI has no one-click execution adapter in this project; it is a
   hosted-link generation method only.
3. **Persist/display payment state**: `sms_tool.storage` and `SmsWorkbench`.
   Storage normalizes status fields; the UI displays and launches commands. The
   UI must not infer success from a URL alone.

Registration, mailbox refresh, CPA import, account scan, SQLite rebuild, and
session refresh must not implicitly run payment execution. Link regeneration may
update `paypal_url` only through the payment-link seam, and failed regeneration
must preserve useful existing URLs unless the caller explicitly clears them.

### MoMo Batch Validation Boundary

MoMo batch extraction is a staged workflow. First probe each local access token
through the local Codex quota endpoint and retain only HTTP 2xx accounts. Then
run the MoMo adapter through a verified VN proxy session. An account passing the
token probe is only authentication-ready; it is not yet MoMo-ready.

The adapter result must preserve these distinct outcomes:

- `account_trial_ineligible`: the account is authenticated but has no usable trial.
- `card_only_full_price`: checkout is available, but MoMo or a zero-due offer is not.
- `approve_result_blocked`: the provider flow reached approve but did not yield a usable result.
- `ready_with_qr`: a `payment.momo.vn` URL or decoded QR file exists; this is the only successful MoMo extraction outcome.

Batch reports must include requested, probed, non-401, attempted, link-ready,
QR-ready, and failure-category counts separately. Runtime reports, account lists,
payment URLs, QR images, access tokens, and authenticated proxy URLs remain local
artifacts under ignored runtime paths and must not be committed or packaged.

### JIT Payment Authentication and Batch Execution

`sms_tool.payment_auth` is the only payment-boundary AT gate. A saved account is
probed immediately before checkout. HTTP 401 goes directly to email-OTP OAuth;
the candidate AT is re-probed and persisted only on HTTP 200. Permanent
`account_deactivated` rows never enter a relogin loop. Public diagnostics contain
only status codes, JWT timing, and a short SHA-256 correlation value.

`sms_tool.payment_batch` owns protocol-payment cohorts. It consumes an explicit
email list, applies bounded method-specific concurrency, runs the JIT gate per
worker, assigns configured eligibility-matrix cells by payment method and
registration country, retries only classified transient failures, and writes an
atomic token-free checkpoint after every completed account under
`runtime/payment_batches/`. `--payment-batch-id` makes the cohort stable;
`--payment-canary` limits the cohort and `--payment-probe-only` stops the MoMo
flow before payment-method creation.

Registration failures no longer enter `accounts` through CLI orchestration.
They are written to `registration_audit`; a successful initial AT probe is a
candidate and only the configured stability-window HTTP-200 result becomes an
active account/session. `accounts.batch_id` and `accounts.registration_state`
allow payment batches to select a registration cohort without inferring it from
timestamps.

Registration stage scheduling is implemented by `registration_progress`: a
worker releases its network slot when it enters mailbox OTP polling, then
reacquires the bounded network gate for resend/validation/account creation.
AT probing and payment have independent caps. This permits more mailboxes to
wait concurrently without multiplying auth/provider request concurrency.
`--target-at200` uses stable-probe successes as its target and respects mailbox
purchase/cost caps plus the ReMail supplier dead-rate circuit breaker.

### Payment Eligibility Matrix

`protocol_payments.matrix.cells` defines small canary cohorts. Each cell records
registration country, checkout/promotion/provider/approve/redirect countries,
strategy, and sample size. The executor reports authentication, eligibility,
offer shape, link-ready, and QR-ready counts per cell. Account/offer conclusions
are not retried with another proxy.

MoMo carries Checkout, Promotion, Stripe provider, Approve, and Redirect proxies
as distinct stage values end to end. A common seed may still be rotated to the
cell's stage countries, preserving one sticky chain identity. Its Stripe API
version, runtime version, client betas, and confirm style are configuration data
under `protocol_payments.methods.momo.stripe_profile`, so a one-account canary can
detect protocol drift before a large batch.

Kakao emits one structured JSON contract on success and failure. Conclusive
credential or checkout-offer results stop immediately; only network/proxy errors
rotate a seed. A successful result requires a Kakao/Nicepay redirect host.

Sentinel account transactions remain independent. Performance optimization is
limited to bounded extraction concurrency, SDK file reuse, token-free queue and
provider timing metrics, and short provider circuit breakers. Sentinel tokens,
device IDs, UA profiles, and TLS profiles are never pooled across accounts.
An optional `sentinel_prewarm_window` starts a bounded set of one-to-one futures
for the first registration workers. Each future is bound to that account's
first-attempt proxy and is consumed once; a retry always creates a fresh
transaction.

### PayPal Payment Layer

`sms_tool/paypal_auto.py` owns browser page mechanics: form filling, PayPal challenge detection, SMS polling hooks, and browser-engine fallback. It must not regenerate links, select accounts, or persist SQLite rows directly except through the result passed back to the adapter.

`sms_tool/paypal_nocard.py` remains available as the older explicit no-card agreement implementation. `sms_tool/paypal_protocol.py` extracts shared protocol helpers (BA token extraction, Stripe redirect follower) used by both `paypal_nocard.py` and `paypal_links.py`.

### GoPay WA Rebind Layer

`sms_tool.gopay_wa_rebind` adapts the byte-v-forge WA flow to this project. `sms_tool.grpcurl_client` owns grpcurl process execution and proto path resolution only.

### Local Provider Services

`services/gopay-flow` is the project-local PaymentService. It owns the ChatGPT checkout, Stripe/Midtrans GoPay linking, OTP handoff, PIN charge, ChatGPT verify, optional unlink trigger, and SMSBower GoPay signup/bootstrap. Its SMSBower bootstrap path imports `gopay_pure_protocol.py` directly and must not call `GopayAppService` or import `opai.core.gojek_client`.

`services/gopay-app/proto` stores the GoPay App service contract used by WA rebind mode. The app-service implementation is a provider boundary: it may be supplied by a local project service or compatible binary, but callers inside `sms_tool` only depend on the proto-level RPC surface.

`services/gopay-adb` owns emulator/ADB HTTP endpoints such as `/health`, `/otp`, `/otp/clear`, and `/gopay/unlink`. It must not know about ChatGPT accounts, SQLite rows, or CPA import.

`services/mail-otp-web` is a standalone operator diagnostic surface for Microsoft Graph inbox/OTP extraction. It accepts the same mailbox account-line formats as `sms_tool.mailbox`, refreshes Microsoft access tokens, displays recent messages, and may return a rotated mailbox refresh token to the operator. It is not the main registration mailbox owner: registration still uses `sms_tool.mailbox`, and this helper service must not edit `hotmail.txt`, session JSON, or SQLite rows directly.


### Agent Identity Layer (Explicit Import Only)

`sms_tool/agent_identity.py` is an explicit credential-conversion boundary used by
SUB2API import. The registration pipeline no longer calls it:

- Generate Ed25519 key pairs in PKCS#8 format.
- Persist private keys independently under `sessions/agent_identities/`.
- Register Agent Identity and task only when the operator explicitly requests the
  SUB2API Agent Identity import mode.
- Handle 403 responses from Free accounts (expected limitation, silently handled).
- Fall back to OAuth mode when Agent Identity registry is disabled.

Agent Identity keys are reused across SUB2API imports. The private key is never
logged or exported in full. A failed Agent Identity conversion must not change the
registration result or invalidate an HTTP-200 AT account.

### SUB2API Import Layer

`sms_tool/sub2api_import.py` owns the SUB2API import boundary:

- Accept session JSON or account-seed data.
- Support three auth modes: `auto` (Free accounts prefer Agent Identity), `oauth`, and `agent_identity`.
- Normalize account data via `session_converter.py` before upload.
- Optionally verify connectivity after import (`sub2api.verify_after_import`).
- Export `expires_at` as Unix timestamp (int64) for Go backend compatibility.

It must not perform registration or payment. It reads existing session data and
agent identity keys, then uploads to the configured SUB2API endpoint.

### Removed / Deprecated Surfaces

- `browser_extensions/paypal_autofill/` is retired. The maintained PayPal browser path is the project-local Python adapter.
- `tests/test_paypal_autofill_*.py` are retired with that extension.
- LuckMail support is retired; ReMail is the maintained API mailbox source.
- Runtime debug artifacts and `__pycache__` folders are not source surfaces and should be deleted or ignored.
- Backup binaries such as `*.exe~` and unused duplicate artwork are not source surfaces.

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
- Payment method persistence for GoPay/UPI/PayPal compatibility.
- Rebuilding SQLite from `sessions/session_*.json`.

`accounts.email` is treated as a normalized logical key. Updates should modify an existing row for the same complete email address instead of creating a new row with different casing.

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

`sms_tool/session_converter.py` is the multi-format conversion core used by export-account flows. `sms_tool/codex_export.py` converts session JSON into the compact Codex JSON shape. `sms_tool/cpa_import.py` accepts existing AT-only session JSON, normalizes it into the CPA payload shape, and uploads it without requiring RT.

Important behavior:

- `codex_oauth.allow_passwordless_takeover=true` is an explicit escape hatch for manual export/refresh paths.
- Forced email OTP may still require add-phone for some accounts. Phone SMS handling remains a separate opt-in boundary via `codex_oauth.auto_phone_verification`.

## Portable Configuration

All paths in `config.example.json` are relative by default:

```json
{
  "email_registration": {
    "token_file": "mailbox_tokens.txt",
    "cfworker_otp_issued_after_grace_seconds": 10
  },
  "mailbox_proxy": "http://127.0.0.1:7897",
  "proxy": {
    "registration": "http://user:pass-JP-session-5m@gateway:port",
    "default": "http://user:pass-JP-session-5m@gateway:port",
    "pool": ["http://user:pass-JP-session-5m@gateway:port"]
  },
  "protocol_payments": {
    "proxy_pool": ["http://user-region-JP-sid-session-t-5:pass@gateway:port"]
  },
  "k12": {
    "workspace_ids": "631e1603-06cf-4f0b-b79b-d09fbfcfe98d",
    "retries": 2,
    "retry_backoff_seconds": 5,
    "invite_timeout_seconds": 240
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
skills-lock.json
session.json
hotmail*.txt
*_tokens.txt
```
