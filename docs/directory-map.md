# Directory Map

This file classifies the repository by responsibility. It is intentionally about
physical placement; `docs/architecture.md` defines the behavioral boundaries.

## Top-level source directories

| Path | Classification | Owner / responsibility | Notes |
| --- | --- | --- | --- |
| `sms_tool/` | Python application core | CLI orchestration, mailbox handling, registration, payment links, payment adapters, storage, account scans, and terminal account cleanup rules | Keep command-specific imports lazy in `sms_tool.cli`. |
| `SmsWorkbench/` | Desktop UI | WPF launcher, account grid, themed dialogs, selected-email seam, account liveness, batch protocol-payment dialog, fixed non-payment proxy launcher, read-only SMSBower catalog adapter, local command planning/result presentation, desktop publish scripts | UI starts CLI commands; payment stage routing and other business logic stay in `sms_tool`. |
| `services/` | Local provider services | Optional mailbox and payment-protocol helpers used by CLI/UI | Services expose explicit process/API boundaries and should not write account SQLite directly. |
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
| `.pytest_cache/`, `TestResults/`, `*.trx`, coverage output | Test-run output | Delete or ignore; never use as release evidence. |
| `SmsWorkbench/**/bin/`, `SmsWorkbench/**/obj/`, `tests/**/bin/`, `tests/**/obj/` | .NET build intermediates | Rebuild from source; never commit. |
| `.workbuddy-ai/`, IDE metadata | Tool-local metadata | Delete or ignore; project decisions belong in source-owned docs. |
| `.zcode/`, root `gates/` | Obsolete generated state | Delete or ignore. Active process-lock slots are generated only under ignored `runtime/gates/`. |

## `sms_tool/` module groups

| Group | Files | Boundary |
| --- | --- | --- |
| Entrypoints/config | `__main__.py`, `cli.py`, `config.py`, `paths.py`, `commands/helpers.py` | Parse global options and resolve config/paths; no vendor protocol implementation. |
| CLI command adapters | `commands/payment.py`, `commands/payment_links.py`, `commands/registration.py`, `commands/accounts.py`, `commands/mailbox_ops.py`, `commands/one_click.py`, `commands/omakse.py` | Translate parsed CLI arguments into domain workflow requests and process exit codes; replaceable hooks arrive through explicit frozen context dataclasses. No provider wire protocol or persistence implementation. `cli.py` may retain thin compatibility wrappers only. |
| Mailbox and phone inventory | `mailbox.py`, `mailbox_types.py`, `mailbox_parsers.py`, `mailbox_remail.py`, `mailbox_smailr.py`, `mailbox_cfworker.py`, `mailbox_graph.py`, `mailbox_gmail.py`, `mailbox_icloud_url.py`, `mailbox_chongzhi.py`, `outlook_imap.py`, `mail_otp.py`, `providers/`, `smsbower.py`, `phone_reuse.py`, `phone_proxy.py`, `sms_provider.py` | Acquire/poll mailboxes or phone activations; ReMail uses API-key-authenticated ordering and service-token pickup with adaptive OTP polling; Smailr supports configured domain IDs, restricted-domain mailbox reuse, detail-body fetch and clock-skew tolerance; Gmail receive/send and iCloud OTP-URL decoding stay inside the mailbox seam; no account persistence except through explicit callers. |
| Registration/auth | `registration.py`, `registration_progress.py`, `registration_concurrency.py`, `cross_process_gate.py`, `registration_outcome.py`, `session_builder.py`, `account_2fa.py`, `auth_flow.py`, `auth_headers.py`, `account_creation.py`, `batch_runner.py`, `sentinel_tokens.py`, `sentinel_quickjs.py`, `otp_strategy.py`, `auth_state.py`, `error_classification.py`, `codex_oauth.py`, `codex_sentinel.py`, `codex_phone.py`, `session_refresh.py` | ChatGPT/OpenAI auth, OTP, Sentinel, session refresh, optional phone verification, progress persistence, in-process stage resource gates plus OS file-lock slots shared by desktop/CLI processes, result judgment, canonical session assembly, and TOTP 2FA enrollment. |
| Agent Identity / explicit import | `agent_identity.py`, `sub2api_import.py` | Ed25519 credential conversion for explicit SUB2API import; not called by the registration pipeline. Keys are persisted under `sessions/agent_identities/`. |
| Workspace compatibility | `k12_client.py`, `k12_identity.py`, `workspace_scan.py` | Legacy explicit Workspace helpers retained for Python callers; the CLI account scan no longer enables this path. |
| Account liveness and recovery | `account_liveness.py`, `account_recovery.py`, `account_scan.py` | Canonical side-effect-free quota probe, explicit OAuth recovery/persistence, and batch account scan; does not switch Workspace state. |
| Account cleanup | `account_cleanup.py`, `scripts/cleanup_invalid_accounts.py`, `scripts/mailbox_pool_orphans.py` | Classify only terminal dropped/deactivated/missing-AT/token-invalid rows and archive/delete their local representations; unknown transport results are retained. `mailbox_pool_orphans.py` is a read-only pool/DB/session reconciliation report that prunes only no-session orphans with `--apply` (dry-run + pool backup by default). |
| Payment links and capability | `payment_link_manager.py`, `payment_auth.py`, `checkout_contract.py`, `payment_capability.py`, `payment_flow.py`, `payment_routing.py`, `payment_executor.py`, `gen_pp_link.py`, `wallet_provider.py`, `wallet_transport.py`, `gcash_provider.py`, `gcash_transport.py`, `paypal_proxy.py`, `paypal_reverse.py` | JIT AT gate, canonical Checkout/Stripe init contract, shared stage vocabulary, immutable method-owned route plans, common execution states, provider-aware side-effect-limited probing, unified terminal results, native/shared-wallet/custom-payment adapters, link reuse, and stage proxy resolution. Promotion/Update is supported by PayPal and by GoPay full/probe zero-due flows; see [`paypal-zero-due-link.md`](paypal-zero-due-link.md) for PayPal details. |
| Payment batch execution | `payment_batch.py` | Stable email cohorts, JIT refresh, capability-aware eligibility matrix, method concurrency, canary pause, classified retry, and atomic token-free checkpoints under `runtime/payment_batches/`. |
| Payment execution and reconciliation | `paypal_auto.py`, `paypal_protocol.py`, `paypal_reconciliation.py`, `nodriver_paypal.py`, `omakse_client.py` | Execute explicit payment commands or independently reconcile an allowlisted PayPal merchant return; reconciliation does not alter the payment-link interface. |
| Account data/import/export | `account_seed.py`, `storage.py`, `codex_export.py`, `cpa_import.py`, `sub2api_import.py`, `session_converter.py`, `import_targets.py` | Normalize account/session state, convert between formats, and upload to external import targets (CPA, SUB2API); CPA import does not own local liveness or recovery. |
| Desktop read transport | `desktop_read.py`, `desktop_serve.py` and `SmsWorkbench/DesktopReadClient.cs` | Sanitized account/mailbox read contracts, resident request-ID-correlated JSONL transport, one-shot fallback, process restart, and file-metadata caches. No registration or payment mutation. |
| Shared utilities | `http_client.py`, `captcha_solver.py`, `nodriver_captcha.py`, `proxy_pool.py`, `doctor.py`, `utils.py` | Reusable transport/browser/helper logic and offline environment diagnostics with minimal state ownership. |

## `SmsWorkbench/` payment command boundary

| File | Responsibility |
| --- | --- |
| `MainWindow.Payment.cs` | Read control state, invoke the payment-link seam, and apply the returned view state. |
| `ProtocolPaymentExecution.cs` | Build deterministic backend command plans and convert backend JSON into presentation models; contains no WPF control access. |
| `PaymentBatchService.cs`, `PaymentBatchViewModel.cs` | Batch dialog execution and state; do not duplicate single-account command planning. |
| `PaymentMethods.cs` | Canonical desktop payment-method catalog, aliases, countries, and single/batch availability. |
| `AccountGridPresentation.cs` | Promotion status classification plus full filtered-set ordering before pagination. |

## `SmsWorkbench/` window-independent backend interpreters

These types hold the deterministic command-building and backend-result logic that
must stay testable without a WPF window (see placement rule 8). `MainWindow.*`
partials call them and only apply the returned view state.

| File | Responsibility |
| --- | --- |
| `BackendCommandPlanner.cs` | Build `BackendCommandPlan` argument lists for registration/payment/scan tasks from primitive inputs; no WPF access. |
| `BackendResultInterpreter.cs` | Interpret backend execution results and proxy-test JSON into typed outcomes (success/timeout/cancelled). |
| `BackendJson.cs`, `BackendJsonProtocol.cs` | Canonical JSON-to-dictionary projection and protocol framing shared by interpreters; keep WPF out of JSON plumbing. |
| `BackendContracts.cs`, `BackendTaskCoordinator.cs` | Backend output-channel contracts and single-flight task coordination. |
| `AccountStatusInterpreter.cs` | Window-independent account JSON interpretation: plan type, wham quota labels, payment status, deactivation, import state. |
| `AccountScanResultInterpreter.cs` | Parse account-scan backend output into per-row presentation state. |

## `services/` module groups

| Path | Boundary |
| --- | --- |
| `services/protocol-payment/` | Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo subprocess extractors. `common/protocol_core.py` owns the exactly-once, redacted `protocol_payment.v1` terminal reporter used by iDEAL/BLIK/TWINT. GoPay/GrabPay share a Python adapter under `sms_tool/`; GCash uses its own Python custom-payment adapter under `sms_tool/`. |
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
8. Command builders and backend-result parsing must be testable without a WPF
   window. Keep those deterministic parts outside `MainWindow.*` code-behind.

## Cleanup boundary

Generated files are not all interchangeable. Cache and build output may be
deleted after the owning process stops: Python bytecode/cache, .NET `bin/obj`,
test results, retention helper logs, the Windows `nul` artifact, and tool-local
`.workbuddy-ai` metadata. Preserve or explicitly archive `config.json*`, mailbox
and token files, `session.json`, `sessions/`, `runtime/`, and provider state
backups because they may contain credentials, account state, reconciliation
evidence, or resumable checkpoints. Never use a broad `git clean -xfd` in this
repository.
