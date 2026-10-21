---
id: spec-07-proxy-string-authority
type: architecture-design
status: accepted
owner: sms_tool/proxy/entry.py
implements:
  - rule-13
depends-on:
  - adr-0011
---

# One module is the proxy-string authority

`proxy/entry.py` parses, rebuilds, retargets and rotates proxy strings. `phone_proxy` and `paypal/proxy.py` are thin wrappers around it. A fourth parser is a defect: divergent credential handling and divergent redaction is how proxy passwords leak into logs.

## Enforced by

- `rule-13` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
