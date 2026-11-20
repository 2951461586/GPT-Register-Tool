# Rule 13 — Proxy string authority is single

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 13`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "^def \(parse_proxy\|rebuild_proxy_credentials\|retarget_region\|rotate_session\|infer_region\|infer_proxy_country\)(" sms_tool/` — `proxy/entry.py` only
  — and, since 2026-11-20, the three string helpers
  `normalize_proxy_url` / `redact_proxy_url` / `redact_proxy_text`, which moved
  out of `phone_proxy` for the same reason (ADR-0011 amendment).
- **Source section**: Proxy Routing Boundary

## Rule

Proxy string authority is single. **2026-11-20 amendment:** the three string
helpers (`normalize_proxy_url` / `redact_proxy_url` / `redact_proxy_text`) are
defined in `proxy/entry.py` too; `phone_proxy` keeps same-signature delegating
shells (including `empty_placeholder`'s two lane-specific defaults), so no caller
or patch target moved. The payment lane imports `proxy.entry` directly, which is
what cut `payment -> registration` from 15 edges to 7. Only `proxy_entry` defines `parse_proxy` / `rebuild_proxy_credentials` / `retarget_region` / `rotate_session` / `infer_region` / `infer_proxy_country`; `phone_proxy` and `paypal_proxy` are thin wrappers. **Added 2026-10-18:** `infer_proxy_country` moved into `proxy_entry` because the registration lane was importing the PayPal-named `paypal_proxy` purely to learn its exit country — the definition site is a proxy/geo concern, not a payment one. `paypal_proxy.infer_proxy_country` remains a delegating shell so existing importers and patch targets keep working.
