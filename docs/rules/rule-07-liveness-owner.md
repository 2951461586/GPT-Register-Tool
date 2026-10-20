# Rule 07 — One liveness owner

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 7`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "backend-api/me" sms_tool/`
- **Source section**: Account Liveness Contract

## Rule

One liveness owner. Only `account_liveness` defines the `/backend-api/wham/usage` probe; registration, JIT payment auth and protocol payment adapters must not define their own `/backend-api/me` probe.
