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
| `mailbox_service.py`, `providers/` | Mailbox routing, polling and provider transport | Registration success |
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

## Registration Modules

See [registration architecture](current/registration-architecture.md) for the
state groups, session ownership, error policy and compatibility guarantees.
See [account health contract](current/account-health.md) for probe/recovery
side effects, result semantics and proxy precedence.
[ADR-0009](adr/0009-registration-hardening.md) records the decisions.

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

### Current Violations

Verified against the working tree on 2026-09-07:

- **Rule 1 — resolved 2026-09-07.** Transport no longer imports registration
  policy. The shared pieces were pushed *down* instead of hidden in a lazy
  import: pure backoff maths moved to `sms_tool/backoff.py` and terminal-error
  classification to `sms_tool/error_classification.py`
  (`is_terminal_registration_error`). `http_client.py` now imports from those
  two only. Do not "restore" the `registration_retry_decision(...)` call —
  it is exactly equivalent and reintroduces the layering violation.
- **Rule 6 — open.** `sms_tool/paypal/orchestrator.py:16` is
  `from ..gen_pp_link import generate_pp_link`. The PayPal layer regenerates
  links instead of consuming the one passed to the adapter.
- **Rule 14 — needs a decision.** `sms_tool/paypal/config_picker.py:16` is
  `from ..storage import upsert_account`, i.e. the PayPal layer persists SQLite
  rows directly. The same snapshot also assigns *result persistence* to
  `paypal.config_picker` in its module table, so the two statements conflict and
  the rule needs to be restated before it can be enforced.

Rules 2–5, 7–13 and 15 currently hold.

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

| Code | Meaning | Desktop log level |
| --- | --- | --- |
| 0 | Normal completion | Information, Warning if IPC payload is absent |
| 2 | Empty/malformed explicit mailbox source | Warning |
| 3 | Provider/import failure | Warning |
| Negative / timeout | Abnormal process termination | Error |

See [telemetry and runtime data](current/telemetry-and-runtime.md) for
`command_id`, `run_id`, schema version and test/live separation.

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
