---
id: spec-24-broad-except-hygiene-budget
type: architecture-design
status: accepted
owner: scripts/broad_except_ratchet.py
implements:
  - rule-26
depends-on:
  - adr-0009
---

# Broad exception handlers stay bounded by a shrinking ratchet

Kept `except Exception:` handlers must be narrowed to concrete tuples where possible, never silently swallow exceptions in the `protocol_registration` lane (`silent = 0`), and stay within the per-file and global budgets enforced by `scripts/broad_except_ratchet.py`.

## Enforced by

- `rule-26` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
