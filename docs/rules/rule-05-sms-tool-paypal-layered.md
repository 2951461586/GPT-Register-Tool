# Rule 05 — `sms_tool/paypal/` is layered one-way and acyclic; `paypal/errors.py` is a dependency-free leaf importable from every layer

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 5`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -n "^from \.\|^import" sms_tool/paypal/errors.py`
- **Source section**: PayPal Payment Layer

## Rule

`sms_tool/paypal/` is layered one-way and acyclic; `paypal/errors.py` is a dependency-free leaf importable from every layer.
