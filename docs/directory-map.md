﻿﻿﻿# Directory Map

This file classifies the repository by responsibility. It is intentionally about
physical placement; `docs/architecture.md` defines the behavioral boundaries.

## Top-level source directories

| Path | Classification | Owner / responsibility | Notes |
| --- | --- | --- | --- |
| `sms_tool/` | Python application core | CLI orchestration, mailbox handling, registration, payment links, payment adapters, storage, account scans | Keep command-specific imports lazy in `sms_tool.cli`. |
| `SmsWorkbench/` | Desktop UI | WPF launcher, account grid, themed dialogs, selected-email seam, account liveness, batch protocol-payment dialog, fixed non-payment proxy launcher, read-only SMSBower catalog adapter, local command execution, desktop publish scripts | UI starts CLI commands; payment stage routing and other business logic stay in `sms_tool`. |
| `services/` | Local provider services | Optional GoPay, ADB, and mailbox helper services used by CLI/UI | Services expose explicit process/API boundaries and should not write account SQLite directly. |
| `tests/` | Offline verification | Unit tests for module seams and persistence semantics | Live vendor/browser tests must be opt-in. |
| `docs/` | Source-owned documentation | Architecture, boundaries, directory map, and operating notes | Do not place runtime logs or screenshots here unless deliberately curated. |
| `scripts/` | Operator scripts | Small launch/setup helpers that call source modules or local services | Keep scripts idempotent and repository-relative. |

## Root-level files

| Path | Classification | Owner / responsibility |
| --- | --- | --- |
| `chatgpt_phone_reg.py` | Compatibility entrypoint | Delegates to `sms_tool.cli`; no business logic should be added here. |
| `config.example.json` | Portable config template | Safe defaults and placeholders only. |
| `requirements.txt` | Python dependency manifest | Single committed Python dependency source. |
| `README.md` | Operator quick start | Setup, mailbox formats, common commands, and high-level module list. |
| `PROXY_GUIDE.md` | Proxy operation guide | Local proxy/stage-proxy setup; no machine-specific secrets. |
| `pytest.ini` | Test discovery compatibility | Keeps repository-wide pytest discovery and markers. |
| `start_proxy_pool.py` | Operator utility | Standalone SOCKS5 proxy-pool server entrypoint. |
| `verify_proxy.py` | Operator utility | Proxy configuration verification; reads `config.json`. |
## Runtime and generated directories

These directories are runtime state and are ignored by Git:

| Path | Contents | Rule |
| --- | --- | --- |
| `sessions/` | Generated `session_*.json` account/session files | Never commit; may contain tokens/cookies. |
| `runtime/` | SQLite index, caches, logs, debug output | Never commit; summarize redacted state only. |
| `dist/` | Published WPF executable and installer assets | Rebuild with `SmsWorkbench/build_dotnet.ps1` or `scripts/build_installer.ps1`; do not commit. |
| `.dotnet/` | Local bundled/runtime SDK | Local machine dependency; do not commit. |
| `__pycache__/`, `*.pyc` | Python bytecode | Delete or ignore. |

## `sms_tool/` module groups

| Group | Files | Boundary |
| --- | --- | --- |
| Entrypoints/config | `__main__.py`, `cli.py`, `config.py`, `paths.py`, `commands/` | Parse commands and resolve config/paths; no vendor protocol implementation. |
| Mailbox and phone inventory | `mailbox.py`, `mailbox_types.py`, `mailbox_parsers.py`, `mailbox_remail.py`, `mailbox_cfworker.py`, `mailbox_graph.py`, `mailbox_gmail.py`, `mailbox_chongzhi.py`, `outlook_imap.py`, `mail_otp.py`, `providers/`, `smsbower.py`, `phone_reuse.py`, `phone_proxy.py`, `sms_provider.py` | Acquire/poll mailboxes or phone activations; ReMail uses API-key-authenticated ordering and service-token pickup with adaptive OTP polling; Gmail receive/send stays inside the mailbox seam and uses exact mailbox addresses without alias expansion; no account persistence except through explicit callers. |
| Registration/auth | `registration.py`, `registration_progress.py`, `auth_flow.py`, `auth_headers.py`, `account_creation.py`, `batch_runner.py`, `sentinel_tokens.py`, `sentinel_quickjs.py`, `otp_strategy.py`, `auth_state.py`, `error_classification.py`, `codex_oauth.py`, `codex_sentinel.py`, `codex_phone.py`, `session_refresh.py` | ChatGPT/OpenAI auth, OTP, Sentinel, session refresh, optional phone verification, error classification. |
| Agent Identity / explicit import | `agent_identity.py`, `sub2api_import.py` | Ed25519 credential conversion for explicit SUB2API import; not called by the registration pipeline. Keys are persisted under `sessions/agent_identities/`. |
| Workspace compatibility | `k12_client.py`, `k12_identity.py`, `workspace_scan.py` | Legacy explicit Workspace helpers retained for Python callers; the CLI account scan no longer enables this path. |
| Account scan | `account_scan.py` | OAuth/account health and quota scan; does not switch Workspace state. |
| Payment links | `payment_link_manager.py`, `payment_auth.py`, `gen_pp_link.py`, `paypal_links.py`, `paypal_proxy.py`, `paypal_reverse.py` | JIT AT gate plus unified state machine and adapters, native link generation/reuse, stage proxy resolution, and reverse-engineering helpers. Optional promotion-update stage (`/checkout/update`) for 0元+PayPal — see [`paypal-zero-due-link.md`](paypal-zero-due-link.md). |
| Payment batch execution | `payment_batch.py` | Stable email cohorts, JIT refresh, eligibility matrix, method concurrency, canary pause, transient retry, and atomic token-free checkpoints under `runtime/payment_batches/`. |
| Payment execution | `paypal_auto.py`, `paypal_protocol.py`, `nodriver_paypal.py`, `grpcurl_client.py`, `omakse_client.py` | Execute explicit payment commands only; use account seed and storage seams. GoPay app RPCs are owned by `services/gopay-flow`. |
| Account data/import/export | `account_seed.py`, `storage.py`, `codex_export.py`, `cpa_import.py`, `sub2api_import.py`, `session_converter.py`, `import_targets.py` | Normalize account/session state, convert between formats, and upload to external import targets (CPA, SUB2API). |
| Shared utilities | `http_client.py`, `captcha_solver.py`, `nodriver_captcha.py`, `proxy_pool.py`, `utils.py` | Reusable transport/browser/helper logic with minimal state ownership. |

## `services/` module groups

| Path | Boundary |
| --- | --- |
| `services/gopay-flow/` | Local GoPay PaymentService and pure-protocol payment/signup implementation. |
| `services/gopay-app/` | GoPay App gRPC implementation/contract used by GoPay registration and phone-change flows. |
| `services/gopay-adb/` | ADB/notification sidecar for local emulator integration. |
| `services/protocol-payment/` | Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo protocol extractors. |
| `services/mail-otp-web/` | Standalone Microsoft Graph inbox/OTP helper UI; operator diagnostic service, not the main registration mailbox owner. |

## Placement rules for new work

1. If it is a CLI command, add a lazy handler in `sms_tool.cli` and put the
   implementation in a focused module under `sms_tool/`.
2. If it is a desktop button/dialog, put UI code in `SmsWorkbench/` and call the
   CLI/backend rather than duplicating protocol logic in C#.
   Read-only provider metadata needed before launch belongs in a focused catalog
   module such as `SmsBowerCatalogClient.cs`, not in a `MainWindow` handler.
3. If it talks to a provider, isolate it under `sms_tool/providers/` or
   `services/<provider>/` and expose a small public method.
4. If it extends mailbox/registration/K12 behavior, prefer adding a focused
   module behind the existing compatibility seam (`mailbox.py`,
   `registration.py`) rather than growing those seam files.
5. If it persists account state, route through `sms_tool.storage` or a documented
   storage seam.
6. If it is runtime output, put it under `runtime/` or `sessions/`, not in source
   directories.
7. Sidebar actions that require an email must use the selected-email seam and
   the themed `未选择邮箱` dialog; do not call `MessageBox.Show` for that state.
