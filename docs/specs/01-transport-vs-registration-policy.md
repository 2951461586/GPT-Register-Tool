---
id: spec-01-transport-vs-registration-policy
type: architecture-design
status: accepted
owner: sms_tool/http_client.py
implements:
  - rule-01
depends-on:
  - adr-0009
---

# Transport must not depend on registration policy

`http_client` is shared transport. It carries retries, timeouts and proxy plumbing, and it is imported by both lanes. Reaching into registration policy would make the transport module a second owner of retry/advice decisions and would drag the registration lane into every consumer.

## Enforced by

- `rule-01` — the rule file carries the check that proves this boundary.
- `scripts/spec_validate.py` — resolves the `implements` / `depends-on` refs above and fails
  on a dangling one, so a renumbered rule or a reverted ADR breaks this spec rather than
  silently describing a decision that no longer exists.
