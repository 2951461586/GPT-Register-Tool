---
id: spec-12-paypal-execution-vs-link-generation
type: architecture-design
status: accepted
owner: sms_tool/paypal/orchestrator.py
implements:
  - rule-06
depends-on:
  - adr-0012
  - spec-11-paypal-layering
---

# PayPal execution consumes links rather than regenerating them

PayPal authorization execution operates on an already-minted redirect or BA approval artifact. It does not invoke link-generation primitives, keeping checkout link creation and payment approval decoupled and independently retryable.

## Enforced by

- `rule-06` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
