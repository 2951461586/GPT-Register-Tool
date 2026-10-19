# Rule 20 — A cross-lane payment primitive has one home

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 20`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "from \.\(paypal_extract\|pp_link_helpers\|checkout_contract\|paypal_proxy\)" sms_tool/payment_wire.py` returns nothing; `grep -rn "^def _checkout_post\|^def _new_session\|^CURRENCY_MAP" sms_tool/` resolves only to `payment_wire.py`
- **Source section**: Ownership Matrix; Dependency Direction

## Rule

**A cross-lane payment primitive has one home.** `sms_tool/payment_wire.py` owns the Checkout primitives (`_checkout_headers`, `_checkout_device_id`, `_is_checkout_create_url`, `_checkout_sentinel_headers`, `_checkout_post`, `_checkout_get`), the non-Checkout session factory `_new_session`, `CURRENCY_MAP`, `_PROVIDER_STAGES`, `_compact_diagnostic` and the five failure-vocabulary classes (`CheckoutNotZeroDueError`, `PayPalHttpError`, `CheckoutApprovalBlockedError`, `PayPalCapabilityError`, `PaymentOutcomeUnknownError`). A module named after one lane must not be the **definition** site for a primitive another lane needs — that is what turned `paypal_extract` into a de-facto shared payment library with the dependency direction inverted. `paypal_extract` keeps `PPLinkExtractor` and re-exports the kernel; it must not redefine it. The kernel is a leaf: inside the package it may import only `auth_headers` and `timeouts`, and `CHATGPT_TIMEOUT` comes from `timeouts` (the constant source) rather than being relayed through `pp_link_helpers`.
