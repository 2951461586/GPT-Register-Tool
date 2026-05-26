# Dependency Audit

This repository keeps a single `requirements.txt` for convenience, but the runtime dependencies fall into clear feature groups.

| Package | Used by | Notes |
| --- | --- | --- |
| `curl_cffi` | registration, mailbox, session refresh, Codex OAuth, CPA import, CF Worker mailboxes | Core transport layer. |
| `requests` | `gen_pp_link`, `paypal_auto`, `paypal_nocard`, `paypal_reverse`, `smsbower`, `providers/luckmail_token.py` | Core HTTP client for a few vendor flows. |
| `camoufox[geoip]` | `paypal_auto` | Optional anti-detect browser path for PayPal auto-pay. |
| `browserforge` | `paypal_auto` | Optional screen-fingerprint helper for the Camoufox path. |
| `cloakbrowser` | `registration`, `session_refresh`, `paypal_auto` | Optional browser fallback for auth/payment flows. |
| `playwright` | `captcha_solver` | Optional CAPTCHA solving path. |

Audit result: no third-party package in the current manifest was unused enough to remove safely. The real cleanup here is boundary clarity: optional browser/CAPTCHA packages stay isolated to the flows that need them.
