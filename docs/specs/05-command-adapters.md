---
id: spec-05-command-adapters
type: architecture-design
status: accepted
owner: sms_tool/commands/payment.py
implements:
  - rule-12
depends-on:
  - adr-0003
---

# Command adapters call the public surface only

`commands/` translates parsed CLI arguments into domain requests. It may read the public facade and nothing below it; reaching into a private module means the adapter has taken over workflow ownership, and the seam it was supposed to protect disappears.

## Enforced by

- `rule-12` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
