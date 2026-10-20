# Rule 14 — Desktop code must not add a static service locator, and must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or SQLite business rules beyond display and deletion

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 14`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "ServiceLocator" SmsWorkbench/`
- **Source section**: WPF UI

## Rule

Desktop code must not add a static service locator, and must not implement ChatGPT registration, PayPal protocol details, mailbox OTP polling, or SQLite business rules beyond display and deletion.
