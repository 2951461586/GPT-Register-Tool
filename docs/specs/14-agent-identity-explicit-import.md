---
id: spec-14-agent-identity-explicit-import
type: architecture-design
status: accepted
owner: sms_tool/importers/sub2api.py
implements:
  - rule-08
depends-on:
  - adr-0008
---

# Agent Identity upload is explicit-import only

Registration completes locally without coupling to downstream Sub2API/CPA upload endpoints. External account import happens only through explicit operator commands owned by `sms_tool/importers/`.

## Enforced by

- `rule-08` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
