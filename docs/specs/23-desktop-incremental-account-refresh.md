---
id: spec-23-desktop-incremental-account-refresh
type: architecture-design
status: accepted
owner: SmsWorkbench/ViewModels
implements:
  - rule-25
depends-on:
  - spec-06-ipc-writer
  - spec-16-desktop-di-and-disposal
---

# Desktop account list refreshes incrementally

The WPF account grid updates rows incrementally on progress/completion events instead of triggering full table reloads on every event frame.

## Enforced by

- `rule-25` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
