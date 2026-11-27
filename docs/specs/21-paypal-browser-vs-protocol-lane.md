---
id: spec-21-paypal-browser-vs-protocol-lane
type: architecture-design
status: accepted
owner: sms_tool/browser
implements:
  - rule-21
depends-on:
  - adr-0012
  - spec-11-paypal-layering
---

# PayPal browser lane stays decoupled from the protocol lane

Browser-driven PayPal automation under `sms_tool/browser/` operates independently from the HTTP reverse/protocol lane so browser fallbacks cannot create circular dependencies with protocol checkout helpers.

## Enforced by

- `rule-21` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
