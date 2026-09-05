# Browser Registration Recovery

## Failure Policy

- Navigation transport errors (`NS_ERROR_ABORT`, TLS/CONNECT failures) retry
  with `commit` navigation and are recorded against proxy health.
- Missing email fields trigger one same-context reload before the batch retry
  policy creates a fresh context.
- Unknown post-OTP state gets one extended state probe; profile completion gets
  one bounded follow-up probe.
- A token with an unknown AT probe status is persisted as `at_probe_pending`.
  It is not treated as a dead account and is not sent through the post-
  registration health queue until the probe succeeds.

## Concurrency

Browser registration defaults to at most two workers. Operators can raise the
limit with `registration.browser_worker_limit` after measuring queue wait and
proxy health. Stage gates remain authoritative for shared auth/network work.

Repeated retryable failures are tracked in
`runtime/registration_retry_guard.json`. The same mailbox is cooled down after
two consecutive `network` or `auth_state` failures so a later batch cannot
spin on the same broken browser state.

## Observability

`registration_progress.jsonl` contains bounded DOM landmarks on browser
failures. Desktop IPC emits `batch_progress` events with completed/total counts
and sanitized failure classes. Proxy preflight and registration outcomes update
`runtime/registration_proxy_health.json` using host/port keys only.

Each progress row also carries `batch_id`, attempt number, driver, proxy slot,
failure class, retryability, and registration state. Browser results include
driver capability metadata so orchestration can distinguish Camoufox's
full-process recycle from Playwright context reuse.

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
