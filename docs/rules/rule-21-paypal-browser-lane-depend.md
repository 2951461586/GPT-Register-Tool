# Rule 21 — The PayPal browser lane must not depend on the protocol lane

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 21`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "paypal_reverse" sms_tool/paypal/orchestrator.py` returns nothing; `grep -rn "reverse_pay=" sms_tool/commands/payment_links.py` shows the three injections
- **Source section**: PayPal Payment Layer; Dependency Direction

## Rule

**The PayPal browser lane must not depend on the protocol lane.** `sms_tool/paypal/orchestrator.py` must not reference `paypal_reverse` — neither at module level (the original defect) nor in a function body, which would only hide the edge from a dependency scan while leaving the coupling real. The reverse-pay adapter arrives as the injected `reverse_pay` parameter, exactly as `link_factory` does for link generation (Rule 6). Omitting it raises `reverse_pay_not_injected` instead of returning a failure dict: a failure dict reads as “the reverse protocol failed” and silently falls through to the browser engines, which is the ambiguity this seam exists to remove.
