---
id: spec-16-desktop-di-and-disposal
type: architecture-design
status: accepted
owner: SmsWorkbench
implements:
  - rule-14
depends-on:
  - adr-0013
---

# Desktop code avoids static service locators and owns disposal

The WPF host (`SmsWorkbench`) wires services via explicit constructor injection and disposes managed resources deterministically rather than hiding dependencies behind a static service locator.

## Enforced by

- `rule-14` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
