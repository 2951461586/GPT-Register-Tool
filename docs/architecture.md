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
| `registration/orchestrator.py` | Stable entry points and compatibility exports | Stage implementations |
| `registration/handlers.py` | Protocol workflow and stage ordering | Payment or recovery orchestration |
| `registration/persistence.py`, `registration/stage_runner.py`, `registration/protocol_helpers.py`, `registration/otp_stages.py`, `registration/account_stages.py`, `registration/edge_challenge.py`, `registration/resume.py`, `registration/sentinel_stages.py` | Persistence seam, stage executor + abort signal, pure transport/format helpers, the email-OTP stages, the account-creation / session / AT-probe stages, the in-flow edge-challenge hook (same-pool exit move + audit counters), checkpoint persistence + the post-create resume stage, and Sentinel issuance (switch / password-page bundle / per-flow); re-exported by `registration/handlers.py` | Stage ordering |
| `registration_drivers/browser_flow/` | Browser page state and form workflow | Mailbox provider transport |
| `registration_drivers/external_sessions/` | Session creation, managed lifecycle, profile config and egress probe | Registration stages |
| `mailbox/service.py`, `mailbox/strategies.py`, `providers/` | Mailbox routing, capability resolution, polling policy and provider transport | Registration success |
| `registration/result.py` | Shared result assembly | Success decisions |
| `store/` | Persistence and domain facts | Provider side effects |
| `accounts/account_recovery.py`, `codex_oauth.py` | Existing-account recovery and OAuth | Automatic new signup |
| `payment_*`, `pay_link/`, `paypal/` | Payment planning and explicit execution | Mailbox purchasing or account signup |
| `pay_link/` vs `paypal_link/` + `upi_link/` | `pay_link/` is the **registry** and the shared extraction state machine: a new payment method goes there, and `scripts/payment_legacy_manifest.py` fails the build if a file is added under either legacy lane. `paypal_link/` and `upi_link/` are frozen legacy lanes that still take bug fixes. A method that lives in a legacy lane bypasses the registry, `payment_catalog` and the desktop method picker -- the exact drift the manifest gate exists to stop. | A new method implemented in a legacy lane |

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
`payment_wire` (cross-lane payment kernel), `store` (persistence),
`desktop_ipc` (v2 IPC envelope) and the injected `reverse_pay` adapter (the
PayPal browser lane's one dependency on the protocol lane). See
[Boundary Rule Checks](#boundary-rule-checks) for the enforceable form.

The stack above is *per module*, not per directory: `sms_tool/` holds leaf
contracts and application workflows side by side, so a directory-level reading
made every pair look legal in both directions. `scripts/layer_manifest.json`
assigns each module one of five layers (1 surface, 2 command adapter, 3
workflow, 4 domain contract, 5 adapter) and
`scripts/import_layer_ratchet.py` fails on any module-scope import that points
outward (`source layer > target layer`), including same-directory edges such as
a package `__init__` re-exporting a higher-layer submodule. Two edges are
allow-listed there with a written reason; everything else must move the shared
helper down or invert the call. A module the manifest does not claim fails the
check, so coverage cannot silently rot.

Operation proxy precedence is explicit input, optional saved registration
affinity, then the operation pool and its documented fallback. The ordered
candidates and their non-sensitive source labels come from
`proxy_routing.operation_proxy_candidates`; callers must not rebuild that
order. Shared proxy-health writes use an OS file lock, while the async SOCKS
pool moves persistence off its event loop.

### Known function-level lazy cycles

Four module pairs import each other **inside function bodies only**, and one
five-module cycle is closed by a single lazy edge. They are not import-time
cycles, but they are real coupling. They are listed here so a future reader
neither re-discovers them as a surprise nor "fixes" them by lifting the import
to module scope (which would create a genuine cycle).
`scripts/delayed_import_ratchet.py` freezes every delayed import by
`(importer, target)` and fails on a new edge or a changed reason; this table is
the *reason* behind the ones that form cycles.

Each frozen edge also carries a reason, derived by rule: `cycle` (one of the
documented pairs above), `startup` (the CLI/command seam, or a heavy optional
stack such as `playwright` / `curl_cffi` / `camoufox`), `provider` (a
provider/browser subsystem), `test-seam` (a path that exists only so tests can
redirect the store — currently none) and `deferred` (a sibling imported late to
keep the module graph acyclic or the import light). Current baseline: 415
delayed statements over 315 distinct edges — 8 `cycle`, 96 `startup`, 34
`provider`, 177 `deferred`.

A **new `deferred` edge cannot be adopted without a written reason**: it needs an
entry in `scripts/delayed_import_reasons.json` (`reason` + `why`), and
`--update-baseline` refuses it otherwise. The same file overrides a derived
reason when the rules are wrong, so a correction is one data entry instead of a
rule change. Rule-derived reasons (`cycle` / `startup` / `provider`) are
self-evident and only need the baseline update, which prints every added edge.

The authoritative form of this list is an exact import graph (every real
`import` statement, resolved against the package layout, including
function-body ones), not the review graph: that graph resolves ~82% of edges by
bare name and reports one 483-edge cycle spanning 14 subpackages, while the
exact graph has only the cycles listed in the table below (there is no committed
script for the exact graph, which is why this sentence carries no number).
Recompute the exact form before adding a row here.

| Pair / cycle | Edge(s) | Why it stays lazy |
| --- | --- | --- |
| `checkout_contract` ↔ `payment_catalog` | `checkout_contract` reads `payment_catalog.PAYMENT_METHODS`; `payment_catalog.validate_catalog_consistency()` reads `checkout_contract.PAYMENT_METHOD_PROFILES` | The catalog is the method vocabulary; the profile comparison is an on-demand assertion. Lifting it would make the catalog import the contract it validates. |
| `payment_catalog` ↔ `payment_flow` | `payment_flow` reads `payment_catalog.PAYMENT_METHODS`; `payment_catalog.validate_catalog_consistency()` reads `payment_flow.FLOW_PROFILES` | Same reason as above, for the flow vocabulary. |
| `payment_routing` ↔ `paypal_proxy` | `payment_routing` reads `paypal_proxy.{redact_proxy_url, proxy_state_from_config, select_proxy_from_pool, rotate_proxy_session}`; `paypal_proxy` reads `payment_routing.method_payment_config` | `paypal_proxy` is the legacy stage-proxy owner and `payment_routing` the newer planner. The proxy helpers are consumed only while building route options, so both edges are function-local. |
| `sentinel_tokens` ↔ `sentinel.client` | `sentinel_tokens` reads `sentinel.client.issue_sentinel_bundle`; `sentinel.client` reads `sentinel_tokens._extract_sentinel` for its legacy fallback | `sentinel_tokens` deliberately imports the **submodule**, not the `sentinel` package entry, because the package `__init__` re-exports from `client` and would close the cycle through the package. |
| `proxy_routing` → `accounts.account_identity`, closing a five-module cycle: `account_identity` → `fingerprint_pool` → `paypal_proxy` → `payment_routing` → `proxy_routing` | `proxy_routing._saved_registration_proxy` reads `accounts.account_identity.resolve_account_proxy` | The lookup is the opt-in `account_health.use_registration_affinity` path, so it stays function-local alongside the other three lazy edges in the cycle (`fingerprint_pool` → `paypal_proxy`, `paypal_proxy` → `payment_routing`); the remaining two (`account_identity` → `fingerprint_pool`, `payment_routing` → `proxy_routing`) are top-level. Lifting this edge would close the cycle at import time. |

Any further lazy edge that closes a cycle must be added here with the same
evidence, or refactored so the edge disappears. `tests/governance/test_delayed_import_ratchet.py`
keeps the count from growing while that decision is pending.

### Review-graph scope (`pi-lens`)

`pi-lens`'s review graph is a second, independent view of the same coupling. Its
scope is **declared** in `.pi-lens.json` rather than inherited, because neither
thing it would otherwise inherit from is a statement about this repository:

- `pi-lens` ships a built-in excluded-directory list (`node_modules`, `dist`,
  `build`, `.venv`, `.ruff_cache`, `.pytest_cache`, `__pycache__`, `.agents`,
  `.claude`, `.codex`, `vendor`, …). That list changes between `pi-lens`
  versions, and it does **not** include `runtime/`, `sessions/`, `.dotnet/`, the
  `.workbuddy*` agent state, `browser_extensions/`, `outlook_results/`, or the
  .NET `bin/`/`obj/` trees.
- Everything else is left to `.gitignore`, which is a statement about **git**,
  not about code review. `pi-lens` also rescues a *tracked* file that matches
  `.gitignore` (git's own "a tracked file is never ignored" semantic), so a
  generated tree that is ever committed stops being excluded. A project
  `ignore` entry is the stronger layer: it is never tracked-rescued.

This matters because `runtime/` alone holds 340 Python files (operator state,
logs and dumps, plus the reference-repo clones the audits make under
`runtime/tmp/refrepos/`), and it grows without bound.

| In scope (the product) | Excluded (tooling, generated, state, vendored) |
| --- | --- |
| `sms_tool/`, `services/protocol-payment/`, `SmsWorkbench/`, `SmsWorkbench.Contracts/` | `scripts/`, `dist/`, `runtime/`, `sessions/`, `.dotnet/`, `.workbuddy*/`, `browser_extensions/`, `outlook_results/`, .NET `bin/`+`obj/`, `sms_tool/upi_link/_vendor/` |

`scripts/` is excluded **on purpose**, and the trade-off is stated so it is not
re-litigated. The review graph is an architecture view of the *product*, and the
gate/ratchet scripts are tooling that consumes the product rather than part of
it. While they were in scope, `DEAD WEIGHT` reported ten `scripts/*.py` files as
unreachable on every build -- all false positives, because CI, the git hooks and
`tests/governance/test_*_ratchet.py` invoke them by filename rather than importing them
(`docs/audits/scans/scan-2026-10-04-import-cycle-decomposition.md` §7 measured 8 of 10
as false positives and deleted the 4 that were genuinely dead). Excluding the
tree makes that section actionable again.

The cost is that `pi-lens`'s LSP diagnostics and symbol search no longer cover
`scripts/`. That is acceptable because the scripts keep their own coverage:
`ruff check scripts` runs in CI, and every ratchet has a `tests/governance/test_*_ratchet.py`
that exercises it. If a script ever needs diagnostics, this is a one-line revert.

`tests/` is deliberately **not** in the ignore list. `pi-lens` already keeps test
files out of the review graph by file role (upstream #260), so ignoring the tree
would buy the graph nothing while removing LSP diagnostics and symbol search from
340 test files the operator navigates daily.

**Auto-formatting is off** (`"format": {"enabled": false}`). `pi-lens` runs a
deferred formatter over every file it edits. On 2026-10-10 that reformatted four
files *outside* the protocol-registration lane the repository gated at the time
(`accounts/account_payment_eligibility.py`, `pay_link/__init__.py`,
`services/protocol-payment/momo/ac_paylink_core.py`,
`tests/accounts/test_account_payment_eligibility.py`) to fix pre-existing
drift the project had decided not to touch -- 431 insertions / 180 deletions,
against edits of one to three lines each. `format.enabled` is one of the
project-scoped *mutation controls* `pi-lens` honours (the others are
`autofix.enabled` and `actionableWarnings.autoFix.enabled`), so this is the
supported way to stop it, not a local preference.

**That lane split no longer exists.** On 2026-10-20 the whole Python tree was
reformatted in one dedicated commit (`64aa42c`) and `scripts/format_guard.py`
was widened from the lane to `sms_tool` / `services` / `scripts` / `tests`, in
the pre-commit hook and in CI. A file touched by an editor or an agent can
therefore no longer drift out of format unnoticed -- which is what makes keeping
auto-format off cost-free, and `scripts/format_guard.py --fix` is the developer
convenience. The two decisions are pinned together by
`tests/config/test_pilens_scope_config.py`: narrowing the gate back below
`sms_tool/`, or removing it while auto-format stays off, fails the suite.

**Suppressing a finding uses the analyser's own marker, not `pi-lens`'s.**  A
Pyright finding needs the tool-native, *same-line* form
(``x = f(y)  # pyright: ignore[reportArgumentType]``); an ``# pi-lens-ignore:
<rule>`` comment on the line above does **not** suppress it.  ``# pi-lens-ignore``
is what works for the ``ast-grep`` rules, and it is what the existing markers in
``services/protocol-payment/`` use.  Measured 2026-10-10 on ``payment/wire.py``
and ``momo/ac_paylink_core.py``: the same-line ``# pyright: ignore[...]`` cleared
both Pyright findings on the first try, while the preceding-line
``# pi-lens-ignore`` and a stored ``false-positive`` disposition left them
reported.  Prefer the source-level marker over a stored disposition for Pyright:
the disposition anchor hashes the diagnostic *message*, and at least one message
here (``dict[str, str]`` vs ``form=``) changes text between runs, so the anchor
can silently stop matching.

This is a *scope* declaration, not a measurement. It does not change the graph's
edge count, and it is not the architecture authority: the frozen measure of
cross-directory coupling remains `scripts/import_layer_ratchet.py` (the numbers
below are regenerated by `scripts/refresh_doc_ratchet_numbers.py`, never edited
by hand), and the authoritative cycle list is the exact import graph described
above.

<!-- ratchet-numbers:begin architecture -->
| Measure | Value |
| --- | --- |
| Module-level cross-directory edges | 437 |
| Directory pairs | 66 |
| Mutual pairs / minority-direction edges | 18 / 49 |
| Delayed cross-directory edges | 253 |
| Delayed imports in the package | 417 (baseline 417) |
| Claimed tracked `sms_tool` modules | 310 of 310 (0 unclaimed, 41 directory-map groups) |
<!-- ratchet-numbers:end architecture -->

`tests/config/test_pilens_scope_config.py` pins the scope and the formatting
decision so neither can silently rot.

## Registration Modules

See [registration architecture](current/registration-architecture.md) for the
state groups, session ownership, error policy and compatibility guarantees.
See [protocol registration](current/protocol-registration.md) for the protocol
lane's endpoints, step order, Sentinel flow and landing-page vocabulary.
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

This is the authoritative, executable form of the boundary rules. Each rule
lives in its own file under [`docs/rules/`](rules/) with the forbidden
dependency, the grep that proves or disproves it, and the section of the
pre-hardening snapshot
`docs/audits/records/architecture-before-registration-hardening-2026-09-06.md`
it was lifted from; run the checks from the repository root.

**The numbers are a contract.** Two dozen places in tests, scripts and other
docs cite `Rule N`, so the index below is the stable surface and the files are
append-only in numbering. `tests/governance/test_architecture_rules.py` fails on
a missing file, a duplicate number, a stale link, or a `Rule N` reference with
no file behind it.

| # | Rule | Full rule |
| --- | --- | --- |
| 1 | Transport must not depend on registration policy | [`rule-01-transport-depend-registration-policy.md`](rules/rule-01-transport-depend-registration-policy.md) |
| 2 | Persistence performs no vendor protocol calls | [`rule-02-persistence-performs-vendor-protocol.md`](rules/rule-02-persistence-performs-vendor-protocol.md) |
| 3 | Provider adapters must not import the registration or payment workflow | [`rule-03-provider-adapters-import-registration.md`](rules/rule-03-provider-adapters-import-registration.md) |
| 4 | A shared contract does not import a concrete provider: `checkout_contract` stays provider-free | [`rule-04-shared-contract-import-concrete.md`](rules/rule-04-shared-contract-import-concrete.md) |
| 5 | `sms_tool/paypal/` is layered one-way and acyclic; `paypal/errors.py` is a dependency-free leaf importable from every layer | [`rule-05-sms-tool-paypal-layered.md`](rules/rule-05-sms-tool-paypal-layered.md) |
| 6 | PayPal execution must not regenerate links: no `gen_pp_link` import inside `sms_tool/paypal/` | [`rule-06-paypal-execution-regenerate-links.md`](rules/rule-06-paypal-execution-regenerate-links.md) |
| 7 | One liveness owner | [`rule-07-liveness-owner.md`](rules/rule-07-liveness-owner.md) |
| 8 | Agent Identity is explicit-import only; the registration pipeline must not reach it | [`rule-08-agent-identity-explicit-import.md`](rules/rule-08-agent-identity-explicit-import.md) |
| 9 | One IPC writer: `sms_tool/desktop_ipc.py` is the sole writer of the `@@SMSWORKBENCH_V2@@` envelope | [`rule-09-ipc-writer-sms-tool.md`](rules/rule-09-ipc-writer-sms-tool.md) |
| 10 | `services/protocol-payment/` is a process boundary with zero import-level coupling to `sms_tool`; the only permitted reference is the documented lazy import in `kakao/kakao_extract.py` | [`rule-10-services-protocol-payment-process.md`](rules/rule-10-services-protocol-payment-process.md) |
| 11 | Progress and concurrency are separate | [`rule-11-progress-concurrency-separate.md`](rules/rule-11-progress-concurrency-separate.md) |
| 12 | Command adapters call public workflow functions; they must not import private provider helpers or write SQLite/session files | [`rule-12-command-adapters-call-public.md`](rules/rule-12-command-adapters-call-public.md) |
| 13 | Proxy string authority is single | [`rule-13-proxy-string-authority-single.md`](rules/rule-13-proxy-string-authority-single.md) |
| 14 | Desktop code must not add a static service locator, and must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or SQLite business rules beyond display and deletion | [`rule-14-desktop-code-add-static.md`](rules/rule-14-desktop-code-add-static.md) |
| 15 | Optional command modules are lazy seams: importing `sms_tool.cli` must not start a command or pull payment/browser dependencies as a side effect | [`rule-15-optional-command-modules-lazy.md`](rules/rule-15-optional-command-modules-lazy.md) |
| 16 | A hub module's private symbols must not gain new cross-module consumers | [`rule-16-hub-module-s-private.md`](rules/rule-16-hub-module-s-private.md) |
| 17 | A text file must not mix CRLF and LF internally | [`rule-17-text-file-mix-crlf.md`](rules/rule-17-text-file-mix-crlf.md) |
| 18 | A watched-hub ratchet must resolve the hub from the **import statement**, never from the importing file or a bare name match | [`rule-18-watched-hub-ratchet-resolve.md`](rules/rule-18-watched-hub-ratchet-resolve.md) |
| 19 | A same-named function in two modules is not a duplicate until its AST proves it | [`rule-19-named-function-modules-duplicate.md`](rules/rule-19-named-function-modules-duplicate.md) |
| 20 | A cross-lane payment primitive has one home | [`rule-20-cross-lane-payment-primitive.md`](rules/rule-20-cross-lane-payment-primitive.md) |
| 21 | The PayPal browser lane must not depend on the protocol lane | [`rule-21-paypal-browser-lane-depend.md`](rules/rule-21-paypal-browser-lane-depend.md) |
| 22 | The registration lane must not import the payment lane | [`rule-22-registration-lane-import-payment.md`](rules/rule-22-registration-lane-import-payment.md) |
| 23 | Lazily imported CLI dependencies must self-heal; a one-shot latch must not outlive a patched symbol | [`rule-23-lazily-imported-cli-dependencies.md`](rules/rule-23-lazily-imported-cli-dependencies.md) |
| 24 | An early command that emitted its result must stop dispatch | [`rule-24-early-command-emitted-result.md`](rules/rule-24-early-command-emitted-result.md) |
| 25 | The desktop account list refreshes incrementally; the full payload is the anti-drift anchor | [`rule-25-desktop-account-list-refreshes.md`](rules/rule-25-desktop-account-list-refreshes.md) |

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

Rules 2–5, 7–13 and 15–22 currently hold.

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

As implemented by `SmsWorkbench/Interop/BackendResultInterpreter.cs` (keep this table
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

- [Payment protocol architecture](current/protocol-payment-enhancement.md)
- [PayPal zero-due link contract](current/paypal-zero-due-link.md)
- [Recovery and cancellation](current/registration-recovery.md)
- [Accepted decisions](adr/README.md)
- [Pre-hardening detailed snapshot](audits/records/architecture-before-registration-hardening-2026-09-06.md)

The historical snapshot preserves the former long document without presenting
old file sizes and audit findings as current facts.
