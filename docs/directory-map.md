# Directory Map

This file classifies the repository by responsibility. It is intentionally about
physical placement; `docs/architecture.md` defines the behavioral boundaries.

## Top-level source directories

| Path | Classification | Owner / responsibility | Notes |
| --- | --- | --- | --- |
| `sms_tool/` | Python application core | CLI orchestration, mailbox handling, registration, payment links, payment adapters, storage, account scans, and terminal account cleanup rules | Keep command-specific imports lazy in `sms_tool.cli`. |
| `SmsWorkbench/` | Desktop UI | WPF launcher, account grid, themed dialogs, selected-email seam, account liveness, mailbox-change dialog, batch protocol-payment dialog, fixed non-payment proxy launcher, read-only SMSBower catalog adapter, local command planning/result presentation, desktop publish scripts | UI starts CLI commands; payment and mailbox-change protocol logic stays in `sms_tool`. |
| `services/` | Local provider services | Optional mailbox and payment-protocol helpers used by CLI/UI | Services expose explicit process/API boundaries and should not write account SQLite directly. |
| `tests/` | Offline verification | Unit tests for module seams and persistence semantics | Live vendor/browser tests must be opt-in. |
| `docs/` | Source-owned documentation | Architecture, boundaries, directory map, and operating notes | Do not place runtime logs or screenshots here unless deliberately curated. |
| `scripts/` | Operator scripts | Small launch/setup helpers that call source modules or local services | Keep scripts idempotent and repository-relative. |
| `local/` | Local operator notes | Machine-local notes deliberately kept in-tree (currently only `local/README.md`) | **Tracked**, so it must stay secret-free; personal scratch state belongs under ignored `runtime/` instead. |
| `.githooks/` | Commit gates | `pre-commit` runs four gates: credential guard, line-ending guard, mailbox private-import ratchet, sentinel asset guard | Installed by `scripts/install_git_hooks.py` (sets `core.hooksPath`). Stdlib-only on purpose — it must run before dependencies are installed. |
| `.github/` | CI | `workflows/ci.yml` holds the authoritative gate list for the Python, .NET and schema checks | Keep in sync with `.githooks/pre-commit` and [architecture.md](architecture.md#boundary-rule-checks). |

## Root-level files

| Path | Classification | Owner / responsibility |
| --- | --- | --- |
| `chatgpt_phone_reg.py` | Compatibility entrypoint | Delegates to `sms_tool.cli`; no business logic should be added here. |
| `config.example.json` | Portable config template | Safe defaults and placeholders only. |
| `requirements.txt` | Python dependency manifest | Single committed Python dependency source. |
| `README.md` | Operator quick start | Setup, mailbox formats, common commands, and high-level module list. |
| `PROXY_GUIDE.md` | Proxy operation guide | Local proxy/stage-proxy setup; no machine-specific secrets. |
| `pytest.ini` | Test discovery compatibility | Keeps repository-wide pytest discovery and markers. |
| `config_schema.json` | Cross-language config manifest | Shard ownership checked against Python and C# stores. |
| `ipc_schema.json` | Resident desktop-read manifest | Protocol version and operation set checked against Python and C# sources. |
| `start_proxy_pool.py` | Operator utility | Standalone proxy-pool server entrypoint: a SOCKS5 listener rotating over SOCKS5 **or** HTTP-CONNECT upstreams. |
| `verify_proxy.py` | Operator utility | Proxy configuration verification; reads merged proxy/runtime/payment shards. |

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

| Group | Files | Boundary | Check (recompute) |
| --- | --- | --- | --- |
| Entrypoints/config | `__main__.py`, `cli.py`, `config.py`, `paths.py`, `commands/helpers.py` | Parse global options and resolve config/paths; no vendor protocol implementation. | `git ls-files sms_tool/__main__.py sms_tool/cli.py sms_tool/config.py sms_tool/paths.py sms_tool/commands/helpers.py` |
| CLI argument groups | `cli_parsers/` (`core.py`, `payment.py`, `paypal.py`, `quota.py`, `session.py`, `codex.py`, `email_change.py`, `omakse.py`, `one_click.py`, `sub2api.py`) | One module per command family; each exposes `register(parser)` holding **only** `add_argument` declarations, so `cli.build_parser` stays a list of `register` calls. Declaration only — no dispatch, no post-parse validation, no provider imports. Importing a parser module must not pull in the command it describes (Rule 15). | `git ls-files "sms_tool/cli_parsers/*.py"` |
| CLI command adapters | `commands/payment.py`, `commands/payment_links.py`, `commands/registration.py`, `commands/accounts.py`, `commands/email_change.py`, `commands/mailbox_ops.py`, `commands/one_click.py`, `commands/omakse.py` | Translate parsed CLI arguments into domain workflow requests and process exit codes; replaceable hooks arrive through explicit frozen context dataclasses or keyword-injected adapters. No provider wire protocol or persistence implementation. `cli.py` retains parsing and thin dispatch only. | `git ls-files sms_tool/commands/payment.py sms_tool/commands/payment_links.py sms_tool/commands/registration.py sms_tool/commands/accounts.py sms_tool/commands/email_change.py sms_tool/commands/mailbox_ops.py sms_tool/commands/one_click.py sms_tool/commands/omakse.py` |
| Mailbox and phone inventory | `mailbox.py`, `mailbox_service.py`, `mailbox_strategies.py`, `mailbox_types.py`, `mailbox_parsers.py`, `providers/mailbox_*.py`, `providers/*_client.py` (`cfworker_client`, `smailr_client`, `outlook_imap_client`), `mail_otp.py`, `smsbower.py`, `phone_reuse.py`, `phone_proxy.py`, `sms_provider.py` | Acquire/poll mailboxes or phone activations. The immutable registry resolves fetch, poll and credential capabilities; `mailbox_errors.py` owns terminal polling classification. All active mailbox provider implementations live under `sms_tool/providers/`; the top-level `mailbox_<provider>` files are aliasing facades only. `mailbox.py` itself is **not** one of those facades: it is the implementation and the private-symbol hub, which is why its cross-module consumers are ratcheted by Boundary Rule 16 rather than assumed away. **Naming contract inside `providers/`:** low-level wire clients end in `_client`, registration-facing flows that compose them are named `mailbox_*`; a `*_client` module must never import a `mailbox_*` module. Chongzhi was removed; unsupported legacy lines are skipped. | `git ls-files sms_tool/mailbox.py sms_tool/mailbox_service.py sms_tool/mailbox_strategies.py sms_tool/mailbox_types.py sms_tool/mailbox_parsers.py "sms_tool/providers/mailbox_*.py" sms_tool/providers/cfworker_client.py sms_tool/providers/smailr_client.py sms_tool/providers/outlook_imap_client.py sms_tool/mail_otp.py sms_tool/smsbower.py sms_tool/phone_reuse.py sms_tool/phone_proxy.py sms_tool/sms_provider.py` |
| Browser registration drivers | `registration_drivers/` (`base.py`, `external_sessions/`, `platform_patches.py`, `browser_session.py`, `browser_flow/`, `playwright.py` shell, `stealth.py`, driver wrappers), `browser_pool.py`, `browser_fingerprint_pool.py`, `fingerprint_pool.py` | Session factory and managed lifecycle, profile configuration and egress probe are separated inside `external_sessions/`. `browser_flow/` owns page state and forms. `base.py` owns the registry. Tests patch the defining module or inject the public factory. | `git ls-files "sms_tool/registration_drivers/*.py" "sms_tool/registration_drivers/**/*.py" sms_tool/browser_pool.py sms_tool/browser_fingerprint_pool.py sms_tool/fingerprint_pool.py` |
| Registration state and policy | `registration_operations.py`, `registration_runtime.py`, `registration_policy.py`, `telemetry.py` | Immutable per-invocation dependency binding, six state groups with flat compatibility properties, shared retry/advice decisions and versioned command/run correlation. Persistent state schemas remain unchanged. | `git ls-files sms_tool/registration_operations.py sms_tool/registration_runtime.py sms_tool/registration_policy.py sms_tool/telemetry.py` |
| Registration/auth | `registration.py` (facade), `registration_handlers.py`, `registration_state.py`, `registration_progress.py`, `registration_concurrency.py`, `cross_process_gate.py`, `registration_outcome.py`, `registration_result.py`, `registration_preflight.py`, `registration_retry_guard.py`, `registration_cancel.py`, `registration_pulse.py`, `session_builder.py`, `accounts/account_2fa.py`, `auth_flow.py`, `auth_headers.py`, `accounts/account_creation.py`, `batch_runner.py`, `sentinel_tokens.py`, `otp_strategy.py`, `auth_state.py`, `error_classification.py`, `codex_sentinel.py`, `codex_phone.py`, `phone_registration.py`, `session_refresh.py` | ChatGPT/OpenAI auth, OTP, Sentinel, session refresh, optional phone verification, progress persistence, in-process stage resource gates plus OS file-lock slots shared by desktop/CLI processes, cooperative cancellation, pulse-wave batch scheduling, result judgment, the shared result contract (`registration_result.build_registration_result`), canonical session assembly, and TOTP 2FA enrollment. `registration.py` is a compatibility facade: re-exports only, no implementation. | `git ls-files sms_tool/registration.py sms_tool/registration_handlers.py sms_tool/registration_state.py sms_tool/registration_progress.py sms_tool/registration_concurrency.py sms_tool/cross_process_gate.py sms_tool/registration_outcome.py sms_tool/registration_result.py sms_tool/registration_preflight.py sms_tool/registration_retry_guard.py sms_tool/registration_cancel.py sms_tool/registration_pulse.py sms_tool/session_builder.py sms_tool/accounts/account_2fa.py sms_tool/auth_flow.py sms_tool/auth_headers.py sms_tool/accounts/account_creation.py sms_tool/batch_runner.py sms_tool/sentinel_tokens.py sms_tool/otp_strategy.py sms_tool/auth_state.py sms_tool/error_classification.py sms_tool/codex_sentinel.py sms_tool/codex_phone.py sms_tool/phone_registration.py sms_tool/session_refresh.py` |
| Agent Identity / explicit import | `agent_identity.py`, `sub2api_import.py` | Ed25519 credential conversion for explicit SUB2API import; not called by the registration pipeline. Keys are persisted under `sessions/agent_identities/`. | `git ls-files sms_tool/agent_identity.py sms_tool/sub2api_import.py` |
| Workspace compatibility | `k12_client.py`, `k12_identity.py`, `workspace_scan.py` | Legacy explicit Workspace helpers retained for Python callers; the CLI account scan no longer enables this path. | `git ls-files sms_tool/k12_client.py sms_tool/k12_identity.py sms_tool/workspace_scan.py` |
| Account liveness and recovery | `accounts/account_liveness.py`, `accounts/account_recovery.py`, `accounts/recovery_batch.py`, `accounts/account_scan.py`, `accounts/account_promotion.py`, `accounts/account_payment_eligibility.py`, `accounts/account_terminal.py`, `promotion_states.py` | Canonical side-effect-free quota probe, explicit OAuth recovery/persistence, batch account scan, the plan/promotion (优惠) probe, and the post-registration payment-eligibility probe (one Checkout + Stripe init per account, reading the full `payment_method_types` list); `accounts/account_terminal.py` (terminal-status vocabulary) and `promotion_states.py` (promotion machine state plus the dependency-free payment-badge formatting rule) are the dependency-free cross-layer vocabularies; `account_payment_eligibility.py` is a thin wrapper over `payment_capability.py` so `account_promotion.py` never imports the five provider modules directly; does not switch Workspace state. | `git ls-files sms_tool/accounts/account_liveness.py sms_tool/accounts/account_recovery.py sms_tool/accounts/recovery_batch.py sms_tool/accounts/account_scan.py sms_tool/accounts/account_promotion.py sms_tool/accounts/account_payment_eligibility.py sms_tool/accounts/account_terminal.py sms_tool/promotion_states.py` |
| Account cleanup | `accounts/account_cleanup.py`, `scripts/cleanup_invalid_accounts.py`, `scripts/mailbox_pool_orphans.py` | Classify only terminal dropped/deactivated/missing-AT/token-invalid rows and archive/delete their local representations; unknown transport results are retained. `mailbox_pool_orphans.py` is a read-only pool/DB/session reconciliation report that prunes only no-session orphans with `--apply` (dry-run + pool backup by default). | `git ls-files sms_tool/accounts/account_cleanup.py scripts/cleanup_invalid_accounts.py scripts/mailbox_pool_orphans.py` |
| Account 2FA batch | `scripts/batch_enable_2fa.py`, `scripts/export_mailbox_2fa.py` | Operator-side batch TOTP enrollment over the local account pool (each secret is fsynced to disk before activation) and the `Email----接码URL----2FA` delivery export, which also marks rows whose 2FA stayed empty instead of leaving them blank. Enrollment needs recent re-authentication; terminal-error markers plus `--retry-terminal` stop dead mailboxes from re-occupying the queue head every round. | `git ls-files scripts/batch_enable_2fa.py scripts/export_mailbox_2fa.py` |
| Payment links and capability | `payment_link_manager.py`, `payment_auth.py`, `checkout_contract.py`, `payment_capability.py`, `payment_flow.py`, `payment_routing.py`, `payment_executor.py`, `gen_pp_link.py`, `wallet_provider.py`, `wallet_transport.py`, `gcash_provider.py`, `gcash_transport.py`, `paypal_proxy.py`, `paypal_reverse.py`, `regional_payment_adapter.py` | JIT AT gate, canonical Checkout/Stripe init contract, shared stage vocabulary, immutable method-owned route plans, common execution states, provider-aware side-effect-limited probing, unified terminal results, native/shared-wallet/custom-payment adapters, offline-verifiable regional-method adapters, link reuse, and stage proxy resolution. Promotion/Update is supported by PayPal and by GoPay full/probe zero-due flows; see [`paypal-zero-due-link.md`](paypal-zero-due-link.md) for PayPal details. | `git ls-files sms_tool/payment_link_manager.py sms_tool/payment_auth.py sms_tool/checkout_contract.py sms_tool/payment_capability.py sms_tool/payment_flow.py sms_tool/payment_routing.py sms_tool/payment_executor.py sms_tool/gen_pp_link.py sms_tool/wallet_provider.py sms_tool/wallet_transport.py sms_tool/gcash_provider.py sms_tool/gcash_transport.py sms_tool/paypal_proxy.py sms_tool/paypal_reverse.py sms_tool/regional_payment_adapter.py` |
| Payment-link state machine | `pay_link/` (`__init__.py`, `base.py`, `core.py`, `adapters.py`, `normalize.py`, `registry.py`, `persistence.py`) | Mechanical split of the former `payment_link_manager.py` (bodies unchanged): the `PaymentMethodSpec` registry, the extraction state machine, method adapters, name/alias normalization, and link persistence. `__init__.py` re-exports the former public surface. One-way acyclic layering inside the package; the same-name rule covers `pay_link/registry.payment_method_label` (Rule 19). The package-root `payment_link_manager.py` is the back-compat shim. | `git ls-files "sms_tool/pay_link/*.py" sms_tool/payment_link_manager.py` |
| Payment batch execution | `payment_batch.py` | Stable email cohorts, JIT refresh, capability-aware eligibility matrix, method concurrency, canary pause, classified retry, and atomic token-free checkpoints under `runtime/payment_batches/`. | `git ls-files sms_tool/payment_batch.py` |
| Payment execution and reconciliation | `paypal/` (`orchestrator.py`, `flow_steps.py`, `form_steps.py`, `session.py`, `dom_fields.py`, `config_picker.py`, `errors.py`), `paypal_auto.py`, `paypal_protocol.py`, `paypal_reconciliation.py`, `nodriver_paypal.py`, `omakse_client.py` | Execute explicit payment commands or independently reconcile an allowlisted PayPal merchant return; reconciliation does not alter the payment-link interface. `paypal/` splits browser automation into seven layers (orchestration, flow, form, session, DOM, config, errors) with a one-way dependency direction; `paypal_auto.py` is only a back-compat re-export shim. | `git ls-files "sms_tool/paypal/*.py" sms_tool/paypal_auto.py sms_tool/paypal_protocol.py sms_tool/paypal_reconciliation.py sms_tool/nodriver_paypal.py sms_tool/omakse_client.py` |
| Account data/import/export | `accounts/account_seed.py`, `storage.py`, `codex_export.py`, `cpa_import.py`, `sub2api_import.py`, `session_converter.py`, `import_targets.py` | Normalize account/session state, convert between formats, and upload to external import targets (CPA, SUB2API); CPA import does not own local liveness or recovery. | `git ls-files sms_tool/accounts/account_seed.py sms_tool/storage.py sms_tool/codex_export.py sms_tool/cpa_import.py sms_tool/sub2api_import.py sms_tool/session_converter.py sms_tool/import_targets.py` |
| Email change workflow | `accounts/account_email_change.py`, `commands/email_change.py`, `storage.py` | Allocate target mailboxes, perform eligibility/begin/OTP/verify, relogin and liveness verification, then atomically migrate the account/session identity. CLI adapter only maps arguments; storage owns the final migration transaction. | `git ls-files sms_tool/accounts/account_email_change.py sms_tool/commands/email_change.py sms_tool/storage.py` |
| Desktop read transport | `desktop_read.py`, `desktop_serve.py` and `SmsWorkbench/DesktopReadClient.cs` | Sanitized account/mailbox read contracts, resident request-ID-correlated JSONL transport, one-shot fallback, process restart, and file-metadata caches. No registration or payment mutation. | `git ls-files sms_tool/desktop_read.py sms_tool/desktop_serve.py SmsWorkbench/DesktopReadClient.cs` |
| Persistence | `storage.py` (back-compat shell), `store/` (`__init__.py`, `connection.py`, `accounts.py`, `markers.py`, `checkpoints.py`, `normalize.py`, `constants.py`, `environment_ledger.py`) | Owns the SQLite schema/connection, account and session facts, quota/promotion markers, registration checkpoints, and the TTL'd egress/fingerprint environment ledger. `storage.py` is an 8-line `from .store import *` shell kept so `patch.object(storage, "database_path", …)` keeps redirecting internal callers. No vendor protocol calls and no HTTP client (Rule 2). `store/connection.py` reaching back into `sms_tool.storage` is a **deliberate** reverse dependency — read its inline comment before "cleaning it up". | `git ls-files sms_tool/storage.py "sms_tool/store/*.py"` |
| Proxy exit-geo resolution | `geo/` (`resolver.py`, `profiles.py`, `clock.py`, `__init__.py`) | Single authority for exit-country detection: one cache, one endpoint list, one precedence chain (`hint → probe → empty`), the canonical per-market profile table, and IANA-timezone→clock facts. Replaced three former geo implementations (protocol path, browser path, fingerprint pool). Do not add a fourth — call `shared_geo_resolver()`. | `git ls-files "sms_tool/geo/*.py"` |
| Shared utilities | `http_client.py`, `captcha_solver.py`, `nodriver_captcha.py`, `proxy_pool.py`, `promotion_states.py`, `doctor.py`, `utils.py`, `timeouts.py`, `driver_env.py`, `operator_output.py`, `stdout_mirror.py` | Reusable transport/browser/helper logic, the dependency-free promotion-state vocabulary consumed across layers, shared timeout constants, the registration-driver environment overlay, the single operator-output seam, stdout/stderr mirroring, and offline environment diagnostics with minimal state ownership. `operator_output.py` is the **one** call that feeds both output channels — do not re-introduce a second print path. `timeouts.py` is the constant source; do not re-inline literals. | `git ls-files sms_tool/http_client.py sms_tool/captcha_solver.py sms_tool/nodriver_captcha.py sms_tool/proxy_pool.py sms_tool/promotion_states.py sms_tool/doctor.py sms_tool/utils.py sms_tool/timeouts.py sms_tool/driver_env.py sms_tool/operator_output.py sms_tool/stdout_mirror.py` |

### Module-group coverage (recompute)

Every row above carries the command that enumerates **exactly** the files it
claims, so membership is a measurement rather than a reading of the prose. Two
conventions matter:

- **Quote globs with double quotes.** The quoted form is the one that works in
  both Git Bash and `cmd.exe`; a *single*-quoted glob is passed through
  literally by `cmd.exe` and silently matches nothing, which reads as "this
  group has no files" rather than "this command did not run".
- **Match by glob, not by literal filename.** Rows legitimately describe
  membership with patterns such as `providers/mailbox_*.py`. Matching those
  literally against the file list produced 131 spurious "undocumented module"
  hits in the 2026-09-22 scan, and that number was discarded for exactly that
  reason. The commands below replace it.

```bash
# files claimed by at least one row  (scratch files go to /tmp, never the repo root)
grep -o '`git ls-files [^`]*`' docs/directory-map.md | tr -d '`' \
  | while IFS= read -r c; do eval "$c"; done | sort -u > /tmp/claimed.txt
# files actually tracked under sms_tool/
git ls-files "sms_tool/*.py" "sms_tool/**/*.py" | sort > /tmp/tracked.txt
# (a) no row names these -- the coverage gap
comm -13 /tmp/claimed.txt /tmp/tracked.txt
# (b) named by more than one row -- the ambiguous ones
grep -o '`git ls-files [^`]*`' docs/directory-map.md | tr -d '`' \
  | while IFS= read -r c; do eval "$c"; done | sort | uniq -d
```

**Measured 2026-09-22** (after this table was normalised from a mixed 3/4-column
layout to four columns):

| Quantity | Value |
| --- | --- |
| Tracked `sms_tool/**.py` | 240 |
| Named by at least one row | 180 (**75%**) |
| Named by no row | **60** |
| Named by more than one row | 5 (`storage.py` ×3; `commands/email_change.py`, `payment_link_manager.py`, `promotion_states.py`, `sub2api_import.py` ×2) |

⚠️ **The table is not exhaustive and this section does not pretend it is.** The
60 unnamed files are real modules with real owners (for example `sentinel/`
×4, `proxy_routing.py`, `paypal_link/` ×3, `desktop_ipc.py`, `sanitizer.py`,
`upi_link.py`); they are simply not yet attributed to a row. Do not quote a
coverage percentage as a gate until that list is empty — attribute them first,
then the number becomes meaningful.

## `SmsWorkbench/` payment command boundary

| File | Responsibility |
| --- | --- |
| `MainWindow.Payment.cs` | Read control state, invoke the payment-link seam, and apply the returned view state. |
| `ProtocolPaymentExecution.cs` | Build deterministic backend command plans and convert backend JSON into presentation models; contains no WPF control access. |
| `PaymentBatchService.cs`, `PaymentBatchViewModel.cs` | Batch dialog execution and state; do not duplicate single-account command planning. |
| `PaymentMethods.cs` | Canonical desktop payment-method catalog, aliases, countries, and single/batch availability. |
| `AccountGridPresentation.cs` | Promotion status classification plus full filtered-set ordering before pagination. |

## `SmsWorkbench/` mailbox-change boundary

| File | Responsibility |
| --- | --- |
| `ChangeEmailDialogService.cs` | Build and validate the provider/concurrency/credential selection dialog; no backend calls or account mutation. |
| `MainWindow.ContextMenu.cs` | Resolve selected account rows and invoke the backend command; no provider allocation or ChatGPT protocol implementation. |
| `BackendCommandPlanner.cs` | Build the deterministic `--change-email` command and temporary account list. |

## `SmsWorkbench/` shell presentation boundary

| File | Responsibility |
| --- | --- |
| `MainWindow.Theme.cs`, `WindowThemeService.cs` | Apply application resource colors and propagate the selected theme to open windows. |
| `MainWindow.Sidebar.cs` | Sidebar collapsed state, icon state and the bounded width animation only. |
| `MainWindow.WindowChrome.cs` | Custom title-bar drag, minimize, maximize and close commands only. |

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
| `services/protocol-payment/` | Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo subprocess extractors. `common/` holds what they share: `protocol_core.py` owns the exactly-once, redacted `protocol_payment.v1` terminal reporter used by iDEAL/BLIK/TWINT; `file_loading.py` (credentials), `http_dump.py` (request/response dumps), `stripe_flow.py` (confirm/redirect), `timeouts.py` (constants), plus `endpoints.py`, `extractor_helpers.py`, `geo.py`, `logging_setup.py`, `redaction.py` and `proxy_bookkeeping.py`/`proxy_selection.py`/`proxy_url.py`. GoPay/GrabPay share a Python adapter under `sms_tool/`; GCash uses its own Python custom-payment adapter under `sms_tool/`. |
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
