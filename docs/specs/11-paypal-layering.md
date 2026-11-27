---
id: spec-11-paypal-layering
type: architecture-design
status: accepted
owner: sms_tool/paypal
implements:
  - rule-05
depends-on:
  - adr-0012
---

# PayPal subpackage layering is one-way and acyclic

`sms_tool/paypal/` separates low-level proxy, fingerprint, and protocol parsing helpers from high-level extraction and authorization orchestration. Lower modules never reach back into `paypal/orchestrator.py` or `paypal/extract.py`, and `sms_tool/paypal/__init__.py` resolves public entrypoints lazily via PEP 562.

## Enforced by

- `rule-05` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
