# Account Health Contract

> **This file is also the current contract for the promotion (优惠) check.**
> There is no separate `docs/current/promotion.md`, on purpose: the promotion
> probe shares this document's subject matter — the same account, the same
> liveness vocabulary, and a result that lands in the same grid row. The
> promotion-specific parts are the `plan` check in
> [Modes](#modes), the `优惠状态` column rules below, and the badge/stale rules
> under [Result semantics](#result-semantics).
>
> Do not go looking for the promotion contract in `docs/audits/`: those are
> frozen evidence snapshots, not how the module works today.

## Modes

`--one-click-scan` is probe-only by default. It loads the saved account and
calls the canonical AT liveness probe; it does not start OAuth, mailbox OTP, or
phone verification. Use `--scan-deep-probe` only when an operator explicitly
accepts those side effects.

`--quota-auto-relogin` is a separate recovery option for a 401 result. Recovery
may use refresh tokens, browser sessions, or email authentication according to
`--scan-relogin-mode` and is not part of a normal liveness probe. Recovery
egress routes through the dedicated `relogin` proxy lane (`relogin_pool` →
`liveness_pool` → health fallback) with round-robin spread across concurrent
workers, rotates exits strictly on pre-OTP transport/429/403/CSRF failures
(never re-sending an email OTP once requested), automatically falls back to
passwordless ChatGPT email-OTP login when legacy accounts with unknown passwords
or 2FA redirects fail password verification, and retries post-relogin liveness
verification across alternative exits on `status_code == 0` transport drops so a
newly issued AT is not discarded by a transient proxy glitch.

**Transport follows the registration method, but never to the point of refusing
to probe.** An account registered by a browser driver carries
`identity_context.browser_identity`, so its promotion check prefers a browser
session with the *same* driver and profile (rebuilt from the stored
`geo_country`); liveness uses the fast protocol probe first and opens that
browser only for a 401/403/status-0/Cloudflare answer. When the browser context
cannot be opened (no Playwright/Camoufox, a crashed child) the probe degrades to
the protocol transport with the same access token and saved fingerprint and the
result records `browser_fallback: unavailable`. It is deliberately not a
verdict: an unavailable browser is an environment condition, and reporting it as
检测失败 hid real account state behind a local install problem.

**Every probe reads the merged account payload.** `identity_context` (fingerprint
key, device id, proxy affinity, browser identity) has no column and lives only
inside `raw_json`, so `store.get_account_payload` — not a bare
`get_account_record` row — is what the probe consumes. The single-row
`--quota-usage` probe used the bare row and therefore ran with a default
fingerprint and no affinity, disagreeing with the batch scan about the same
account.

## Result semantics

- `probe_ok` / `health_state` describe the remote account observation.
- `persisted` describes whether the local marker was written.
- A persistence failure must not turn a healthy remote probe into an unhealthy
  account result.
- `browser_fallback` (`unavailable`) is diagnostic only: it says the probe had to
  leave the browser transport. It never changes `promotion_state`,
  `promotion_status` or the liveness verdict.
- The liveness batch summary counts `AT失效` (`at_invalid`) from the **initial**
  verdict, not the final one: an account whose relogin restored the token stays
  counted there. `正常 2/2，AT失效 2` is therefore a correct line (both healthy
  now, both entered the batch with a dead AT) — read `success` for health and
  `at_invalid` for "how many needed recovery".
- **A verdict is written back to the row, in both directions.** A probe that
  answers HTTP 200 retires the row's recorded `error` and turns a stored
  `at_invalid` back into `registered`; a definitive 401 (or the `token_invalid`
  label the probe produces) turns the row into `at_invalid`. One writer
  (`store.markers.mark_quota_status`) and one definition of “definitive”
  (`store.normalize._probe_is_token_invalid`): a transport failure
  (`status: unknown`, `status_code: 0`) is neither, so a blocked exit never
  marks an account dead, and a confirmed deactivation
  (`account_deactivated` / `token_revoked`) is never downgraded by a later probe.
  Both halves were missing until 2026-11-20 — the store only wrote
  `quota_status`, which no grid column reads — so a batch could print
  `正常 2，掉号 0，其他失败 0` while both rows showed `失败` (a stale
  `web_session_access_token_probe_failed:unknown` left by a dead exit), and a
  row whose AT had been revoked kept displaying `已注册`.
- **`at_probe_status_code` is derived, not stored.** The row payload ranks the
  probe records by timestamp (`desktop.read._access_token_probe_status_code`:
  `account_scan.token_probe` → `token_probe` → `scan.token_probe` →
  `quota.last_result`), so the AT badge always follows the newest observation.
  Nothing reads a stored copy, and `mark_quota_status` no longer writes one:
  the 2026-09-04 drift left five rows claiming 401 long after a scan had
  verified the same token with a 200. `scripts/repair_stale_probe_markers.py`
  (dry-run by default) retires such copies from `raw_json` and the session JSON
  and refuses any row whose derived verdict would change — the cleanup may not
  move a single badge.

Promotion checks (`--check-promotion`) inspect the account plan and trial offers
only — including the read-only Plus trial coupon probe
(`/backend-api/promo_campaign/check_coupon?coupon=<plus_campaign>&is_coupon_from_query_param=true`)
and the read-only **GPT Team 5-seat trial** probe
(`coupon=team-1-month-free&is_coupon_from_query_param=true`, sent with the
resolved personal account's `ChatGPT-Account-ID` from `accounts/check` where
`structure == "personal"`, strictly validating `response.coupon == "team-1-month-free"`).
Eligible accounts render `可试用Team(5席)` (combined with Plus trial or active
subscription labels when both apply), set `promotion_state = "trial_eligible"`,
and persist machine-readable `plus_trial_eligible` / `team_trial_eligible` /
`team_trial_state` flags into SQLite `raw_json` (as well as
`team_trial_ineligibility_reason` in probe results and batch summaries). They
never create Checkout and never enumerate payment methods; when an operator
explicitly requests a Team trial checkout URL, the separate `team_trial` method
(`sms_tool/pay_link/team_trial.py`, `--extract-payment-link --payment-method
team_trial`) creates a hosted `chatgptteamplan` checkout link directly in-process.
Normal liveness scans do not enter the payment path.

Ruled out by experiment, each measured on a fresh account: the promo campaign id
(explicitly emptied, payload verified clean), the `impersonate` target
(chrome124/chrome136), the ChatGPT client identity headers (`oai-device-id`,
`oai-language`, `oai-session-id`, `oai-client-version`,
`oai-client-build-number`, `sec-fetch-*`, `sec-ch-ua`, `Accept-Language`, an
`oai-did` cookie), the `x-openai-target-*` headers, `checkout_ui_mode: "hosted"`
with `price_interval`/`seat_quantity`/`cancel_url`, a single signed Sentinel
token, and the egress itself (residential Reliance Jio IN, residential Focus
Broadband US, and a datacenter US exit all answer the same 400). `direct_card` /
`paypal` / `upi` produce byte-identical `checkout_payload()` output, so the
carrier method never reaches the create request.

The single-token result above does **not** generalise to the token *pair*, and
neither does its converse. The reference project reverse-engineered the create
gate and the UPI lane independently reported it: the endpoint requires **both**
`openai-sentinel-token` and `openai-sentinel-so-token`, minted under flow
`chatgpt_checkout`.

🔴 **A controlled A/B on 2026-10-06 could not reproduce the "both" half.** On the
create endpoint, holding account, verified-BR exit and request body constant and
varying only the headers: no pair → `400 unusual activity`; **`openai-sentinel-token`
alone, no SO → `200`.** The mint could not even produce an SO for this flow —
`sms_tool/sentinel/client.py` only forwards one when the challenge response
carries an `so`, and `chatgpt_checkout` did not. So on this flow a lone main
token was sufficient, which is the opposite of "a lone main token is rejected".

The earlier observation that both tokens were required is **left open on
purpose**: that run varied identity, egress and payload together, so it cannot
show which of them the single token failed to compensate for. Two readings survive
and this document must not pick between them — the gate differs by flow, or it
changed between the two observations. Treat the main token as
sufficient-not-necessary and the SO as unproven either way.

`payment_wire._checkout_post` attaches the pair to every Checkout create through
the public `sentinel.checkout_sentinel_headers` authority (`payment_capability`,
`paypal_extract._create_checkout`, `wallet_transport` and `gcash_transport` all
reach the wire through it), and since 2026-10-06 the subprocess extractors receive
it too — see [protocol-registration.md](protocol-registration.md). The mint stays
advisory: a missing local runner degrades to no headers, so the existing
classification path is unchanged.

**A dead create gate was not the only thing hiding behind this.** Once the pair
was injected, the six extractors built on Stripe `payment_pages`
(blik / ideal / twint / pix / kakao / momo) still could not proceed: the create
endpoint no longer issues `cs_*` sessions at all, and Stripe answers an `oaics_*`
id with `404 resource_missing`. `direct_card`, which accepts `oaics_*` and never
touches `payment_pages`, now completes. That split is owned by
`payment/capability.py`; `checkout_kind` and `evidence_sources` identify which
evidence a result came from.

An independent read-only signal is read alongside a capability probe:
`GET /backend-api/payments/payment_methods` returns `one_click_trial_eligible`
and the offered method types. It is attached as `payment_methods_signal` and
**survives a refused create**, so a risk-blocked Checkout is not left with no
signal at all. It is additive evidence only — it never changes the
`eligible` / `ineligible` / `unknown` classification (or a Canary's stop
condition), because a trial-eligibility flag is not a method-availability
verdict.

When the create is `checkout_risk_blocked` (or a retryable transport failure),
`payment_method_capability_probe` can retry once through a pluggable
`fallback_transport`. `sms_tool/payment/capability_browser.py` implements a real-browser
create — it performs the `payments/checkout` POST inside the page context so the
request carries the browser's own TLS, cookies and Cloudflare clearance — and
delegates Stripe init / custom-checkout read / elements back to the protocol
transport. Build it with `browser_fallback_transport()`, which returns `None`
when Playwright is unavailable; the result's `fallback_used` records whether it
ran.

**Reachability (2026-11-20).** Until this date nothing passed that transport:
the seam was documented here and had zero production readers
(`docs/audits/scans/scan-2026-11-02-unused-module-inventory.md` measured
"0 production / 1 test"). `pay_link/core.py` now fills it from an explicit
opt-in switch — `PP_CAPABILITY_BROWSER_FALLBACK=1` — and the import is lazy, so
a default probe neither pays for Playwright nor loads the browser driver stack.
Default `off` means the network shape of a normal probe is unchanged; turning it
on is a behaviour change (one real browser session per risk-blocked create) and
belongs to whoever runs the canary.

The probe classifies only known, non-sensitive response signals: HTTP 400
`checkout_risk_blocked`, HTTP 429 `checkout_creation_rate_limited`, or a generic
HTTP failure with `http_status`. It never persists the raw response body or an
unrecognized upstream error code. A refused Checkout is unknown, not evidence
that the account has no payment methods.

On `payment_methods_label` and a 支付方式 column: **the account grid has no
支付方式 column** (its columns are 选中 / 创建时间 / 更新时间 / 邮箱 / 账号类型 /
注册区 / 类型 / 状态 / AT / RT / 2FA / 优惠状态 / 详情), and nothing in the
current tree produces `payment_methods_label`. Its only producer was the 09-08
code removed on 09-18. Do not reintroduce a reader for it.

## Promotion status contract

The 优惠状态 badge has three representations:

- `promotion_status` — the Chinese display label ("可试用Plus·-100%·×1month").
  Display copy only.
- `promotion_state` — the machine state (`trial_eligible`, `subscribed`,
  `free`, `auth_invalid`, `probe_failed`, `unknown`), defined in
  `sms_tool/payment/promotion_states.py` and pinned cross-language by
  `tests/fixtures/promotion_status_cases.json`. Filtering, sorting and
  branching must key off the state, never the label.
- `promotion_display` — the string the desktop renders. It is produced on the
  Python side (`desktop_read`, `promotion_batch`) and WPF reads it, falling
  back to the bare `promotion_status` when it is absent; no presentation layer
  re-derives it.

`promotion_marker_is_stale` in the same module is the single owner of the
"401 promotion marker cleared by a later verified AT-200" rule;
`desktop_read` applies it at display time and `account_recovery` at
persistence time. Both sides of the desktop read payload surface
`promotion_state` alongside `promotion_status`.

## Proxy precedence

Operation-specific callers must resolve proxies in this order:

1. Explicit command proxy
2. Registration affinity for accounts that have one
3. Operation pool (`liveness`, `promotion`, or browser health)
4. Configured fallback

The selected source should be retained in diagnostics as a non-sensitive label.

`proxy_routing.operation_proxy_candidates` is the single owner of this order.
Promotion retries consume that ordered list rather than rebuilding their own
pool order.

## Execution ownership

Foreground batches own worker lanes, deadlines and partial snapshots. The
durable queue owns claim, lease, heartbeat and restart recovery only; each
claimed job delegates its plan or liveness work to the same foreground
workflow. Health budgets are resolved by
`accounts.account_health.resolve_account_health_budgets`, with defaults of
300 seconds for relogin, 900 seconds per batch and 360 seconds per account.
CLI values are explicit overrides, not a second set of defaults.
