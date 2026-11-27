---
id: spec-20-duplicate-function-name-semantics
type: architecture-design
status: accepted
owner: sms_tool/kernel
implements:
  - rule-19
depends-on:
  - adr-0011
---

# Shared helper deduplication verifies semantic equivalence

Two modules defining a same-named helper are consolidated into `sms_tool/kernel/` or a single owning authority only when their inputs, edge cases, and failure contracts genuinely match.

## Enforced by

- `rule-19` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
