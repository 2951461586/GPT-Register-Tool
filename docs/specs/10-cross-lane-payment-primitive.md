---
id: spec-10-cross-lane-payment-primitive
type: architecture-design
status: accepted
owner: sms_tool/operation_journal.py
implements:
  - rule-20
depends-on:
  - adr-0002
---

# A payment primitive shared by both lanes is not payment-named

`operation_journal` is the durable idempotency and recovery journal. `commands/registration.py` uses it for the same restart-survival reason the payment lane does, which is exactly why it is provider-neutral and not named after a payment concern.

## Enforced by

- `rule-20` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
