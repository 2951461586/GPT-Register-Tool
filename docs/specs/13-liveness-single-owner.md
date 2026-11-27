---
id: spec-13-liveness-single-owner
type: architecture-design
status: accepted
owner: sms_tool/accounts/account_liveness.py
implements:
  - rule-07
depends-on:
  - adr-0002
---

# Account liveness probing has a single owner

`sms_tool/accounts/account_liveness.py` is the sole authority for side-effect-free ChatGPT access-token liveness probes. Recovery, batch health checks, and payment auth gates delegate probing here instead of re-implementing HTTP probe logic.

## Enforced by

- `rule-07` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
