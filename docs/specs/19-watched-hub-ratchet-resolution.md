---
id: spec-19-watched-hub-ratchet-resolution
type: architecture-design
status: accepted
owner: scripts/mailbox_private_import_ratchet.py
implements:
  - rule-18
depends-on:
  - spec-08-hub-private-surface
---

# Watched-hub ratchets resolve targets from the imported module

Governance ratchets watching private symbol imports must resolve the target hub from the AST import target (including relative imports and aliases), never from fragile text grep over call sites.

## Enforced by

- `rule-18` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
