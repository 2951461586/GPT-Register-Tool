---
id: spec-17-cli-lazy-command-seams
type: architecture-design
status: accepted
owner: sms_tool/cli.py
implements:
  - rule-15
  - rule-23
depends-on:
  - spec-05-command-adapters
---

# CLI command modules are lazy, self-healing seams

Importing `sms_tool.cli` stays fast and free of heavy transport/provider stacks by resolving command adapters through `_LazyModule` and self-healing slot resolvers (never a one-shot latch that breaks after test patching).

## Enforced by

- `rule-15`, `rule-23` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
