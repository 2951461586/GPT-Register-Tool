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
| `registration_persistence.py`, `registration_stage_runner.py`, `registration_protocol_helpers.py`, `registration_otp_stages.py`, `registration_edge_challenge.py`, `registration_resume.py`, `registration_sentinel_stages.py` | Persistence seam, stage executor + abort signal, pure transport/format helpers, the email-OTP stages, the in-flow edge-challenge hook (same-pool exit move + audit counters), checkpoint persistence + the post-create resume stage, and Sentinel issuance (switch / password-page bundle / per-flow); re-exported by `registration_handlers.py` | Stage ordering |
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
`payment_wire` (cross-lane payment kernel), `store` (persistence),
`desktop_ipc` (v2 IPC envelope) and the injected `reverse_pay` adapter (the
PayPal browser lane's one dependency on the protocol lane). See
[Boundary Rule Checks](#boundary-rule-checks) for the enforceable form.

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
`scripts/delayed_import_ratchet.py` counts every delayed import in the package
and fails on growth; this table is the *reason* behind the ones that form
cycles.

The authoritative form of this list is an exact import graph (every real
`import` statement, resolved against the package layout, including
function-body ones), not the review graph: that graph resolves ~82% of edges by
bare name and reports one 483-edge cycle spanning 14 subpackages, while the
exact graph has six cycles over 16 of 278 modules. Recompute the exact form
before adding a row here.

| Pair / cycle | Edge(s) | Why it stays lazy |
| --- | --- | --- |
| `checkout_contract` ↔ `payment_catalog` | `checkout_contract` reads `payment_catalog.PAYMENT_METHODS`; `payment_catalog.validate_catalog_consistency()` reads `checkout_contract.PAYMENT_METHOD_PROFILES` | The catalog is the method vocabulary; the profile comparison is an on-demand assertion. Lifting it would make the catalog import the contract it validates. |
| `payment_catalog` ↔ `payment_flow` | `payment_flow` reads `payment_catalog.PAYMENT_METHODS`; `payment_catalog.validate_catalog_consistency()` reads `payment_flow.FLOW_PROFILES` | Same reason as above, for the flow vocabulary. |
| `payment_routing` ↔ `paypal_proxy` | `payment_routing` reads `paypal_proxy.{redact_proxy_url, proxy_state_from_config, select_proxy_from_pool, rotate_proxy_session}`; `paypal_proxy` reads `payment_routing.method_payment_config` | `paypal_proxy` is the legacy stage-proxy owner and `payment_routing` the newer planner. The proxy helpers are consumed only while building route options, so both edges are function-local. |
| `sentinel_tokens` ↔ `sentinel.client` | `sentinel_tokens` reads `sentinel.client.issue_sentinel_bundle`; `sentinel.client` reads `sentinel_tokens._extract_sentinel` for its legacy fallback | `sentinel_tokens` deliberately imports the **submodule**, not the `sentinel` package entry, because the package `__init__` re-exports from `client` and would close the cycle through the package. |
| `proxy_routing` → `accounts.account_identity`, closing a five-module cycle: `account_identity` → `fingerprint_pool` → `paypal_proxy` → `payment_routing` → `proxy_routing` | `proxy_routing._saved_registration_proxy` reads `accounts.account_identity.resolve_account_proxy` | The lookup is the opt-in `account_health.use_registration_affinity` path, so it stays function-local alongside the other three lazy edges in the cycle (`fingerprint_pool` → `paypal_proxy`, `paypal_proxy` → `payment_routing`); the remaining two (`account_identity` → `fingerprint_pool`, `payment_routing` → `proxy_routing`) are top-level. Lifting this edge would close the cycle at import time. |

Any further lazy edge that closes a cycle must be added here with the same
evidence, or refactored so the edge disappears. `tests/test_delayed_import_ratchet.py`
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
`tests/test_*_ratchet.py` invoke them by filename rather than importing them
(`docs/audits/scan-2026-10-04-import-cycle-decomposition.md` §7 measured 8 of 10
as false positives and deleted the 4 that were genuinely dead). Excluding the
tree makes that section actionable again.

The cost is that `pi-lens`'s LSP diagnostics and symbol search no longer cover
`scripts/`. That is acceptable because the scripts keep their own coverage:
`ruff check scripts` runs in CI, and every ratchet has a `tests/test_*_ratchet.py`
that exercises it. If a script ever needs diagnostics, this is a one-line revert.

`tests/` is deliberately **not** in the ignore list. `pi-lens` already keeps test
files out of the review graph by file role (upstream #260), so ignoring the tree
would buy the graph nothing while removing LSP diagnostics and symbol search from
340 test files the operator navigates daily.

**Auto-formatting is off** (`"format": {"enabled": false}`). `pi-lens` runs a
deferred formatter over every file it edits, but this repository enforces
formatting on the **protocol-registration lane only** -- `scripts/format_guard.py`
checks `sms_tool/auth_flow/` and `sms_tool/registration_*.py`, and its own
docstring states why the scope is the lane and not the package:

> widening it to ``sms_tool/`` would reformat ~100 unrelated files in one commit
> and bury the real edit.

The auto-formatter did exactly that on 2026-10-10: four files *outside* the lane
(`accounts/account_payment_eligibility.py`, `pay_link/__init__.py`,
`services/protocol-payment/momo/ac_paylink_core.py`,
`tests/test_account_payment_eligibility.py`) were reformatted to fix pre-existing
drift the project had decided not to touch -- 431 insertions / 180 deletions,
against edits of one to three lines each. `format.enabled` is one of the
project-scoped *mutation controls* `pi-lens` honours (the others are
`autofix.enabled` and `actionableWarnings.autoFix.enabled`), so this is the
supported way to stop it, not a local preference.

Nothing is lost: the lane's formatting is enforced by the gate, which runs in
both the pre-commit hook and CI, and `scripts/format_guard.py --fix` is the
developer convenience. The two decisions are pinned together by
`tests/test_pilens_scope_config.py`, so turning auto-format back on without also
widening the gate -- or removing the gate while auto-format stays off -- fails.

**Suppressing a finding uses the analyser's own marker, not `pi-lens`'s.**  A
Pyright finding needs the tool-native, *same-line* form
(``x = f(y)  # pyright: ignore[reportArgumentType]``); an ``# pi-lens-ignore:
<rule>`` comment on the line above does **not** suppress it.  ``# pi-lens-ignore``
is what works for the ``ast-grep`` rules, and it is what the existing markers in
``services/protocol-payment/`` use.  Measured 2026-10-10 on ``payment_wire.py``
and ``momo/ac_paylink_core.py``: the same-line ``# pyright: ignore[...]`` cleared
both Pyright findings on the first try, while the preceding-line
``# pi-lens-ignore`` and a stored ``false-positive`` disposition left them
reported.  Prefer the source-level marker over a stored disposition for Pyright:
the disposition anchor hashes the diagnostic *message*, and at least one message
here (``dict[str, str]`` vs ``form=``) changes text between runs, so the anchor
can silently stop matching.

This is a *scope* declaration, not a measurement. It does not change the graph's
edge count, and it is not the architecture authority: the frozen measure of
cross-directory coupling remains `scripts/import_layer_ratchet.py` (287
module-level edges over 31 directory pairs), and the authoritative cycle list is
the exact import graph described above. `tests/test_pilens_scope_config.py` pins
the scope and the formatting decision so neither can silently rot.

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
| 20 | **A cross-lane payment primitive has one home.** `sms_tool/payment_wire.py` owns the Checkout primitives (`_checkout_headers`, `_checkout_device_id`, `_is_checkout_create_url`, `_checkout_sentinel_headers`, `_checkout_post`, `_checkout_get`), the non-Checkout session factory `_new_session`, `CURRENCY_MAP`, `_PROVIDER_STAGES`, `_compact_diagnostic` and the five failure-vocabulary classes (`CheckoutNotZeroDueError`, `PayPalHttpError`, `CheckoutApprovalBlockedError`, `PayPalCapabilityError`, `PaymentOutcomeUnknownError`). A module named after one lane must not be the **definition** site for a primitive another lane needs — that is what turned `paypal_extract` into a de-facto shared payment library with the dependency direction inverted. `paypal_extract` keeps `PPLinkExtractor` and re-exports the kernel; it must not redefine it. The kernel is a leaf: inside the package it may import only `auth_headers` and `timeouts`, and `CHATGPT_TIMEOUT` comes from `timeouts` (the constant source) rather than being relayed through `pp_link_helpers`. | `grep -rn "from \.\(paypal_extract\|pp_link_helpers\|checkout_contract\|paypal_proxy\)" sms_tool/payment_wire.py` returns nothing; `grep -rn "^def _checkout_post\|^def _new_session\|^CURRENCY_MAP" sms_tool/` resolves only to `payment_wire.py` | Ownership Matrix; Dependency Direction |
| 21 | **The PayPal browser lane must not depend on the protocol lane.** `sms_tool/paypal/orchestrator.py` must not reference `paypal_reverse` — neither at module level (the original defect) nor in a function body, which would only hide the edge from a dependency scan while leaving the coupling real. The reverse-pay adapter arrives as the injected `reverse_pay` parameter, exactly as `link_factory` does for link generation (Rule 6). Omitting it raises `reverse_pay_not_injected` instead of returning a failure dict: a failure dict reads as “the reverse protocol failed” and silently falls through to the browser engines, which is the ambiguity this seam exists to remove. | `grep -rn "paypal_reverse" sms_tool/paypal/orchestrator.py` returns nothing; `grep -rn "reverse_pay=" sms_tool/commands/payment_links.py` shows the three injections | PayPal Payment Layer; Dependency Direction |

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

Rules 2–5, 7–13 and 15–21 currently hold.

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
