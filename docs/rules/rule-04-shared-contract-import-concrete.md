# Rule 04 — A shared contract does not import a concrete provider: `checkout_contract` stays provider-free

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 4`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -n "^from \.\(wallet_provider\|wallet_transport\|pay_link\|paypal\|gcash\)" sms_tool/checkout_contract.py`
- **Source section**: Dependency Direction

## Rule

A shared contract does not import a concrete provider: `checkout_contract` stays provider-free.
