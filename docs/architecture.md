# Project Architecture and Boundaries

Current ownership and operating contracts. The physical file inventory lives in
[directory-map.md](directory-map.md), not a second duplicated tree here.

## Runtime Flow

```text
WPF / CLI -> command planning -> application workflow
                                   -> mailbox/provider adapters
                                   -> protocol/browser registration driver
                                   -> verified account/session persistence
```

Registration success requires the configured AT probe, not merely a visible
success page or a received token. Payment authentication, account recovery and
export remain separate workflows.

## Ownership Matrix

| Module | Owns | Does not own |
| --- | --- | --- |
| `SmsWorkbench/` | UI, command planning, task lifecycle, presentation | Provider protocol and registration state |
| `sms_tool/commands/` | CLI argument adaptation | Provider implementation |
| `registration.py` | Stable entry points and compatibility exports | Stage implementations |
| `registration_handlers.py` | Protocol workflow and stage ordering | Payment or recovery orchestration |
| `registration_drivers/browser_flow/` | Browser page state and form workflow | Mailbox provider transport |
| `registration_drivers/external_sessions/` | Session creation, managed lifecycle, profile config and egress probe | Registration stages |
| `mailbox_service.py`, `mailbox_strategies.py`, `providers/` | Mailbox routing, capability resolution, polling policy and provider transport | Registration success |
| `registration_result.py` | Shared result assembly | Success decisions |
| `store/` | Persistence and domain facts | Provider side effects |
| `accounts/account_recovery.py`, `codex_oauth.py` | Existing-account recovery and OAuth | Automatic new signup |
| `payment_*`, `pay_link/`, `paypal/` | Payment planning and explicit execution | Mailbox purchasing or account signup |

## Dependency Direction

Entrypoints depend on workflow Interfaces. Workflows call provider and storage
Adapters. Provider implementations must not import the registration facade.
`RegistrationOperations` is a fixed immutable Interface bound per invocation;
it is not a live module passed into the workflow.

The layer stack, outermost first, is UI (`SmsWorkbench/`) -> CLI/command adapters
-> application workflows -> domain contracts -> provider/persistence adapters.
Imports point inward only; a lower layer never imports a higher one. Shared
contracts never import a concrete provider: `checkout_contract` stays
provider-free. The seams fixed in place are `account_seed` (seed lookup),
`account_liveness` (liveness probe), `proxy_entry` (proxy string authority),
`store` (persistence) and `desktop_ipc` (v2 IPC envelope). See
[Boundary Rule Checks](#boundary-rule-checks) for the enforceable form.

Operation proxy precedence is explicit input, optional saved registration
affinity, then the operation pool and its documented fallback. The ordered
candidates and their non-sensitive source labels come from
`proxy_routing.operation_proxy_candidates`; callers must not rebuild that
order. Shared proxy-health writes use an OS file lock, while the async SOCKS
pool moves persistence off its event loop.

## Registration Modules

See [registration architecture](current/registration-architecture.md) for the
state groups, session ownership, error policy and compatibility guarantees.
See [account health contract](current/account-health.md) for probe/recovery
side effects, result semantics and proxy precedence.
[ADR-0009](adr/0009-registration-hardening.md) records the decisions.
The durable account-health queue owns claim, lease, heartbeat and scheduling
only. It delegates plan and liveness work to the same workflows used by
foreground commands.

## Boundary Rules

Prose form. The enforceable form of the same rules is
[Boundary Rule Checks](#boundary-rule-checks).

- Configuration is explicit or context-scoped. Do not mutate `CFG` in production
  workflows; its shared overrides exist for compatibility.
- State persistence does not call provider APIs.
- New dependencies belong in the owning Module's Interface, not an unrelated
  facade. Tests patch the defining Module or inject the workflow dependency.
- Browser challenges are terminal for automation. Do not disable protected
  controls or treat an unknown page as a successful registration.
- Cancellation, stage deadlines, session circuits and mailbox cooldowns have
  distinct lifetimes. A shared error policy does not make their state global.
- Payment retries must not repeat an ambiguous external side effect.
- A same-named function in two modules is not a duplicate until its AST proves
  it. Same-name functions that differ by layer, signature or a deliberately
  different default carry that difference on purpose; merging them changes
  production behaviour and needs fresh evidence and an explicit decision.

## Boundary Rule Checks

This is the authoritative, executable form of the boundary rules. Every row
names the forbidden dependency and the grep that proves or disproves it; run
them from the repository root. All rows are lifted from the pre-hardening
snapshot
`docs/audits/architecture-before-registration-hardening-2026-09-06.md` — the
source section is named in the last column so the original wording can be
recovered.

| # | Rule | Check | Source section |
| --- | --- | --- | --- |
| 1 | Transport must not depend on registration policy. `sms_tool/http_client.py` imports only `config` and `sanitizer`. | `grep -n "^from \.\|^import " sms_tool/http_client.py` | Registration Protocol Consistency; Dependency Direction |
| 2 | Persistence performs no vendor protocol calls. `sms_tool/store/**` imports nothing above `account_models` / `config` / `paths`, and no HTTP client. | `grep -rn "^from \.\.\|^from sms_tool\|^import requests\|^import httpx\|curl_cffi" sms_tool/store/` | Storage Layer; Ownership Matrix |
| 3 | Provider adapters must not import the registration or payment workflow. | `grep -rn "from \.\.\(registration\|payment\|paypal\)" sms_tool/providers/` | Dependency Direction; Ownership Matrix |
| 4 | A shared contract does not import a concrete provider: `checkout_contract` stays provider-free. | `grep -n "^from \.\(wallet_provider\|wallet_transport\|pay_link\|paypal\|gcash\)" sms_tool/checkout_contract.py` | Dependency Direction |
| 5 | `sms_tool/paypal/` is layered one-way and acyclic; `paypal/errors.py` is a dependency-free leaf importable from every layer. | `grep -n "^from \.\|^import" sms_tool/paypal/errors.py` | PayPal Payment Layer |
| 6 | PayPal execution must not regenerate links: no `gen_pp_link` import inside `sms_tool/paypal/`. | `grep -rn "gen_pp_link" sms_tool/paypal/` | PayPal Payment Layer; Payment Responsibility Boundary |
| 7 | One liveness owner. Only `account_liveness` defines the `/backend-api/wham/usage` probe; registration, JIT payment auth and protocol payment adapters must not define their own `/backend-api/me` probe. | `grep -rn "backend-api/me" sms_tool/` | Account Liveness Contract |
| 8 | Agent Identity is explicit-import only; the registration pipeline must not reach it. | `grep -rn "agent_identity" sms_tool/registration*.py sms_tool/registration_drivers/` | Agent Identity Layer (Explicit Import Only) |
| 9 | One IPC writer: `sms_tool/desktop_ipc.py` is the sole writer of the `@@SMSWORKBENCH_V2@@` envelope. | `grep -rn "SMSWORKBENCH_V2" sms_tool/` | WPF UI |
| 10 | `services/protocol-payment/` is a process boundary with zero import-level coupling to `sms_tool`; the only permitted reference is the documented lazy import in `kakao/kakao_extract.py`. | `grep -rn "from sms_tool\|import sms_tool" services/` | 依赖关系 (services/protocol-payment) |
| 11 | Progress and concurrency are separate. `registration_concurrency` must not write progress files or classify registration results; progress may call concurrency, never the reverse. | `grep -n "registration_progress" sms_tool/registration_concurrency.py` | Registration Progress and Concurrency |
| 12 | Command adapters call public workflow functions; they must not import private provider helpers or write SQLite/session files. | `grep -rn "^from \.\.\(pay_link\|store\|wallet_provider\|providers\)" sms_tool/commands/` | Dependency Direction; CLI |
| 13 | Proxy string authority is single. Only `proxy_entry` defines `parse_proxy` / `rebuild_proxy_credentials` / `retarget_region` / `rotate_session` / `infer_region`; `phone_proxy` and `paypal_proxy` are thin wrappers. | `grep -rn "^def \(parse_proxy\|rebuild_proxy_credentials\|retarget_region\|rotate_session\|infer_region\)(" sms_tool/` — `proxy_entry.py` only | Proxy Routing Boundary |
| 14 | Desktop code must not add a static service locator, and must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or SQLite business rules beyond display and deletion. | `grep -rn "ServiceLocator" SmsWorkbench/` | WPF UI |
| 15 | Optional command modules are lazy seams: importing `sms_tool.cli` must not start a command or pull payment/browser dependencies as a side effect. | `grep -n "^from \.\(paypal\|payment_batch\|paypal_link\|pay_link\|playwright\)" sms_tool/cli.py` | CLI |
| 16 | A hub module's private symbols must not gain new cross-module consumers. A hub is a module whose public surface is effectively empty while other modules reach into it anyway — its `_`-prefixed names are a de facto interface, and nothing else counts them. Six are watched: `mailbox` (~40 top-level functions, **every one `_`-prefixed**), `session_refresh`, `mail_otp`, `sentinel_tokens`, `http_utils` and `utils`; a 2026-09-17 survey found each has a private-definition count close to its consumed-symbol count, i.e. they too are hubs rather than modules with a thin private tail. The ratchet records today's `(importer, watched_module, symbol)` triples **per module** and fails only when a set grows; it does not demand that existing consumers be rewritten. Baselines are per module on purpose: with one grand total, a reduction in `mailbox` would mask new coupling onto `utils`. | `python scripts/mailbox_private_import_ratchet.py` exits 0, and `scripts/mailbox_private_import_baseline.json` lists every triple under its module | Ownership Matrix; Dependency Direction |
| 17 | A text file must not mix CRLF and LF internally. `.gitattributes` pins what git stores (LF); this rule covers what arrives from an editor or a generator, which git cannot normalise retroactively. A single mixed file makes `git diff` report a whole-file change, destroys `git blame` attribution, and breaks the byte-exact reconciliation this repo relies on to attribute a change to a baseline (`git show HEAD:path` + SHA256). Either ending is acceptable **per file**; only mixing is forbidden. **`MIXED_EXEMPT` is now empty**: the whole working tree was measured byte-by-byte on 2026-09-17 and every mixed file was normalised, so any mixed file at all now fails. Scope is decided by **evidence, not declaration**: the suffix whitelist is only a fast path, and an extensionless file is still classified (by a UTF-8/NUL content sniff) — a `LICENSE` holding 21 CRLF + 11 lone LF sat undetected precisely because `Path("LICENSE").suffix == ""` never matched the whitelist and `inspect` never read the file. The guard self-tests its classifier first: a weakened predicate fails loudly instead of silently reporting a clean tree. | `python scripts/line_ending_guard.py --all` exits 0; `pytest tests/test_line_ending_guard.py` (41 cases) | Repository Hygiene |
| 18 | A watched-hub ratchet must resolve the hub from the **import statement**, never from the importing file or a bare name match. `_load_mailbox_pool` is imported from `.mailbox` by `sms_tool/cli.py`, so the consumer is `cli` and the hub is `mailbox`; conversely `mailbox_service.py` imports `_email_cfg` *from* `.mailbox`, so it is a consumer of `mailbox`, not a hub. Resolution is therefore on the last dotted segment of the `ImportFrom` **plus** a package check: `from sms_tool.providers.mailbox import ...` is accepted for a hub that may live in `providers/`, while `from third_party.utils import ...` is rejected — `utils` and `mail_otp` are generic enough that a vendor module could share the name. A hub is never counted as a consumer of its own surface. | `pytest tests/test_mailbox_private_import_ratchet.py` (25 cases), including `test_same_named_module_outside_the_package_is_rejected` and `test_a_watched_module_is_not_its_own_consumer` | Ownership Matrix; Dependency Direction |
| 19 | **A same-named function in two modules is not a duplicate until its AST proves it.** Six names had multiple definitions on 2026-09-17 (`parse_proxy_pool`, `payment_proxy_pools`, `redact_proxy_url`, `rotate_proxy_session`, `normalize_proxy_url`, `payment_method_label`); normalising provider tokens away, **all 25 definition sites were mutually distinct**. Two groups are already converged (one canonical body plus thin `return canonical_*(...)` shims): `payment_proxy_pools` → `payment_routing`, `redact_proxy_url` → `phone_proxy`. The rest are **deliberately divergent**: `redact_proxy_url`'s shims pass different `empty_placeholder` defaults (`""` vs `"DIRECT"`), `normalize_proxy_url` has one canonical body per **layer** (`sms_tool` applies a `parse_proxy` fallback; `services/protocol-payment` keeps a `udealproxy.com → socks5h://` rule), `rotate_proxy_session` exists in both 1-arg and 2-arg forms and the `services/` copy cannot reach `sms_tool.proxy_entry` (Rule 10), and `payment_method_label` exists in no-arg, delegated and registry-lookup forms. Merging any of these changes production behaviour, so convergence is forbidden without fresh evidence and an explicit decision — the same conclusion P0 reached for `normalize_proxy_url`. **Renamed 2026-09-18** so the divergence is legible at the call site (bodies untouched): `proxy_routing.parse_lane_proxy_pool` (registration/account-health lanes) vs `payment_routing.parse_proxy_pool` (payment domain); `pp_link_helpers.rotate_proxy_session_id` (1-arg, session id only) and `direct_card_extract.rotate_direct_card_proxy_session` (the Rule-10-bound `services/` copy) vs `paypal_proxy.rotate_proxy_session`; `blik_qr_extract.current_payment_method_label` (no-arg, current method) and `commands/helpers.cli_payment_method_label` (adds an `or "PayPal"` fallback) vs `pay_link/registry.payment_method_label` (registry lookup). | `python scripts/extractor_parity_report.py` exits 0 (append-only ratchet on extractor duplication); the same-name audit is `runtime/tmp/p2_shim_vs_canonical.py` and its output `runtime/tmp/p2_compare.txt`; `pytest tests/test_same_name_disambiguation.py` (31 cases) pins the rename and fails on both a re-introduced old name and a converged body | Ownership Matrix; Dependency Direction |

### Current Violations

- **Rule 1 — resolved 2026-09-07.** Transport no longer imports registration
  policy. The shared pieces were pushed *down* instead of hidden in a lazy
  import: pure backoff maths moved to `sms_tool/backoff.py` and terminal-error
  classification to `sms_tool/error_classification.py`
  (`is_terminal_registration_error`). `http_client.py` now imports from those
  two only. Do not "restore" the `registration_retry_decision(...)` call —
  it is exactly equivalent and reintroduces the layering violation.
- **Rule 6 — resolved 2026-09-12.** `sms_tool/paypal/orchestrator.py` no
  longer imports `gen_pp_link`. The execution layer consumes the URL the
  caller owns; when none exists the command adapter
  (`sms_tool/commands/payment_links.py`) injects `link_factory=generate_pp_link`
  so lazy generation stays adapter-owned, and without a factory a missing URL
  is an explicit `paypal_link_missing` failure instead of a silent second
  order. Enforced by the same grep as the rule row.
- **Rule 14 — restated 2026-09-12 (decision).** The pre-hardening snapshot
  assigned *result persistence* to `paypal.config_picker` in its module table
  and forbade "SQLite business rules" in the same breath; those two statements
  conflicted. Decision: `paypal/config_picker.py` IS the documented
  persistence owner for the auto-pay flow — its `upsert_account` call is
  sanctioned and `grep -rn "upsert_account" sms_tool/paypal/` must resolve
  only to `config_picker.py`. The desktop layer stays out of SQLite business
  rules; any new persistence site in `sms_tool/paypal/` needs a rule change
  first.

Rules 2–5, 7–13 and 15–19 currently hold.

## Email Change Flow

`commands/email_change.py` adapts arguments; `accounts/account_email_change.py` owns target
allocation, OTP verification and relogin; persistence follows a successful
liveness check. WPF never owns provider API calls.

## Portable Configuration

See [configuration ownership](current/configuration.md). Shards are authoritative
when any shard exists. `config.json` remains a supported migration input, not a
second source to merge into shards.

## Status and Dedup Semantics

Display rows are deduplicated by normalized email. SQLite/session data takes
precedence over mailbox pool rows. Transport-unknown AT results keep resumable
checkpoints; terminal account results are not retried as network errors.

## Exit Codes

As implemented by `SmsWorkbench/BackendResultInterpreter.cs` (keep this table
in sync with that mapping):

| Code | Meaning | Desktop presentation |
| --- | --- | --- |
| 0 | Normal completion | Information; Warning when the IPC payload is absent |
| 1 | Missing/invalid arguments | `[失败·参数]`, state failed |
| 2 | Preflight/environment failure | `[失败·前置检查]`, state failed |
| 3 | Runtime/provider failure | `[失败·运行时]`, state failed |
| Other / negative / timeout | Abnormal process termination | `[失败·运行时]`, state failed or timed_out |

`doctor.py` deliberately exits with the count of failed environment checks and
is not part of this table.

See [telemetry and runtime data](current/telemetry-and-runtime.md) for
`command_id`, `run_id`, schema version and test/live separation.
See [mailbox architecture](current/mailbox.md) for provider capabilities,
configuration and terminal-error policy.

## Release Boundary

Release packages must come from a clean source revision. Local configs,
credentials, SQLite, sessions and runtime state are not release payloads.
Use `scripts/build_installer.ps1`; do not publish mixed-build assets.

## Local Files That Must Stay Out of Git

`config.json`, `proxy.json`, `runtime.json`, `payment.json`, mailbox credentials,
`sessions/`, `runtime/`, `dist/` and tool-generated caches remain ignored.
Ignored does not mean disposable. Cleanup defaults to a report, never deletion.

## Terminal Account Cleanup

Only explicit terminal evidence permits account cleanup. Network/TLS/timeouts
remain eligible for recheck. The operator must approve any destructive action.
This hardening work does not delete or archive existing runtime files.

## Detailed And Historical References

- [Payment protocol architecture](protocol-payment-enhancement.md)
- [PayPal zero-due link contract](paypal-zero-due-link.md)
- [Recovery and cancellation](current/registration-recovery.md)
- [Accepted decisions](adr/README.md)
- [Pre-hardening detailed snapshot](audits/architecture-before-registration-hardening-2026-09-06.md)

The historical snapshot preserves the former long document without presenting
old file sizes and audit findings as current facts.
