---
id: spec-06-ipc-writer
type: architecture-design
status: accepted
owner: sms_tool/desktop_ipc.py
implements:
  - rule-09
depends-on:
  - adr-0013
---

# IPC envelopes are written by one module

`desktop_ipc` owns the versioned envelope the WPF host reads. A second writer would let the wire format fork between the resident server and the one-shot fallback, which is the failure mode the single-writer rule exists to prevent.

## Enforced by

- `rule-09` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
