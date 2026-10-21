---
id: spec-03-provider-adapters
type: architecture-design
status: accepted
owner: sms_tool/providers
implements:
  - rule-03
depends-on:
  - adr-0001
---

# Provider adapters must not import registration workflows

`providers/` holds the mailbox wire clients. They are the innermost layer for their vendor and must stay callable from a single function. Importing a registration workflow would invert the layer order and make a provider unusable outside the registration lane.

## Enforced by

- `rule-03` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
