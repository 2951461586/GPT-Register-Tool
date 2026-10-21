---
id: spec-04-shared-contract-vs-concrete
type: architecture-design
status: accepted
owner: sms_tool/payment/contracts.py
implements:
  - rule-04
depends-on:
  - adr-0008
---

# Shared contracts must not import a concrete implementation

`payment/contracts.py` is the typed result contract shared by capability probes, the executor and the `pay_link` adapters. Importing an implementation would make the contract a hub and every adapter would inherit that implementation's dependencies.

## Enforced by

- `rule-04` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
