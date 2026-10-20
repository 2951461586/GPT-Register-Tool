# Rule 01 — Transport must not depend on registration policy

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 1`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -n "^from \.\|^import " sms_tool/http_client.py`
- **Source section**: Registration Protocol Consistency; Dependency Direction

## Rule

Transport must not depend on registration policy. `sms_tool/http_client.py` imports only `config` and `sanitizer`.
