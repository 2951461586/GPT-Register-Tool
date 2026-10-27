# Directory Map

This file classifies the repository by responsibility. It is intentionally about
physical placement; `docs/architecture.md` defines the behavioral boundaries.

## Top-level source directories

| Path | Classification | Owner / responsibility | Notes |
| --- | --- | --- | --- |
| `sms_tool/` | Python application core | CLI orchestration, mailbox handling, registration, payment links, payment adapters, storage, account scans, and terminal account cleanup rules | Keep command-specific imports lazy in `sms_tool.cli`. |
| `SmsWorkbench/` | Desktop UI | WPF launcher, account grid, themed dialogs, selected-email seam, account liveness, mailbox-change dialog, batch protocol-payment dialog, fixed non-payment proxy launcher, read-only SMS provider catalog adapter, local command planning/result presentation, the `Themes/` resource dictionaries that `App.xaml` merges (theme-independent `Tokens.xaml` + `Glyphs.xaml`, the swappable `Colors.Light/Dark.xaml` schemes, and the `Controls.xaml` overrides), desktop publish scripts | UI starts CLI commands; payment and mailbox-change protocol logic stays in `sms_tool`. |
| `SmsWorkbench.Contracts/` | Desktop contracts | Window-independent backend command plans, JSON/protocol contracts, progress events, result types, and the embedded `sensitive_policy.json` redaction policy | Referenced by the WPF app and desktop tests; keep WPF controls and protocol implementation out. **The backend CLI flag vocabulary belongs here** (`BackendCommandPlanner`): window partials resolve settings and call `CreateXxx` / `AppendProxy` / `AppendSessionFile` instead of adding `"--flag"` literals, and `tests/workbench/test_backend_cli_contract.py` only checks C#→Python drift — it cannot see a flag that moved into the window. `SmsWorkbench.Contracts.csproj` is the **sole** embedder of `sensitive_policy.json` (as `SmsWorkbench.sensitive_policy.json`): `SensitiveDataSanitizer` reads it via `Assembly.GetExecutingAssembly()` (its own manifest), so the embed must live in the project that compiles that file. `scripts/sensitive_field_scan.py` resolves the pair rather than hard-coding a path. |
| `services/` | Local provider services | Optional mailbox and payment-protocol helpers used by CLI/UI | Services expose explicit process/API boundaries and should not write account SQLite directly. |
| `tests/` | Offline verification | Unit tests for module seams and persistence semantics, grouped by domain: `registration/`, `payment/` (incl. PayPal/UPI/wallet), `accounts/`, `mailbox/`, `proxy/` (incl. geo/http), `config/` (incl. paths/logging/sanitizer), `workbench/` (desktop IPC/CLI contracts), `governance/` (ratchets, guards, docs gates). `tests/` itself keeps only `conftest.py`, the shared replay/fake helper modules and `fixtures/`. | Live vendor/browser tests must be opt-in. A new test file belongs in the domain directory of the module it pins; paths to the repo root are `Path(__file__).resolve().parents[2]`. |
| `docs/` | Source-owned documentation | Architecture, boundaries, directory map, and operating notes | Do not place runtime logs or screenshots here unless deliberately curated. |
| `scripts/` | Operator scripts | Small launch/setup helpers that call source modules or local services | Keep scripts idempotent and repository-relative. `scripts/at_survival_watch.py` is the read-only access-token liveness-curve recorder (`--ids --label --schedule-minutes`); its raw `runtime/at_survival_*.jsonl` output is ignored scratch and is summarised in `docs/audits/`, never committed. One-off probes belong in ignored `runtime/tmp/` and are deleted when the round ends. |
| `local/` | Local operator notes | Machine-local notes deliberately kept in-tree (currently only `local/README.md`) | **Tracked**, so it must stay secret-free; personal scratch state belongs under ignored `runtime/` instead. |
| `.githooks/` | Commit gates | `pre-commit` runs five gates, in this order: `precommit_guard.py` (credentials and runtime-state dumps), `line_ending_guard.py` (a file that mixes CRLF and LF internally), `mailbox_private_import_ratchet.py` (hub private-surface consumers), `sentinel_asset_guard.py` (vendored Sentinel asset digest pin), `format_guard.py` (the protocol-registration lane stays `ruff format`-clean) | Installed by `scripts/install_git_hooks.py` (sets `core.hooksPath`). Stdlib-only on purpose — it must run before dependencies are installed. The set and order above are pinned by `tests/governance/test_gate_list_consistency.py`, which also records which gates have no CI or test coverage. |
| `.github/` | CI | `workflows/ci.yml` holds the authoritative gate list for the Python, .NET and schema checks. It includes an **order-dependence shard** (`pytest -q tests/registration tests/workbench`) because the full-suite order cannot see a lazy-dependency latch that an earlier test already populated. | Keep in sync with `.githooks/pre-commit` and [architecture.md](architecture.md#boundary-rule-checks). |

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
| `runtime/` | SQLite index, derived caches (`cache/config_usage.json`), logs, debug output | Never commit; summarize redacted state only. Cache files are mtime-keyed derivatives: deleting one only forces a rescan. |
| `dist/` | Published WPF executable and installer assets | Rebuild with `SmsWorkbench/build_dotnet.ps1` or `scripts/build_installer.ps1`; do not commit. |
| `.dotnet/` | Local bundled/runtime SDK | Local machine dependency; do not commit. |
| `__pycache__/`, `*.pyc` | Python bytecode | Delete or ignore. |
| `.pytest_cache/`, `TestResults/`, `*.trx`, coverage output | Test-run output | Delete or ignore; never use as release evidence. |
| `SmsWorkbench/bin/`, `SmsWorkbench/obj/`, `SmsWorkbench.Contracts/bin/`, `SmsWorkbench.Contracts/obj/`, `tests/**/bin/`, `tests/**/obj/` | .NET build intermediates | Rebuild from source; never commit. |
| `.workbuddy-ai/`, IDE metadata | Tool-local metadata | Delete or ignore; project decisions belong in source-owned docs. |
| `.zcode/`, root `gates/` | Obsolete generated state | Delete or ignore. Active process-lock slots are generated only under ignored `runtime/gates/`. |

## `sms_tool/` module groups

| Group | Files | Boundary |
| --- | --- | --- |
| Kernel | `kernel/` (`__init__.py`, `paths.py`, `sanitizer.py`, `timeouts.py`, `env_loader.py`, `driver_env.py`, `failure_registry.py`, `error_classification.py`) | Modules that may import only the stdlib and each other; no application module may be imported here (module-level or function-local). `config/` is deliberately **not** in the kernel until `validate_config`'s driver vocabulary is extracted. Check: `python scripts/kernel_boundary_ratchet.py`. |
| Entrypoints/config | `__main__.py`, `cli.py`, `config/`, `paths.py`, `commands/helpers.py` | Parse global options and resolve config/paths; no vendor protocol implementation. |
| Package entrypoints | `__init__.py`, `commands/__init__.py`, `providers/__init__.py` | Package markers for the application, CLI adapters, and provider implementations; keep workflow and wire-protocol behavior in their named modules. |
| Configuration and environment inspection | `env_loader.py`, `config_usage.py` | Load optional environment values without overriding exported variables, and report configuration keys not referenced by source; neither owns provider credentials or persistence. |
| CLI argument groups | `cli_parsers/` (`core.py`, `payment.py`, `paypal.py`, `quota.py`, `session.py`, `codex.py`, `email_change.py`, `omakse.py`, `one_click.py`, `sub2api.py`) | One module per command family; each exposes `register(parser)` holding **only** `add_argument` declarations, so `cli.build_parser` stays a list of `register` calls. Details: [CLI argument groups](#cli-argument-groups). |
| CLI command adapters | `commands/payment.py`, `commands/payment_links.py`, `commands/registration.py`, `commands/accounts.py`, `commands/email_change.py`, `commands/mailbox_ops.py`, `commands/one_click.py`, `commands/omakse.py`, `commands/proxy_admin.py` | Translate parsed CLI arguments into domain workflow requests and process exit codes; replaceable hooks arrive through explicit frozen context dataclasses or keyword-injected adapters. No provider wire protocol or persistence implementation. `cli.py` retains parsing and thin dispatch only. |
| Mailbox and phone inventory | `mailbox/router.py`, `mailbox/service.py`, `mailbox/strategies.py`, `mailbox/types.py`, `mailbox/parsers.py`, `providers/mailbox_*.py`, `providers/*_client.py` (`cfworker_client`, `smailr_client`, `outlook_imap_client`), `providers/ports.py`, `mail_otp.py`, `smsbower.py`, `nexsms.py`, `phone_reuse.py`, `phone_config.py`, `phone_pool.py`, `phone_provider_lifecycle.py`, `phone_proxy.py`, `sms_provider_adapter.py`, `sms_providers.py` | Acquire/poll mailboxes or phone activations. The immutable registry resolves fetch, poll and credential capabilities; `mailbox/errors.py` owns terminal polling classification. Details: [Mailbox and phone inventory](#mailbox-and-phone-inventory). |
| Mailbox facades and polling primitives | `mailbox/cfworker.py`, `mailbox/gmail.py`, `mailbox/graph.py`, `mailbox/icloud_url.py`, `mailbox/remail.py`, `mailbox/smailr.py`, `outlook_imap.py` (facades) · `mailbox/errors.py`, `mailbox/poll.py`, `mailbox/quarantine.py` (real modules) | The seven top-level provider files are **compatibility facades only** — every implementation lives in `providers/mailbox_*`; add behaviour there, not here. Details: [Mailbox facades and polling primitives](#mailbox-facades-and-polling-primitives). |
| Browser registration drivers | `registration_drivers/` (`base.py`, `external_sessions/`, `platform_patches.py`, `browser_session.py`, `browser_flow/`, `playwright.py` shell, `stealth.py`, driver wrappers), `browser_pool.py`, `browser_fingerprint_pool.py`, `fingerprint_pool.py` | Session factory and managed lifecycle, profile configuration and egress probe are separated inside `external_sessions/`. `browser_flow/` owns page state and forms. `base.py` owns the registry. Tests patch the defining module or inject the public factory. |
| Registration state and policy | `registration/operations.py`, `registration/runtime.py`, `registration/policy.py`, `registration/funnel.py`, `telemetry.py` | Immutable per-invocation dependency binding, six state groups with flat compatibility properties, shared retry/advice decisions, token-free batch/trial denominators, and versioned command/run correlation. Persistent state schemas remain unchanged. |
| Registration failure and batch safety | `failure_registry.py`, `batch_circuit_breaker.py` | Centralize failure-class vocabulary and stop a batch after repeated environment failures; per-account retry decisions and proxy health stay with their existing owners. |
| Registration checkpoints, helpers and completion | `registration/checkpoint.py`, `registration/finalize.py`, `registration/persistence.py`, `registration/stage_runner.py`, `registration/protocol_helpers.py`, `registration/flags.py`, `registration/otp_stages.py`, `registration/account_stages.py`, `registration/edge_challenge.py`, `registration/resume.py`, `registration/sentinel_stages.py`, `registration/warmup.py`, `registration/reference_lane.py` | Define resumable post-create checkpoint payloads and persistence, then assemble final results and enroll credentials. Details: [Registration checkpoints, helpers and completion](#registration-checkpoints-helpers-and-completion). |
| Registration browser support | `chatgpt_bootstrap.py`, `humanize.py`, `page_truth.py`, `browser_profile_reclaim.py` | Warm up anonymous/authenticated browser sessions, pace human-like interactions, record machine-readable page evidence alongside debug screenshots, and diagnose/reclaim stale browser profile holders. |
| Sentinel issuance | `sentinel/` (`__init__.py`, `bundle.py`, `client.py`, `runner.py`) | Validate the vendored runtime bundle, invoke the Node runner, and expose shared Sentinel token issuance for registration and recovery; the top-level `sentinel_tokens.py` remains with registration/auth. |
| OAuth and auth HTTP helpers | `codex_oauth.py`, `http_utils.py` | Handle Codex OAuth token collection/refresh and share OpenAI-auth HTTP helpers without duplicating protocol parsing across registration and account flows. |
| Registration/auth | `registration/orchestrator.py` (entry points + operations binding), `registration/handlers.py`, `registration/state.py`, `registration/progress.py`, `registration/concurrency.py`, `cross_process_gate.py`, `registration/outcome.py`, `registration/result.py`, `registration/preflight.py`, `registration/probe.py`, `registration/retry_guard.py`, `registration/cancel.py`, `registration/pulse.py`, `session_builder.py`, `accounts/account_2fa.py`, `auth_flow/` (package), `auth_headers.py`, `auth_wire.py`, `account_creation.py`, `batch_runner.py`, `sentinel_tokens.py`, `otp_strategy.py`, `auth_state.py`, `error_classification.py`, `codex_sentinel.py`, `codex_phone.py`, `phone_registration.py`, `session_refresh.py` | ChatGPT/OpenAI auth, OTP, Sentinel, session refresh, optional phone verification, progress persistence, in-process stage resource gates plus OS file-lock slots shared by desktop/CLI processes, cooperative cancellation, pulse-wave batch scheduling, result judgment, the shared result contract (`registration_result.build_registration_result`), canonical session assembly, and TOTP 2FA enrollment. Details: [Registration/auth](#registrationauth). |
| Agent Identity / explicit import | `agent_identity.py`, `sub2api_import.py` | Ed25519 credential conversion for explicit SUB2API import; not called by the registration pipeline. Keys are persisted under `sessions/agent_identities/`. |
| Workspace compatibility | `k12_client.py`, `k12_identity.py`, `workspace_scan.py` | Legacy explicit Workspace helpers retained for Python callers; the CLI account scan no longer enables this path. |
| Account liveness and recovery | `accounts/account_liveness.py`, `accounts/wham_usage.py`, `accounts/account_recovery.py`, `accounts/recovery_batch.py`, `accounts/account_scan.py`, `accounts/account_promotion.py`, `accounts/promotion_batch.py`, `accounts/account_terminal.py`, `promotion_states.py` | Canonical side-effect-free quota probe (`account_liveness.py`) with its pure quota parsing/formatting split into `accounts/wham_usage.py`, explicit OAuth recovery/persistence, batch account scan, and the separate plan/promotion (优惠) check. Details: [Account liveness and recovery](#account-liveness-and-recovery). |
| Account cleanup | `accounts/account_cleanup.py`, `scripts/cleanup_invalid_accounts.py`, `scripts/mailbox_pool_orphans.py` | Classify only terminal dropped/deactivated/missing-AT/token-invalid rows and archive/delete their local representations; unknown transport results are retained. Details: [Account cleanup](#account-cleanup). |
| Account 2FA batch | `scripts/batch_enable_2fa.py`, `scripts/export_mailbox_2fa.py` | Operator-side batch TOTP enrollment over the local account pool (each secret is fsynced to disk before activation) and the `Email----接码URL----2FA` delivery export, which also marks rows whose 2FA stayed empty instead of leaving them blank. Details: [Account 2FA batch](#account-2fa-batch). |
| Payment links and capability | `payment/link_manager.py`, `payment/auth.py`, `checkout_contract.py`, `capability_browser.py`, `payment/capability.py`, `payment/flow.py`, `payment/routing.py`, `payment/executor.py`, `gen_pp_link.py`, `wallet_provider.py`, `wallet_transport.py`, `gcash_provider.py`, `gcash_transport.py`, `paypal/proxy.py`, `paypal/reverse.py`, `regional_payment_adapter.py` | JIT AT gate, canonical Checkout/Stripe init contract, shared stage vocabulary, immutable method-owned route plans, common execution states, provider-aware side-effect-limited probing, unified terminal results, native/shared-wallet/custom-payment adapters, offline-verifiable regional-method adapters, link reuse, and stage proxy resolution. Details: [Payment links and capability](#payment-links-and-capability). |
| Payment-link state machine | `pay_link/` (`__init__.py`, `base.py`, `core.py`, `adapters.py`, `normalize.py`, `registry.py`, `persistence.py`) | Mechanical split of the former `payment/link_manager.py` (bodies unchanged): the `PaymentMethodSpec` registry, the extraction state machine, method adapters, name/alias normalization, and link persistence. Details: [Payment-link state machine](#payment-link-state-machine). |
| PayPal-only link generation | `paypal_link/` (`__init__.py`, `gen_link.py`, `reconciliation.py`), `pp_link_helpers.py` | Implement PayPal Checkout link extraction and merchant-return reconciliation behind the `gen_pp_link.py` / `paypal/reconciliation.py` compatibility shims; shared proxy, URL, and billing helpers live in `pp_link_helpers.py`. Distinct from the multi-method `pay_link/` registry. |
| UPI link and QR extraction | `upi_link/` (`__init__.py`, `constants.py`, `env.py`, `dump.py`, `config.py`, `session.py`, `browser.py`, `stripe.py`, `sentinel.py`, `extract.py`, `flows.py`, `pipeline.py`, `_extract.py`) | Implement the UPI custom-Checkout flow, payment-page polling, UPI URI extraction, and QR rendering; do not place this provider-specific flow in PayPal link helpers. Details: [UPI link and QR extraction](#upi-link-and-qr-extraction). |
| Payment batch execution | `payment/batch.py` | Stable email cohorts, JIT refresh, capability-aware eligibility matrix, method concurrency, canary pause, classified retry, and atomic token-free checkpoints under `runtime/payment_batches/`. |
| Payment execution and reconciliation | `paypal/` (`orchestrator.py`, `flow_steps.py`, `form_steps.py`, `session.py`, `dom_fields.py`, `config_picker.py`, `errors.py`), `paypal/auto.py`, `paypal/protocol.py`, `paypal/reconciliation.py`, `nodriver_paypal.py`, `omakse_client.py` | Execute explicit payment commands or independently reconcile an allowlisted PayPal merchant return; reconciliation does not alter the payment-link interface. Details: [Payment execution and reconciliation](#payment-execution-and-reconciliation). |
| Payment SMS helpers | `sms_utils.py` | Provide neutral SMS polling/extraction helpers to PayPal automation and reverse flows without circular imports. |
| Account data/import/export | `account_seed.py`, `storage.py`, `codex_export.py`, `cpa_import.py`, `sub2api_import.py`, `session_converter.py`, `import_targets.py` | Normalize account/session state, convert between formats, and upload to external import targets (CPA, SUB2API); CPA import does not own local liveness or recovery. |
| Email change workflow | `accounts/account_email_change.py`, `commands/email_change.py`, `storage.py` | Allocate target mailboxes, perform eligibility/begin/OTP/verify, relogin and liveness verification, then atomically migrate the account/session identity. CLI adapter only maps arguments; storage owns the final migration transaction. |
| Desktop read transport | `desktop_read.py`, `desktop_serve.py` and `SmsWorkbench/Interop/DesktopReadClient.cs` | Sanitized account/mailbox read contracts, resident request-ID-correlated JSONL transport, one-shot fallback, process restart, and file-metadata caches. No registration or payment mutation. |
| Desktop event transport | `desktop_ipc.py` | Normalize stage events and emit framed progress/result JSON over stdout for the desktop host; ordinary log text must not masquerade as protocol frames. |
| Mailbox credential writeback | `mailbox/pool_writer.py` | Persist rotated OAuth refresh tokens back to the mailbox pool with an explicit writeback policy; mailbox polling and provider clients remain in their existing groups. |
| Persistence | `storage.py` (back-compat shell), `store/` (`__init__.py`, `connection.py`, `accounts.py`, `markers.py`, `checkpoints.py`, `normalize.py`, `constants.py`, `environment_ledger.py`) | Owns the SQLite schema/connection, account and session facts, quota/promotion markers, registration checkpoints, and the TTL'd egress/fingerprint environment ledger. Details: [Persistence](#persistence). |
| Account domain core | `accounts/__init__.py`, `accounts/account_models.py`, `accounts/account_identity.py`, `accounts/account_events.py`, `accounts/account_health.py`, `accounts/account_health_queue.py`, `accounts/account_lifecycle.py` | `account_models.py` is the typed account/session boundary used by persistence and workflow results; `account_identity.py` allocates per-account network and protocol identity once at registration and rebuilds it per worker; `account_events.py` publishes post-persistence domain facts **without importing a mailbox provider** (provider side effects stay behind the dispatcher); `account_health.py` is the plan/liveness **result contract** and `account_health_queue.py` the durable deduplicated queue that owns claim / lease / heartbeat / scheduling only — both are documented in [`current/account-health.md`](current/account-health.md), which is also the promotion (优惠) contract; `account_lifecycle.py` holds typed lifecycle operations shared by the CLI and desktop adapters. Details: [Account domain core](#account-domain-core). |
| Payment contracts, catalog and stage gates | `payment/catalog.py`, `payment/contracts.py`, `payment/errors.py`, `payment/adapters.py`, `payment/egress.py`, `operation_journal.py`, `payment/operation.py`, `payment/batch_setup.py`, `payment/wire.py`, `paypal/extract.py`, `paypal/fingerprints.py`, `paypal/authorization.py`, `paypal/authorization_queue.py` | `payment/catalog.py` is the versioned method catalog shared with the desktop; `payment/contracts.py` owns typed result contracts and retry gates; `payment/errors.py` is the typed failure-attribute contract — `annotate_error` rejects unknown attribute names on the writer side and `error_code_of` / `error_stage_of` / `retryable_of` / `retryable_flag` own the read-side names and defaults, shared by `payment_capability`, `payment_executor` and the `pay_link` adapters; `payment/adapters.py` is the typed seam to the subprocess extractors; `payment/egress.py` owns the **strong egress-country gate** — stage proxies are probed through themselves and the observed exit must match the route plan *before* an extractor or capability probe starts; `operation_journal.py` owns the **provider-neutral** durable idempotency and recovery journal (`commands/registration.py` uses it for the same restart-survival reason the payment lane does, which is why it is not payment-named); `payment/operation.py` is its re-export shim; `payment/batch_setup.py` holds pure setup helpers. Details: [Payment contracts, catalog and stage gates](#payment-contracts-catalog-and-stage-gates). |
| Proxy authority, lanes, health and bridge | `proxy/entry.py`, `proxy/routing.py`, `proxy/registry.py`, `proxy/health.py`, `proxy/bridge.py`, `proxy/edge_probe.py` | `proxy/entry.py` is the **single authority** for parsing, rebuilding, retargeting and rotating proxy strings (Boundary Rule 13 — `phone_proxy` and `paypal_proxy` are thin wrappers around it, and a fourth parser is a defect); `proxy/routing.py` owns the explicit lanes (registration / protocol work / account health) and the ordered operation-candidate list that callers must **not** rebuild; `proxy/registry.py` is the unified read/management facade over the registration / mailbox / payment lanes — one vocabulary, one resolve call, one credential-free census, and the optional `proxy.lanes` single declaration point documented in `PROXY_GUIDE.md` (it fronts the resolvers, it does not replace them); `proxy/health.py` is the process-shared health tracker whose writes go through an OS file lock; `proxy/bridge.py` starts the in-process `socks5h://127.0.0.1:<port>` bridge for a single upstream when a browser needs a local endpoint; `proxy/edge_probe.py` is the read-only, credential-redacting probe that answers the question the tunnel-only pool health check cannot — whether an exit is served or Cloudflare-blocked at the ChatGPT login edge (`scripts/proxy_pool_probe.py` runs it over a whole pool; `verify_proxy.py --chatgpt` over the configured proxies). Details: [Proxy authority, lanes, health and bridge](#proxy-authority-lanes-health-and-bridge). |
| Proxy exit-geo resolution | `geo/` (`resolver.py`, `profiles.py`, `clock.py`, `__init__.py`) | Single authority for exit-country detection: one cache, one endpoint list, one precedence chain (`hint → probe → empty`), the canonical per-market profile table, and IANA-timezone→clock facts. Details: [Proxy exit-geo resolution](#proxy-exit-geo-resolution). |
| Shared utilities | `endpoints.py`, `http_client.py`, `captcha_solver.py`, `nodriver_captcha.py`, `proxy/pool.py`, `promotion_states.py`, `doctor.py`, `utils.py`, `timeouts.py`, `driver_env.py`, `operator_output.py`, `stdout_mirror.py` | Reusable transport/browser/helper logic, the dependency-free promotion-state vocabulary consumed across layers, shared timeout constants, the registration-driver environment overlay, the single operator-output seam, stdout/stderr mirroring, and offline environment diagnostics with minimal state ownership. Details: [Shared utilities](#shared-utilities). |
| Transport retry primitives | `backoff.py` | Dependency-free bounded cooldown and exponential transport backoff shared by HTTP transport and registration policy; registration-specific advice stays in `registration/policy.py`. |
| Sensitive output and telemetry | `sanitizer.py`, `diagnostics.py`, `logging_setup.py`, `token_telemetry.py` | Centralize redaction of operator-visible data, safe stdout/stderr, correlated logging, and token-free timing metadata; do not log credential values. |

### Module-group coverage

The table above is the **human** half: which modules form a group, in prose, and
the boundary each group owns. The **machine** half lives in
`scripts/directory_map_groups.json`, one entry per row, each a list of
`git ls-files` pathspecs.

Keeping the two apart is deliberate. The claims used to be a `git ls-files ...`
command inside every row, so this document carried a 41-line copy of the
inventory whose only job was to be re-run -- and any row whose command drifted
from its prose was invisible until someone ran all 41 by hand.

The gate reads the JSON, resolves each pathspec through Git (posix paths, no
shell), and fails on two different defects:

* a tracked `sms_tool/**/*.py` module claimed by **no** group (reverse coverage,
  so a new module cannot land without a home here);
* a group that exists in only one half -- a JSON entry with no row, or a row
  with no JSON entry -- so the table cannot grow a category the gate never
  measures.

```
python scripts/module_coverage_check.py --detail
```

Untracked files do **not** enter the denominator or the claims: an untracked
scratch module is not part of the source inventory yet. Git pathspec `*` can
match `/`, unlike shell or `pathlib` glob `*`; pathspecs are passed to Git
directly (never through a shell), and the explicit-list groups exist to avoid
cross-tool expansion drift. Cross-row claims are reported but are not an error.
`tests/governance/test_module_coverage.py` pins the parser, the unclaimed
failure, the group-symmetry rule and the real tree; CI runs the same script.

**Current counts are computed, not copied into this document.** Run
`python scripts/module_coverage_check.py` for tracked `sms_tool` Python files,
claimed/unclaimed files, group count and cross-row claims; add `--detail` for
each claimed module and the groups that claim it. The reported denominator excludes incidental
`scripts/` and `SmsWorkbench/` paths named by a row. An unclaimed tracked module
fails the gate; cross-row claims are reported, not rejected, because some
workflow and implementation rows intentionally overlap. This is a tracked-file
placement inventory, **not** a check of untracked modules or a behavioral
architecture gate.

### Module-group boundary notes

Full boundary text for the module-group rows whose table cell is abbreviated.
The table above keeps only the first sentence so it stays scannable; every
rule, naming contract and pointer lives here unchanged.

#### CLI argument groups

One module per command family; each exposes `register(parser)` holding **only** `add_argument` declarations, so `cli.build_parser` stays a list of `register` calls. Declaration only — no dispatch, no post-parse validation, no provider imports. Importing a parser module must not pull in the command it describes (Rule 15).

#### Mailbox and phone inventory

Acquire/poll mailboxes or phone activations. The immutable registry resolves fetch, poll and credential capabilities; `mailbox/errors.py` owns terminal polling classification. All active mailbox provider implementations live under `sms_tool/providers/`; the top-level `mailbox_<provider>` files are aliasing facades only. `mailbox/router.py` itself is **not** one of those facades: it is the implementation and, until 2026-10-21, the private-symbol hub. Its 16 consumed symbols are now public API (`poll_email_otp`, `load_mailbox_pool`, `mailbox_email_config`, ...), the private-import baseline for this module is **0**, and Rule 16 fails on any new private consumer. **Naming contract inside `providers/`:** low-level wire clients end in `_client`, registration-facing flows that compose them are named `mailbox_*`; a `*_client` module must never import a `mailbox_*` module. **SMS-provider naming contract:** `sms_providers.py` is the **single source of truth** for the provider vocabulary (keys, aliases, default endpoints, API-key env names, `client_available`) and `sms_provider_adapter.py` is the 43-line adapter *contract* that consumes it. The `_adapter` suffix is the load-bearing part: add a provider to the registry, add lifecycle surface to the adapter — never the other way round. **Phone-reuse split:** `phone_reuse.py` is the facade and the OpenAI-OTP orchestration; `phone_config.py` owns provider key resolution/diagnostics, `phone_pool.py` owns `PhoneSlot`/`PhonePool` and the atomic cross-process state file, and `phone_provider_lifecycle.py` owns the vendor acquire/prepare/complete/cancel calls. The split exists so the pool and lifecycle do not have to know how a provider key is spelled, and so a vendor-name comparison cannot hide in a 1300-line file. **Two protocol families, one vocabulary:** `smsbower.py` speaks the sms-activate handler family (`protocol=sms_activate_handler`, lifecycle keyed on `activationId`), while `nexsms.py` speaks the NexSMS JSON REST family (`protocol=nexsms_json`, lifecycle keyed on the **phone number** — that vendor issues no `activationId` and has no `setStatus`). `sms_providers.IMPLEMENTED_PROTOCOLS` is the set this repository ships a client for, so the hand-written `client_available` flag must equal the derived `has_client`. `tests/registration/test_sms_provider_registry.py` pins the registry (including that `client_available == has_client` equality), `tests/registration/test_sms_provider_adapter_pure.py` pins the adapter, `tests/workbench/test_settings_catalog_provider_parity.py` pins the C# mirror, and `tests/registration/test_nexsms_client_pure.py` pins the JSON family's wire contract. Chongzhi was removed; unsupported legacy lines are skipped.

#### Mailbox facades and polling primitives

The seven top-level provider files are **compatibility facades only** — every implementation lives in `providers/mailbox_*`; add behaviour there, not here. The other three are not facades: `mailbox/errors.py` owns the provider-independent **terminal-error vocabulary** understood by every OTP poller, `mailbox/poll.py` owns the shared **settle-stability polling loop** (deadline / interval / settle) that `poll_email_otp` and the provider pollers both use, and `mailbox/quarantine.py` owns the **token-free quarantine** for credentials that are no longer usable.

#### Registration checkpoints, helpers and completion

Define resumable post-create checkpoint payloads and persistence, then assemble final results and enroll credentials. The 2026-10-01 split moved the workflow's cohesive periphery here: the persistence seam, the stage executor + `RegistrationAbort`, the pure transport/format/safe-cast helpers, and the send/wait/validate email-OTP trio. The 2026-10-10 consolidation then moved the single `registration.*` boolean-toggle parser here as `registration/flags.py` — a **leaf** module (it imports nothing from `sms_tool`), so the four non-`auth_flow` A/B toggles that read through it add no cross-directory import edge; `registration_protocol_helpers` re-exports it and `auth_flow.steps._registration_flag` deliberately keeps its own copy, because importing the leaf module from `auth_flow` would grow the saturated `sms_tool/auth_flow -> sms_tool` ratchet edge. The 2026-10-08 split moved the in-flow edge-challenge unit here too: `registration/edge_challenge.py` owns the session's `_openai_edge_challenge_hook`, the same-pool exit move, the `proxy_audit` counters and the `clear_session_circuit` invariant, while `http_client` keeps the response, the retry budget and the circuit itself. The same split then moved the two remaining cohesive units: `registration/resume.py` owns checkpoint persistence and the post-create resume stage (the payload *contract* stays in `registration/checkpoint.py`), and `registration/sentinel_stages.py` owns the `sentinel_password_bundle` switch, the password-page bundle and the per-flow issuance (the Node runner and the flow-to-page map stay in `sentinel/`), and `registration/warmup.py` owns the default-off post-signup warm-up + dwell for the protocol lane (14 read-only GETs after the access token is confirmed), and `registration/reference_lane.py` owns the default-off `registration.reference_lane` switch: the preset that runs the reference project's password stage order as one lane (its toggles are forced on), the anonymous ChatGPT bootstrap + `providers` read before `csrf`, and the password-page Sentinel bundle before `user/register`, and `registration/account_stages.py` owns the `about-you` prime, `create_account`, `fetch_auth_session` and `probe_access_token` stage bodies. `registration/handlers.py` still owns stage ordering and re-exports every moved name, so historical imports and `patch(...)` targets resolve.

#### Registration/auth

ChatGPT/OpenAI auth, OTP, Sentinel, session refresh, optional phone verification, progress persistence, in-process stage resource gates plus OS file-lock slots shared by desktop/CLI processes, cooperative cancellation, pulse-wave batch scheduling, result judgment, the shared result contract (`registration_result.build_registration_result`), canonical session assembly, and TOTP 2FA enrollment. `registration/orchestrator.py` is the **entry-point module** (not a package): it resolves runtime config, selects the driver, exposes `run_email` / `run_batch`, and binds the per-invocation `RegistrationOperations`; stage order and stage bodies belong to `registration/handlers.py` and its sibling stage modules. `auth_flow/` is the protocol step package: `steps` is a leaf of predicates/helpers, `signup` and `login` are the two lanes, `otp`/`totp` own the code exchange, `sentinel_flow` owns same-flow token issuance, and `deps` re-exports the external seam so tests patch one place. Submodules call each other module-qualified (`from . import steps; steps._is_about_you_step(...)`), so a patch on the defining submodule is seen by every caller.

#### Account liveness and recovery

Canonical side-effect-free quota probe (`account_liveness.py`) with its pure quota parsing/formatting split into `accounts/wham_usage.py`, explicit OAuth recovery/persistence, batch account scan, and the separate plan/promotion (优惠) check. `account_promotion.py` owns the single-account probe; `accounts/promotion_batch.py` owns the multi-account run (proxy rotation, 429 throttle, persistence and events) so the probe module stays off the desktop read path. The provider handshake lives in `payment/capability.py`, which the payment link lanes use directly and which never creates a payment method or charge. `accounts/account_terminal.py` (terminal-status vocabulary) and `promotion_states.py` (promotion machine state) are cross-layer vocabularies. Account checks do not switch Workspace state.

#### Account cleanup

Classify only terminal dropped/deactivated/missing-AT/token-invalid rows and archive/delete their local representations; unknown transport results are retained. `mailbox_pool_orphans.py` is a read-only pool/DB/session reconciliation report that prunes only no-session orphans with `--apply` (dry-run + pool backup by default).

#### Account 2FA batch

Operator-side batch TOTP enrollment over the local account pool (each secret is fsynced to disk before activation) and the `Email----接码URL----2FA` delivery export, which also marks rows whose 2FA stayed empty instead of leaving them blank. Enrollment needs recent re-authentication; terminal-error markers plus `--retry-terminal` stop dead mailboxes from re-occupying the queue head every round.

#### Payment links and capability

JIT AT gate, canonical Checkout/Stripe init contract, shared stage vocabulary, immutable method-owned route plans, common execution states, provider-aware side-effect-limited probing, unified terminal results, native/shared-wallet/custom-payment adapters, offline-verifiable regional-method adapters, link reuse, and stage proxy resolution. Promotion/Update is supported by PayPal and by GoPay full/probe zero-due flows; see [`paypal-zero-due-link.md`](paypal-zero-due-link.md) for PayPal details.

#### Payment-link state machine

Mechanical split of the former `payment/link_manager.py` (bodies unchanged): the `PaymentMethodSpec` registry, the extraction state machine, method adapters, name/alias normalization, and link persistence. `__init__.py` re-exports the former public surface. One-way acyclic layering inside the package; the same-name rule covers `pay_link/registry.payment_method_label` (Rule 19). The package-root `payment/link_manager.py` is the back-compat shim. **Do not confuse with `paypal_link/`** — this package is the *multi-method* registry (blik / ideal / twint / momo / kakao / pix …); `paypal_link/` is PayPal-only link generation and return reconciliation.

#### UPI link and QR extraction

Implement the UPI custom-Checkout flow, payment-page polling, UPI URI extraction, and QR rendering; do not place this provider-specific flow in PayPal link helpers. Mechanical split of the former `upi_link.py` (bodies unchanged): `__init__.py` is a re-export facade, `pipeline.py` owns the single `generate_upi_qr_link` definition, `_extract.py` is the pure layer, and the remaining submodules are a one-way acyclic dependency stack (`env` → `dump` → `config` → `session` → `browser`/`stripe` → `sentinel` → `extract` → `flows` → `pipeline`).

#### Payment execution and reconciliation

Execute explicit payment commands or independently reconcile an allowlisted PayPal merchant return; reconciliation does not alter the payment-link interface. `paypal/` splits browser automation into seven layers (orchestration, flow, form, session, DOM, config, errors) with a one-way dependency direction; `paypal/auto.py` is only a back-compat re-export shim. **`paypal_link/`** (`gen_link.py`, `reconciliation.py`) holds the PayPal-only bodies behind the two package-root shims `paypal/reconciliation.py` (7 lines) and `gen_pp_link.py` — **do not confuse it with `pay_link/`**, the multi-method registry.

#### Persistence

Owns the SQLite schema/connection, account and session facts, quota/promotion markers, registration checkpoints, and the TTL'd egress/fingerprint environment ledger. `storage.py` is an 8-line `from .store import *` shell kept so `patch.object(storage, "database_path", …)` keeps redirecting internal callers. No vendor protocol calls and no HTTP client (Rule 2). `store/connection.py` reaching back into `sms_tool.storage` is a **deliberate** reverse dependency — read its inline comment before "cleaning it up".

#### Account domain core

`account_models.py` is the typed account/session boundary used by persistence and workflow results; `account_identity.py` allocates per-account network and protocol identity once at registration and rebuilds it per worker; `account_events.py` publishes post-persistence domain facts **without importing a mailbox provider** (provider side effects stay behind the dispatcher); `account_health.py` is the plan/liveness **result contract** and `account_health_queue.py` the durable deduplicated queue that owns claim / lease / heartbeat / scheduling only — both are documented in [`current/account-health.md`](current/account-health.md), which is also the promotion (优惠) contract; `account_lifecycle.py` holds typed lifecycle operations shared by the CLI and desktop adapters. `accounts/__init__.py` records why these sit in a package instead of flat `account_*.py` at the root.

#### Payment contracts, catalog and stage gates

`payment/catalog.py` is the versioned method catalog shared with the desktop; `payment/contracts.py` owns typed result contracts and retry gates; `payment/errors.py` is the typed failure-attribute contract — `annotate_error` rejects unknown attribute names on the writer side and `error_code_of` / `error_stage_of` / `retryable_of` / `retryable_flag` own the read-side names and defaults, shared by `payment_capability`, `payment_executor` and the `pay_link` adapters; `payment/adapters.py` is the typed seam to the subprocess extractors; `payment/egress.py` owns the **strong egress-country gate** — stage proxies are probed through themselves and the observed exit must match the route plan *before* an extractor or capability probe starts; `operation_journal.py` owns the provider-neutral durable idempotency and recovery journal (`commands/registration.py` uses it for the same restart-survival reason the payment lane does, which is why it is not payment-named); `payment/operation.py` is its re-export shim; `payment/batch_setup.py` holds pure setup helpers. `payment/wire.py` is the **cross-lane payment kernel** — the Checkout primitives, the non-Checkout session factory, `CURRENCY_MAP`, the provider-stage order, the failure vocabulary and `_compact_diagnostic`. It is a leaf (inside the package it imports only `auth_headers` and `timeouts`), and `paypal/extract.py` re-exports it rather than redefining it, so a PayPal-named module is no longer the definition site for a primitive the UPI or eligibility lanes need (Boundary Rule 20). PayPal side: `paypal/extract.py` is the three-stage proxy link extractor (moved verbatim out of `gen_pp_link.py`), `paypal/fingerprints.py` centralises the UA/version strings so the PayPal modules cannot drift apart, `paypal/authorization.py` is a read-only authorization-response parser, and `paypal/authorization_queue.py` persists the approval-link → BA hand-off so a restart cannot lose a generated link.

#### Proxy authority, lanes, health and bridge

`proxy/entry.py` is the **single authority** for parsing, rebuilding, retargeting and rotating proxy strings (Boundary Rule 13 — `phone_proxy` and `paypal_proxy` are thin wrappers around it, and a fourth parser is a defect); `proxy/routing.py` owns the explicit lanes (registration / protocol work / account health) and the ordered operation-candidate list that callers must **not** rebuild; `proxy/registry.py` is the unified read/management facade over the registration / mailbox / payment lanes — one vocabulary, one resolve call, one credential-free census, and the optional `proxy.lanes` single declaration point documented in `PROXY_GUIDE.md` (it fronts the resolvers, it does not replace them); `proxy/health.py` is the process-shared health tracker whose writes go through an OS file lock; `proxy/bridge.py` starts the in-process `socks5h://127.0.0.1:<port>` bridge for a single upstream when a browser needs a local endpoint; `proxy/edge_probe.py` is the read-only, credential-redacting probe that answers the question the tunnel-only pool health check cannot — whether an exit is served or Cloudflare-blocked at the ChatGPT login edge (`scripts/proxy_pool_probe.py` runs it over a whole pool; `verify_proxy.py --chatgpt` over the configured proxies).

#### Proxy exit-geo resolution

Single authority for exit-country detection: one cache, one endpoint list, one precedence chain (`hint → probe → empty`), the canonical per-market profile table, and IANA-timezone→clock facts. Replaced three former geo implementations (protocol path, browser path, fingerprint pool). Do not add a fourth — call `shared_geo_resolver()`.

#### Shared utilities

Reusable transport/browser/helper logic, the dependency-free promotion-state vocabulary consumed across layers, shared timeout constants, the registration-driver environment overlay, the single operator-output seam, stdout/stderr mirroring, and offline environment diagnostics with minimal state ownership. `endpoints.py` is the single authority for the OpenAI hosts the client calls (`auth.openai.com` / `chatgpt.com` / `api.openai.com`); import a host from it instead of re-inlining one — `scripts/endpoints_literal_ratchet.py` freezes the remaining inline count per file and only lets it fall. `operator_output.py` is the **one** call that feeds both output channels — do not re-introduce a second print path. `timeouts.py` is the constant source; do not re-inline literals.

## `SmsWorkbench/` payment command boundary

| File | Responsibility |
| --- | --- |
| `MainWindow.Payment.cs` | Read control state, invoke the payment-link seam, and apply the returned view state. |
| `PaymentBatchService.cs`, `PaymentBatchViewModel.cs` | Batch dialog execution and state; do not duplicate single-account command planning. |
| `AccountGridPresentation.cs` | Promotion status classification plus full filtered-set ordering before pagination. |

## `SmsWorkbench/` mailbox-change boundary

| File | Responsibility |
| --- | --- |
| `ChangeEmailDialogService.cs` | Build and validate the provider/concurrency/credential selection dialog; no backend calls or account mutation. |
| `MainWindow.ContextMenu.cs` | Resolve selected account rows and invoke the backend command; no provider allocation or ChatGPT protocol implementation. |
| `SmsWorkbench.Contracts/BackendCommandPlanner.cs` | Build the deterministic `--change-email` command and temporary account list. |

## `SmsWorkbench/` shell presentation boundary

| File | Responsibility |
| --- | --- |
| `MainWindow.Theme.cs`, `WindowThemeService.cs` | Apply application resource colors and propagate the selected theme to open windows. |
| `MainWindow.Sidebar.cs` | Sidebar collapsed state, icon state and the bounded width animation only. |
| `TitleBar.xaml(.cs)` | Custom title-bar drag, minimize, maximize and close commands only; resolves the host window through `Window.GetWindow`, owns no state. |
| `AccountFilterBar.xaml(.cs)` | Account counters plus search/scope/page-size/paging; inherits MainWindow's DataContext, clear-button visibility via `PoolViewModel.HasSearchText`. |
| `Sidebar.xaml(.cs)` | Brand, navigation and collapse button. The column width animation stays on `MainWindow`; `IsCollapsed` drives the visuals and `ToggleRequested` asks the window to flip `SidebarCollapsed`. |
| `AccountGridView.xaml(.cs)` | Account table + context menu. Owns sorting (`PoolViewModel.SortBy`) and row-selection checkbox ticking; exposes `SelectedRow` / `ClearSelection()` and takes the pool through a DP. |
| `WorkbenchConsole.xaml(.cs)` | Task list + log console. Owns scroll-to-last, append-into-TextBox and the clear event; takes `TaskList` / `LogPanel` through DPs. |
| `PoolViewModel.cs` | Account grid state: all rows, search/scope filter, sort, paging, selected row, overview counters and the pager/clear/select-all commands. `PoolRow` stays the row model. |
| `TaskListViewModel.cs`, `LogViewModel.cs`, `StatusBarViewModel.cs` | Batch task rows and the "批次 N" counter, the capped log buffer, and the status-bar line. |
| `ExportViewModel.cs`, `RegisterViewModel.cs` | Window-independent rules behind export/import and registration: row-selection fallbacks, format labels, probe summary, batch count and worker defaults, which rows still need registering. Dialogs and backend calls stay in `MainWindow.Export.cs` / `MainWindow.Register.cs`. |
| `MainWindow.Commands.cs`, `MainWindow.Navigation.cs` | Commands bound from `MainWindow.xaml` and the sidebar action table; each routes to an action method in another partial. |

## Window-independent desktop contracts and interpreters

These types hold the deterministic command-building and backend-result logic that
must stay testable without a WPF window (see placement rule 8). Command plans
and protocol contracts live in `SmsWorkbench.Contracts/`; result interpretation
and task coordination live in `SmsWorkbench/`. `MainWindow.*` partials call
them and only apply the returned view state.

| File | Responsibility |
| --- | --- |
| `SmsWorkbench.Contracts/BackendCommandPlanner.cs` | Build `BackendCommandPlan` argument lists and stable account-result kinds for registration/payment/scan tasks from primitive inputs; no WPF access. |
| `SmsWorkbench.Contracts/PaymentMethods.cs` | Canonical desktop payment-method catalog, aliases, countries and single/batch availability; reads the embedded `payment_methods.json`. |
| `SmsWorkbench.Contracts/ProxyInputNormalizer.cs` | `host:port:user:pass` to proxy-URL normalization and country inference, shared by the command planners. |
| `SmsWorkbench.Contracts/SensitiveDataSanitizer.cs` | C# consumer of the repository `sensitive_policy.json`; log-strength redaction for operator-visible output. |
| `SmsWorkbench/Interop/BackendResultInterpreter.cs` | Interpret backend execution results and proxy-test JSON into typed outcomes (success/timeout/cancelled). Owns account-result routing (`HasAccountResults` / `AccountResultTitle` / `ScanResultError`); presentation lives in `Views/MainWindow.Export.cs::ShowAccountScanResultDialog`. |
| `SmsWorkbench.Contracts/BackendJson.cs`, `SmsWorkbench.Contracts/BackendJsonProtocol.cs` | Canonical JSON-to-dictionary projection and protocol framing shared by interpreters; keep WPF out of JSON plumbing. |
| `SmsWorkbench.Contracts/BackendContracts.cs`, `SmsWorkbench/Services/BackendTaskCoordinator.cs` | Backend output-channel contracts and single-flight task coordination. |
| `SmsWorkbench/Views/AccountStatusInterpreter.cs` | Window-independent account JSON interpretation: plan type, wham quota labels, payment status, deactivation, import state. |

## `services/` module groups

| Path | Boundary |
| --- | --- |
| `services/protocol-payment/` | Vendored iDEAL/PIX/Kakao Pay/BLIK/TWINT/直卡 Checkout/MoMo subprocess extractors. `common/` holds what they share: `protocol_core.py` owns the exactly-once, redacted `protocol_payment.v1` terminal reporter and its `run_extractor_entrypoint` guard, used by all seven extractors; `file_loading.py` (credentials), `http_dump.py` (request/response dumps), `stripe_flow.py` (confirm/redirect), `timeouts.py` (constants), plus `endpoints.py`, `extractor_helpers.py`, `payload_helpers.py` (pure payload helpers: six shared by iDEAL/TWINT/BLIK, plus `configured_country` and the setup-intent error helpers shared by iDEAL/TWINT/Kakao; BLIK keeps its own divergent setup-intent variants), `geo.py`, `logging_setup.py`, `payment_predicates.py` (pure payload/redirect predicates shared by iDEAL/TWINT/BLIK), `provider_profile.py` (per-provider data), `redaction.py`, `proxy_bookkeeping.py`/`proxy_selection.py`/`proxy_url.py`, and `extractor_runtime.py` (provider-neutral pre-proxy normalization and the sticky proxy-chain key shared by iDEAL/TWINT/BLIK; provider-specific env/locale constants stay in their extractor — Rule 19). The 27 deliberate same-named divergences are frozen as a typed inventory in `tests/payment/test_extractor_divergence_contract.py` (file + function + category + reason, plus semantic pins for the pure ones), so a silent convergence fails before it can change a production request. GoPay/GrabPay share a Python adapter under `sms_tool/`; GCash uses its own Python custom-payment adapter under `sms_tool/`. |
| `services/mail-otp-web/` | Standalone Microsoft Graph inbox/OTP helper UI; operator diagnostic service, not the main registration mailbox owner. |

## Placement rules for new work

1. If it is a CLI command, add a lazy handler in `sms_tool.cli` and put the
   implementation in a focused module under `sms_tool/`.
2. If it is a desktop button/dialog, put UI code in `SmsWorkbench/` and call the
   CLI/backend rather than duplicating protocol logic in C#.
   Read-only provider metadata needed before launch belongs in a focused catalog
   module such as `SmsProviderCatalogClient.cs`, not in a `MainWindow` handler.
3. If it talks to a provider, isolate it under `sms_tool/providers/` or
   `services/<provider>/` and expose a small public method.
4. If it extends mailbox/registration/K12 behavior, prefer adding a focused
   module behind the existing compatibility seam (`mailbox/router.py`,
   `registration/orchestrator.py`) rather than growing those seam files.
5. If it persists account state, route through `sms_tool.storage` or a documented
   storage seam.
6. If it is runtime output, put it under `runtime/` or `sessions/`, not in source
   directories.
7. Sidebar actions that require an email must use the selected-email seam and
   the themed `未选择邮箱` dialog; do not call `MessageBox.Show` for that state.
8. Command builders and backend-result parsing must be testable without a WPF
   window. Keep those deterministic parts outside `MainWindow.*` code-behind.
9. A new Python module must **not** land in the `sms_tool/` package root.
   `scripts/root_module_ratchet.py` freezes the root set at 155 names; a new file
   belongs in the domain subpackage that owns it (`registration_drivers/`,
   `store/`, `providers/`, `commands/`, …). Adding a root name needs a decision
   recorded in `docs/architecture.md` and a baseline edit.
10. A new C# file goes into its role folder: `Views/` (windows, `MainWindow.*`
   partials, presentation), `ViewModels/`, `Services/` (workflow + persistence
   adapters), `Interop/` (the Python process boundary), `Models/`, or `Dialogs/`.
   A new **resource dictionary** goes into `Themes/` (XAML only, no
   code-behind). Five dictionaries, merged in this order by `App.xaml`:
   `Tokens.xaml` (spacing/radius/type/motion), `Glyphs.xaml` (`Glyph.*`
   geometries), `Colors.Light.xaml` (swapped for `Colors.Dark.xaml` at runtime,
   same key set) and `Controls.xaml` (implicit/keyed styles). Reach colours with
   `DynamicResource` (a theme switch replaces the dictionary); tokens and glyphs
   are merged before it and never change, so `StaticResource` is correct there.
   Only the application shell (`App.xaml(.cs)`, `AppHost.cs`, `AssemblyInfo.cs`,
   `GlobalUsings.cs`) lives at `SmsWorkbench/` root;
   `tests/workbench/test_sms_workbench_layout.py` pins both the folder set and
   that root exception list.

## Cleanup boundary

Generated files are not all interchangeable. Cache and build output may be
deleted after the owning process stops: Python bytecode/cache, .NET `bin/obj`,
test results, retention helper logs, the Windows `nul` artifact, and tool-local
`.workbuddy-ai` metadata. Preserve or explicitly archive `config.json*`, mailbox
and token files, `session.json`, `sessions/`, `runtime/`, and provider state
backups because they may contain credentials, account state, reconciliation
evidence, or resumable checkpoints. Never use a broad `git clean -xfd` in this
repository.
