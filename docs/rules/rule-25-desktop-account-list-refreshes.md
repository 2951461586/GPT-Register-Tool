# Rule 25 — The desktop account list refreshes incrementally; the full payload is the anti-drift anchor

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 25`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `pytest tests/workbench/test_desktop_read_delta.py`; `dotnet test` (`PoolViewModelTests.MergeDatabaseRows_*`, `DesktopReadClientResidentStderrTests.ResidentAccountsDeltaSendsTheCursor`)
- **Source section**: WPF UI; CLI

## Rule

**The desktop account list refreshes incrementally; the full payload is the anti-drift anchor.** `sms_tool/desktop_read.read_accounts_delta` returns `accounts` (only rows whose `updated_at` moved past `since`), the complete `ids` set, `cursor` (= max `updated_at`) and `full`. The resident `accounts` / `pools` ops carry the same fields, additively, so the protocol version does not move and one-shot readers keep their list shape. The client merges by `RawLine` (= numeric id), drops database rows absent from `ids`, keeps the operator's checkbox, and forces a full rebuild every 10 deltas or on any channel error. Measured on 434 accounts: ~623 KB full versus ~3.6 KB for an idle delta; mailbox-pool rows are deliberately untouched by this path because their owner is the pool-file read.
