# Rule 06 — PayPal execution must not regenerate links: no `gen_pp_link` import inside `sms_tool/paypal/`

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 6`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "gen_pp_link" sms_tool/paypal/`
- **Source section**: PayPal Payment Layer; Payment Responsibility Boundary

## Rule

PayPal execution must not regenerate links: no `gen_pp_link` import inside `sms_tool/paypal/`.
