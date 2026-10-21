---
id: spec-02-persistence-vs-vendor-protocol
type: architecture-design
status: accepted
owner: sms_tool/store/connection.py
implements:
  - rule-02
depends-on:
  - adr-0002
---

# Persistence must not perform vendor protocol calls

`store/` owns the SQLite schema and the account/session facts. It is the only module that writes the database, and it never opens a socket: a persistence call that also talks to a vendor makes every write non-deterministic and untestable offline.

## Enforced by

- `rule-02` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
