---
id: spec-09-registration-vs-payment-lane
type: architecture-design
status: accepted
owner: sms_tool/registration/handlers.py
implements:
  - rule-22
depends-on:
  - adr-0009
---

# The registration lane must not import the payment lane

The two lanes share infrastructure (Sentinel client, proxy string authority, auth-header profiles) and nothing else. This is the one lane rule with an empty baseline: any new registration -> payment edge fails, including a function-local one, because a lazy import is the documented cycle-break rather than an exemption.

## Enforced by

- `rule-22` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
