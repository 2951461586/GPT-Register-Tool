# Vendored Sentinel bridge (UPI / payment risk layer)

Source: <https://github.com/wangshen233/upi-zero-link>
(`upi_zero_link/_vendor/sentinel_assets/`)

License: MIT — upstream `LICENSE`:
<https://github.com/wangshen233/upi-zero-link/blob/main/LICENSE>

| File | Role |
| --- | --- |
| `sentinel_bootstrap.js` | Browser-global shim the SDK needs inside the Node VM |
| `sentinel_bridge.js` | `SentinelSDK.__proto2(flow)` driver: `/sentinel/req`, turnstile, PoW, SO |
| `sentinel_sdk.js` | Snapshot of OpenAI's `sdk.js` the bridge is pinned to |
| `sentinel_curl_fetch.py` | `curl_cffi` HTTP transport used for `https://` / SOCKS proxies |

Local modifications: **none** — the four files are verbatim. The directory is
excluded from pi-lens scans via the repo-root `.pi-lens.json` `ignore` (the
upstream `json()` helper intentionally throws on a bad body, and the vendored JS
is not ours to restyle). This project's own wrapper is `../upi_sentinel.py`; it
sets `PYTHON_BIN` / `SENTINEL_PYTHON` to `sys.executable` so the transport
helper uses the project virtualenv, and it drives the bridge over stdin/stdout.

## Why this exists

The registration runner in `sms_tool/sentinel/` mints only
`openai-sentinel-token`. The ChatGPT `backend-api/payments/checkout/approve`
gate also reads `openai-sentinel-so-token` (the signed session-observer token),
and it is the reason UPI approve answered `result=blocked` on every attempt
with the protocol fallback. This bridge's `SentinelSDK.__proto2(flow)` returns
both the turnstile proof and the SO.

## Refreshing

The SDK snapshot ages. If Checkout/approve starts rejecting the Sentinel token
after an upstream release, drop in a newer `sentinel_sdk.js` and update the
version string (`UPI_SENTINEL_VERSION`, default in `../upi_sentinel.py`).
