# Registration Architecture

## Protocol workflow

`registration.py` binds an immutable `RegistrationOperations` per call. Existing
facade patches made before `run_email` are respected; changing a facade symbol
mid-run no longer changes an already bound workflow. Isolated tests may inject
a fake operations object.

`registration_runtime.py` separates mutable state into resources, identity,
auth, OTP, account and outcome groups. Flat field access is a transitional
compatibility view of the same storage, not a second copy. Checkpoint and result
schemas remain unchanged. New stage code should consume the smallest group it
needs. Full immutable stage input/output migration remains future work.

The email-registration path is AT-only. Its impossible OAuth/phone readiness
branches have been removed. The separate SMS entry point, account recovery,
payment authentication and OAuth export still use phone/OAuth modules, so those
modules must not be deleted as dead code.

## Browser drivers

`base.py` remains the driver registry. `external_sessions/__init__.py` preserves
the existing import/factory Interface; `managed.py` owns engine/API lifecycle,
`profiles.py` owns persistence configuration, and `probe.py` owns egress audits.
Per-engine implementations remain together in `managed.py`; extracting each
engine separately is not required for the current Interface.

Camoufox removes only a profile created by its own `mkdtemp`. User-supplied
profile directories are never removed. A successful `keep_browser_open` run
retains its profile for operator inspection; failed launch always attempts
cleanup. Windows file-lock failures emit a warning and retain ownership for a
later close attempt.

`platform_patches.py` constructs the child process environment. It preserves
the existing `disable_content_sandbox` option without modifying `os.environ` or
the process-wide asyncio policy. This option reduces Firefox sandbox protection;
disable the workaround where the host supports normal sandboxed operation.

## Error and retry policy

`registration_policy.py` owns retry classification and actionable advice,
replacing the unused `error_advice.py` table. Result assembly, batch retry and
mailbox registration cooldown use the same decision.

Session-level HTTP circuits, stage admission, persistent mailbox cooldown and
stage time budgets keep distinct state owners. HTTP backoff and admission
cooldown use shared bounds. Challenges, explicit credential failures, rate-limit
circuits and exceeded stage budgets must not become transport retries merely
because the error text also includes `timeout` or `proxy`.

iCloud inbox 404/410 stops the current OTP poll and opens a five-minute cooldown.
An explicit credential failure remains quarantined until repaired. An empty
inbox or HTTP 503 is not treated as invalid credentials. Reads check quarantine
before contacting a provider.

## Validation limits

Offline regressions cover disabled/read-only email selection, bounded fill
timeouts, fallback-selector budgets, terminal mailbox failures and session
cleanup. They do not establish live registration success rates or explain
provider-side account revocation.

Do not use historical mixed test/live progress rows to claim a success-rate
trend. A Gmail/ReMail comparison requires explicit sample and budget approval,
valid owned mailbox credentials, matched conditions and separate run IDs.
