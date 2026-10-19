# Rule 11 — Progress and concurrency are separate

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 11`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -n "registration_progress" sms_tool/registration_concurrency.py`
- **Source section**: Registration Progress and Concurrency

## Rule

Progress and concurrency are separate. `registration_concurrency` must not write progress files or classify registration results; progress may call concurrency, never the reverse.
