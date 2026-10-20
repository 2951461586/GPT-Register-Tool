# Rule 03 — Provider adapters must not import the registration or payment workflow

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 3`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "from \.\.\(registration\|payment\|paypal\)" sms_tool/providers/`
- **Source section**: Dependency Direction; Ownership Matrix

## Rule

Provider adapters must not import the registration or payment workflow.
