# v2026.08.05

This release completes the account-pool cleanup and tightens the registration,
mailbox, proxy, and desktop boundaries.

## Account pool

- Removed 10 terminal rows classified as dropped/deactivated or missing/invalid
  AT. The active SQLite pool now contains 419 rows with no missing AT records.
- Unknown network/proxy results were retained for recheck instead of being
  treated as account failures.
- Added `sms_tool.account_cleanup` and the dry-run-by-default
  `scripts/cleanup_invalid_accounts.py`. Applying cleanup creates a database
  backup and archives matching session files before removing mailbox-pool lines.

## Module and protocol changes

- Registration proxy resolution now uses only registration/default settings and
  distributes healthy static proxies across workers and retries.
- Mailbox selection accepts iCloud receive URLs through the mixed compatibility
  route; iCloud listing and message-detail fetching are separate operations and
  OTP polling ignores the initial snapshot.
- Proxy credentials are normalized at transport boundaries and redacted from
  liveness, Sentinel, and registration errors.
- WPF selected-mailbox and failed-account reruns share one format-aware mailbox
  file builder, keeping provider parsing out of window handlers.

## Cleanup and verification

- Removed generated Python/.NET test caches and intermediate build output; local
  `config.json`, `sessions/`, and `runtime/` remain ignored operator state.
- Verified `pytest -q` (620 passed, 1 skipped, 22 subtests) and Python bytecode
  compilation. The .NET SDK selector now accepts the installed 10.0.302 feature
  band while CI remains pinned by `global.json` compatibility rules.
