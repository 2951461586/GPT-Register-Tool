# Browser Registration Recovery

## Failure Policy

- Navigation transport errors (`NS_ERROR_ABORT`, TLS/CONNECT failures) retry
  with `commit` navigation and are recorded against proxy health.
- Missing email fields trigger one same-context reload before the batch retry
  policy creates a fresh context.
- Unknown post-OTP state gets one extended state probe; profile completion gets
  one bounded follow-up probe.
- The OpenAI `/email-verification` route is classified separately. The runner
  may click one explicit continuation control, reload once, and then ends with
  `browser_email_verification_stuck` instead of hiding the cause as
  `browser_registration_state_unknown`.
- OTP acceptance in the browser runner requires destination evidence. A still
  mounted OTP form without a route transition is recorded as `pending` and is
  resolved by the post-OTP state probe.
- A token with an unknown AT probe status is persisted as `at_probe_pending`.
  It is not treated as a dead account and is not sent through the post-
  registration health queue until the probe succeeds.

## Failure Classification

`failure_registry.FAILURE_CLASSES` is the single ordered source of truth, and
`error_classification.classify_error` answers with the first class whose marker
matches. Two rules matter when reading a failure class:

- **A stage-scoped code beats a bare transport word.** `NETWORK` is tested before
  `AUTH_STATE`, and its markers include bare vocabulary (`timeout`,
  `connection`, `proxy`…). A code such as `browser_profile_submit_timeout`
  contains that vocabulary, so it used to be answered as `network` even though
  the browser lane pins it as `auth_state`. Bare transport words are now held
  back until no stage-scoped class matched; a compound token (`curl: (35)`,
  `session_circuit_open`, `connection reset`) still decides on its own.
- **Do not widen a stage class with a generic word to "catch" a failure.** Bare
  transport words also appear inside genuine defects
  (`NameError: name 'connection_pool' is not defined`), which would then be
  retried as a network error. Match the exact failure code instead.

Since 2026-09-13 `missing_auth_session_access_token`,
`browser_passwordless_otp_state_unknown`, `browser_email_value_mismatch` and
`browser_profile_submit_timeout` classify as `auth_state` (retryable) rather
than `unknown`/`network`. This is a **behaviour change**: those failures now feed
the retry guard below, so a mailbox that hits one of them twice is cooled down
instead of being retried immediately. The revert point is the marker list in
`failure_registry.py`.

There is only one shared classifier. Progress rows take their `failure_class`
from `registration_retry_decision()` too (`build_registration_result` applies it
via `setdefault`); the browser lane's `session._browser_failure_class` is the
only other place a class is assigned.

## Concurrency

Browser registration follows the requested worker count (the desktop UI allows
1-8). `registration.browser_worker_limit` is an optional extra cap: unset or 0
means no extra limit, a positive value still wins. When the browser process
pool is enabled, keep `registration.browser_process_pool.max_concurrent` at or
above the worker count or it becomes the effective cap. Stage gates remain
authoritative for shared auth/network work.

The auth-stage admission lease is acquired before allocating a browser process
or context. A run waiting for serialized auth work therefore does not consume a
browser slot. The same stage-group lease is reused as the run enters the first
auth stage.

Repeated retryable failures are tracked in
`runtime/registration_retry_guard.json`. The same mailbox is cooled down after
two consecutive `network` or `auth_state` failures so a later batch cannot
spin on the same broken browser state. Because the cooldown keys off the failure
class, a misclassified failure is invisible here — see
[Failure Classification](#failure-classification) before changing any marker.

Registration batches also expose cooperative cancellation through
`request_registration_cancel()`. Remaining accounts receive a `cancelled`
terminal result, and browser polling loops stop at their next bounded check.

## Observability

`registration_progress.jsonl` contains bounded DOM landmarks on browser
failures. Desktop IPC emits `batch_progress` events with completed/total counts
and sanitized failure classes. Proxy preflight and registration outcomes update
`runtime/registration_proxy_health.json` using host/port keys only.

Browser failure landmarks include the URL host/path and verification-input
count, which distinguishes a stalled verification route from a proxy or
browser-process failure without recording page content or secrets.

Each progress row also carries `batch_id`, attempt number, driver, proxy slot,
failure class, retryability, and registration state. Browser results include
driver capability metadata so orchestration can distinguish Camoufox's
full-process recycle from Playwright context reuse.

When synchronous promotion checking is requested, the registration command
does not enqueue a duplicate promotion-plan job. Account-health queue jobs use
renewable leases and heartbeats; expired or interrupted `running` jobs are
recovered for a later worker instead of remaining stuck indefinitely.

## Account Health

Local quota scans enforce a 15-minute batch deadline and a 6-minute per-account
deadline by default (`account_health.batch_timeout_seconds` and
`account_health.account_timeout_seconds`). Probe results are persisted before
any optional relogin; relogin failures are reported separately as
`relogin_otp_failed` or `relogin_failed` instead of blocking the entire scan.
The desktop account-scan plan passes the same deadlines to the backend and uses
a 15-minute process watchdog. Known OTP failures are held in
`runtime/account_relogin_guard.json` for the configured cooldown before another
mailbox recovery attempt is allowed.
