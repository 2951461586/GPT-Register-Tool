# Rule 08 — Agent Identity is explicit-import only; the registration pipeline must not reach it

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 8`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "agent_identity" sms_tool/registration*.py sms_tool/registration_drivers/`
- **Source section**: Agent Identity Layer (Explicit Import Only)

## Rule

Agent Identity is explicit-import only; the registration pipeline must not reach it.
