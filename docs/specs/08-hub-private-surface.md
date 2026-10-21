---
id: spec-08-hub-private-surface
type: architecture-design
status: accepted
owner: sms_tool/mailbox/router.py
implements:
  - rule-16
depends-on:
  - adr-0001
---

# A hub module's private surface is consumed through public names

`mailbox/router.py` is the mailbox hub. Its 16 consumed underscore symbols are public API now, and the private-import baseline for it is zero. New consumers of its private names fail the ratchet; the fix is to publish the name, not to add a baseline entry.

## Enforced by

- `rule-16` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
