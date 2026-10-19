# Rule 02 — Persistence performs no vendor protocol calls

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 2`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -rn "^from \.\.\|^from sms_tool\|^import requests\|^import httpx\|curl_cffi" sms_tool/store/`
- **Source section**: Storage Layer; Ownership Matrix

## Rule

Persistence performs no vendor protocol calls. `sms_tool/store/**` imports nothing above `account_models` / `config` / `paths`, and no HTTP client.
