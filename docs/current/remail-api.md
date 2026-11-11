# ReMail Open API Contract (provider wire)

> **Scope.** The ReMail supplier's HTTP contract as this repository uses it, plus
> the polling policy built on top of it. The mailbox *architecture* (provider
> capabilities, error disposition, proxy candidates) stays in
> [mailbox.md](mailbox.md); registration stages stay in
> [registration-architecture.md](registration-architecture.md).
>
> **Owner.** `sms_tool/providers/mailbox_remail.py`. `sms_tool/mailbox/remail.py`
> is a compatibility facade (ADR-0001) and holds no wire logic.

## Source of truth

Captured 2026-11-11 from the supplier's own machine-readable document:

```bash
curl -sS https://remail.best/openapi.json     # OpenAPI 3.0.3, info.version 1.0.0
```

The rendered page (`https://remail.best/docs`) is a JavaScript app and cannot be
scraped; `/openapi.json` is the authoritative artifact and is what this file
transcribes. `https://remail.best/llms.txt` and `/docs.json` are not documents —
they answer with the SPA shell.

The base URL is **configurable** (`email_registration.remail.base_url`); the
deployment this repo runs against is `https://remail.aishop6.com`, not
`remail.best`. Paths and payloads are identical (both serve version 1.0.0);
anything that changes below must be re-verified against the configured host,
because a mirror can lag.

## Authentication

| Surface | Credential | Notes |
| --- | --- | --- |
| `/v1/open/*` | `Authorization: Bearer rk-…` (`securitySchemes.remailApiKey`, `type: http`, `scheme: bearer`) | one API key per supplier account; `GET /v1/open/apikey/profile` reports its own quota, balance and expiry |
| `/v1/pickup*` | **no API key**: `email` + `token` (`serviceToken` from the order) | possession of the delivered mailbox is the authorization; the email must be the *exact* delivered address |

Both are secret material: the API key and every `serviceToken` are redacted in
logs (`_redact`, `secrets=(token,)`) and must never be written to a report.

## Endpoints

### Used by this repository

| Method | Path | Purpose here |
| --- | --- | --- |
| POST | `/v1/open/orders` | single mailbox order (`--buy-remail-mailbox` with `count=1`) |
| POST | `/v1/open/orders/batch` | batch order (`count=2..100`) |
| GET | `/v1/open/orders/{orderNo}` | order detail; also service-token recovery |
| GET | `/v1/open/orders` | ambiguous-batch recovery (`search` = delivered email) |
| GET | `/v1/pickup` | OTP poll: messages + server-side fetch state |
| GET | `/v1/pickup/messages/{messageId}` | full body of one message (only when the preview is not enough) |
| GET | `/v1/open/apikey/profile` | optional preflight: key status, balance, quota |

### Available, deliberately unused

| Surface | Why not |
| --- | --- |
| `POST /v1/pickup/batch` | the registration lane polls one mailbox per account; batching 2..100 pickups buys nothing for a single OTP wait and would couple two accounts' failures |
| `GET /v1/open/wallet`, `/v1/open/wallet/transactions`, `/v1/open/recharges`, `POST /v1/open/cards/redeem` | operator-facing billing; `apikey/profile.balance` already answers "can we order" |
| `Resources` / `Proto` / `domains` / `servers` | inventory management for supplier-account holders; this repo only buys orders |

## Orders

### `POST /v1/open/orders`

Required header `Idempotency-Key` (1–128 chars): *"相同 API Key 下，相同幂等键不会重复创建业务事实"*.

| Query | Enum | Default |
| --- | --- | --- |
| `serviceMode` | `purchase` (long-lived mailbox) / `code` (short-lived OTP inbox) | supplier default |
| `supply` | `private_first` / `public_only` | `private_first` |

Body — `CreateOrderRequest`, `additionalProperties: false`:

```json
{"projectId": 2, "emailSuffix": "icloud.com"}
```

`emailSuffix` semantics we depend on: a **concrete** suffix (`icloud.com`,
`outlook.com`, `proton.me`, `daum.net` …) selects exactly that inventory and
**does not fall back** to another suffix when it is out of stock; `outlook` and
`domain` are the weighted-random selectors; a full address is rejected.

Response: `Order` (`200`/`201`).

### `POST /v1/open/orders/batch`

Required header `Idempotency-Key`; `quantity` is `2..100`; the suffix is drawn
**once** and every sub-order uses it. Responses: `200`, `201`, `207` (batch
completed with one or more failed items) — all three are success codes for a
transport that only checks `2xx`.

```json
[{"index": 0, "status": "succeeded", "order": {…}},
 {"index": 1, "status": "failed", "error": {"code": "insufficient_inventory", "message": "…"}}]
```

`error.code` enum: `insufficient_balance` · `insufficient_inventory` ·
`idempotency_conflict` · `temporarily_unavailable`. Each names a different
operator action, so `_batch_failure_reason()` maps them to distinct errors
(`remail_insufficient_balance`, `remail_insufficient_inventory`,
`remail_idempotency_conflict`, `remail_temporarily_unavailable`) instead of the
generic "batch returned no usable mailboxes".

**Idempotency is what makes the retry safe.** The document promises that
re-using the key converges on the same complete batch result, so a transient
Cloudflare 5xx must be retried rather than aborting acquisition. Both the single
and the batch path now go through `_post_remail_order()` with one key generated
per attempt group (`_remail_idempotency_key`, salted with the batch id and the
order round).

### `Order` fields consumed here

| Field | Use |
| --- | --- |
| `orderNo`, `id`, `deliveryEmail`, `serviceToken` | mailbox identity; `serviceToken` is what `/v1/pickup` needs |
| `serviceMode`, `supplyPolicy`, `projectId`, `createdAt` | ambiguous-batch recovery matching (exact quantity, window, mode, project, suffix) |
| `status` | `pending_payment` · `paid` · `active` · `completed` · `refunded` · `failed` · `closed` |
| `failureCode` | `unknown` · `insufficient_inventory` · `insufficient_balance` · `allocation_failed` · `service_token_failed` · `activation_failed` |
| `payAmount`, `refundAmount` | recorded price / refunds (`--max-remail-cost` accounting) |
| `receiveUntil` | short-lived `code` orders: the deadline quoted when the token is gone |
| `productType`, `allocationType` | `icloud` orders without a `serviceToken` cannot be polled — surfaced explicitly instead of a null-token poll |

`GET /v1/open/orders` paging: `afterId` + `limit` (≤100) → `{items, total,
offset, nextAfterId, hasNext, limit}`; filters `status`, `serviceMode`, `search`.

## Pickup

### `GET /v1/pickup?email&token`

No API key. `email` must be the **delivered** address — the Daum family
(`@daum.net` / `@hanmail.net` / `@hanmail2.net`) are distinct orders and the
address may not be rewritten or re-suffixed.

```json
{"items": [MailMessage…], "fetch": FetchState}
```

`MailMessage`: `sender` · `recipient` · `receivedAt` · `subject` · `bodyPreview` ·
`verificationCode` (all required except `id`/`verificationCode`); `id` is present
only when positive and is the handle for the detail endpoint.

`FetchState` (the server-side fetch job):

| Field | Enum / type | How it is used |
| --- | --- | --- |
| `lastStatus` | `pending` · `processing` · `normal` · `abnormal` | `normal` = healthy (no backoff), `pending`/`processing` = not ready (2 s floor), `abnormal` = supplier fetch failed (3 s floor + one log line from `lastSafeError`) |
| `nextFetchAllowedAt` | ISO-8601 | hard floor on the next pickup (clamped to ≤5 s so a long server hint cannot stall the OTP wait) |
| `lastReceivedAt` | ISO-8601 | unchanged ⇒ no new mail ⇒ mild backoff |
| `lastSafeError` | string | printed once per distinct value when `lastStatus` is failing |

> **Drift fixed 2026-11-11.** This repo used to test `lastStatus == "succeeded"`,
> which version 1.0.0 never sends. Every pickup therefore took the "not ready"
> branch and the 1 s ReMail-specific poll interval degraded to 2 s on every poll.
> `succeeded`/`failed` are still accepted as aliases for a pre-1.0 supplier build.

### `GET /v1/pickup/messages/{messageId}?email&token`

`MailMessageDetail` = `MailMessage` + required `body`. Fetching the body is a
second round-trip per message, so the poll only asks for it when no message
carries a usable `verificationCode` / preview code.

## Errors

Wire error body — `ErrorResponse`, `additionalProperties: false`:

```json
{"code": "…optional stable code…", "message": "…", "requestId": "…", "fields": {"field": "reason"}}
```

`message` and `requestId` are required; `requestId` is what the operator quotes to
the supplier, so it is preserved in `ReMailHttpError` (`_remail_request_id`) and
surfaced in credential/auth failures.

| Condition | Disposition here |
| --- | --- |
| `429`, `500`, `502`, `503`, `504` | retryable: order path retries under the same `Idempotency-Key` (`order_retry_attempts`, backoff capped at 30 s); poll path walks the proxy candidates (`transport_retry_attempts`) |
| other `5xx` after retries, or a batch that produced nothing | `_recover_recent_remail_batch()` looks the exact orders up by window + mode + project + suffix before giving up |
| `401`/`403` on `/v1/pickup` | `ReMailCredentialError` → terminal quarantine of that mailbox (never the API key's fault) |
| `401`/`403` on `/v1/open/*` | surfaced as API-key failure; preflight falls back to `GET /docs` and never raises |
| `200` with `{"raw": …}` | the body was not JSON — recorded verbatim (≤500 chars) rather than being treated as success |

## Polling policy

`_poll_remail_otp()` (default interval `remail.otp_poll_interval`, 1 s):

1. 1 s settle delay before the first call, so a fast OTP mail is not missed by a
   wasted round-trip.
2. Trusted fast path: `verificationCode` is used only when it is exactly six
   digits, `recipient` equals the target mailbox, and `sender` matches the
   OpenAI sender patterns (`_trusted_structured_remail_code`).
3. Fallback: normalise → `_latest_remail_otp_candidate` with `keyword`,
   `issued_after_unix` and `excluded_otps` filtering (`seen_message_id` de-dup).
4. Graduated backoff 1 s (0–5 s) → 1.5 s (5–15 s) → 3 s (15 s+), then the floors
   above.
5. Optional preflight (`remail.preflight_enabled`, default **off** in code,
   **on** in `config.example.json`): authenticated `GET /v1/open/apikey/profile`
   (costs no credits) with a `/docs` fallback, so a dead key or an empty wallet is
   reported once instead of per mailbox.

## Verification

- `tests/registration/test_remail_mailbox.py` — order/batch/recovery parsing,
  idempotent retry, item error codes, pickup fast path, fetch-state gating,
  credential-failure disposition.
- Re-capture the upstream document and diff it against this file whenever a
  supplier change is suspected; a new `status`/`failureCode`/error `code` value
  must be added here **and** to the constants that classify it.
