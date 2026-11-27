---
id: spec-15-progress-vs-concurrency
type: architecture-design
status: accepted
owner: sms_tool/registration/concurrency.py
implements:
  - rule-11
depends-on:
  - adr-0005
  - adr-0006
---

# Progress reporting and concurrency control stay separate

Stage gates, semaphores, and pulse pacing live in `sms_tool/registration/concurrency.py` and `sms_tool/registration/pulse.py`, while progress event formatting lives in `sms_tool/registration/progress.py`. Concurrency limits never couple to UI progress formatting.

## Enforced by

- `rule-11` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
