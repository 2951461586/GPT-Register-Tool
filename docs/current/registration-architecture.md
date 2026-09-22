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

`RegistrationStageRunner.run_stage` is the only stage execution Seam. Each stage
mutates the same `RegistrationRuntimeState`; there is no test-only dict-delta
stage framework or parallel cleanup lifecycle.

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

The process-wide browser pool key includes a hash of the injected runtime
configuration. Changing driver credentials or process launch settings rebuilds
the pool; a resident relaunch never falls back to the first invocation's stale
configuration.

Browser hardware fingerprints come from the built-in
`BROWSER_PROFILE_POOL`. `registration.browser_profile_pool` is rejected rather
than accepted as a configuration value that has no runtime effect.

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

Protocol bootstrap reads a post-create checkpoint before saving any early-stage
state. SQLite rejects early-stage overwrites of post-create checkpoints, including
across processes. A saved AT resumes at the AT probe without mailbox reads or OTP.

Candidate selection bulk-reads post-create checkpoints before batch sizing.
An expired, exhausted or malformed `auth_session_pending` checkpoint is written
to the retry guard and excluded before it can claim a worker/proxy slot. A valid
checkpoint remains eligible for the recovery path.

A successful create response without an AT saves `auth_session_pending` with
private, scoped session cookies. Recovery fetches the session and probes the AT;
it never repeats signup or sends another OTP. Recovery is limited to two attempts
within fifteen minutes of account creation. Missing/expired context or an exhausted
budget is a terminal recovery result, not permission to start signup again.

`user_already_exists` is persisted immediately as `partial_registered` in the
registration retry guard. The desktop displays this as **半注册** and excludes it
from unused-mailbox registration. This label means the server has an account but
this registration has not established a usable local account; it is not a claim
that the server account was only partially created. Legacy `dead_end` records are
read as the same state. A failed recovery retains it; a successful recovery clears
the guard, and verified account data takes precedence in the desktop grid.

`registration_policy.py` owns retry classification, disposition and actionable
advice. Its pure `RetryDecision` separates three questions:

- `attempt_retryable`: may this account retry immediately inside the same batch;
- `future_batch_eligible`: may a later batch reconsider the mailbox;
- `guard_action`: retain no state, cooldown, escalate OTP pending, or mark a
  permanent dead end.

The historical result key `retryable` remains an alias for
`attempt_retryable`. `email_otp_send_stuck` never retries immediately: the first
cross-batch observation enters `registration.retry_policy.cross_batch_cooldown_seconds`,
and the second (configurable threshold) enters OTP-pending quarantine. This
quarantine is not `partial_registered`; it makes no claim that an account exists.
A success clears either state.

Pulse scheduling starts with a one-account canary when
`registration.pulse.canary_enabled` is true. A blocked canary or unanimously
blocked full wave advances the proxy-pool cursor for accounts not yet started,
cools down, and keeps the next wave as a canary. With only one pool slot the log
reports cooldown only. Protocol and browser results both carry the same
allow-listed `proxy_audit` fields: pool index, countries, scheme, and rotation
generation. No proxy URL, host, credential, or session ID is included.

Session-level HTTP circuits, stage admission, persistent mailbox cooldown and
stage time budgets keep distinct state owners. HTTP backoff and admission
cooldown use shared bounds. Challenges, explicit credential failures, rate-limit
circuits and exceeded stage budgets must not become transport retries merely
because the error text also includes `timeout` or `proxy`.

iCloud inbox 404/410 stops the current OTP poll and opens a five-minute cooldown.
The pre-OTP baseline propagates this error too, stopping before authorization or
OTP dispatch. Explicit mailbox credential failures also stop at this boundary.
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
