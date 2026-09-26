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
> frozen evidence snapshots. The most recent one that discusses promotion
> (`scan-2026-09-21-payment-eligibility-in-promotion-column.md`) records how the
> eligibility suffix was added, not how the module works today.

## Modes

`--one-click-scan` is probe-only by default. It loads the saved account and
calls the canonical AT liveness probe; it does not start OAuth, mailbox OTP, or
phone verification. Use `--scan-deep-probe` only when an operator explicitly
accepts those side effects.

`--quota-auto-relogin` is a separate recovery option for a 401 result. Recovery
may use refresh tokens, browser sessions, or email authentication according to
`--scan-relogin-mode` and is not part of a normal liveness probe.

## Result semantics

- `probe_ok` / `health_state` describe the remote account observation.
- `persisted` describes whether the local marker was written.
- A persistence failure must not turn a healthy remote probe into an unhealthy
  account result.

Promotion checks (`--check-promotion`) inspect the account plan and trial offer
only by default. They never create Checkout unless the operator explicitly
adds `--payment-eligibility`. The separate `--check-payment-eligibility`
command probes selected saved accounts without changing promotion markers.
The desktop “查支付资格” entry is separate and starts directly for the selected
or currently filtered accounts, without a warning dialog. It creates a
disposable Checkout session and may be refused or rate-limited by the
platform, but stops before payment-method creation, confirmation, approval or
charge. Normal liveness scans do not enter this path.

The independent probe appends a compact explicit method list to the 优惠状态
column (for example `可试用Plus · card/upi/momo`). This is Checkout evidence
for the billing country, not a guarantee that a purchase can complete.

The suffix has three states and they are deliberately distinguishable
(`promotion_states.payment_eligibility_label`):

| State | Suffix | Meaning |
|---|---|---|
| never probed | *(empty)* | no `payment_capability` record; `safe_snapshot()` writes `{}` for these |
| probed, rails found | `card/upi/momo` | Checkout's explicit order, capped at 8 tokens with `+N` |
| probed, inconclusive | `支付资格未知` | the probe failed or had no explicit method evidence |

The marker exists because a blank suffix is indistinguishable from "never
probed". Never render the empty mapping as `支付资格未知`: that shape is what every
untouched account carries. Never display stale methods from a failed result.

**The rail list can only come from a Checkout session.** `GET accounts/check` —
the endpoint the promotion check itself uses — returns no payment field at all
(measured: a 9.7 KB body whose only payment-shaped keys are
`entitlement.billing_currency` and `entitlement.billing_period`, both `null`).
Any claim that the promotion endpoint already carries `payment_method_types` is
wrong.

**Historical evidence, not a current diagnosis:** `POST
/backend-api/payments/checkout` answers `HTTP 400 {"detail":"Our systems have
detected unusual activity. Please try again later."}` — a risk-engine block, not
a validation error. This is not specific to the eligibility probe:
`PPLinkExtractor._create_checkout` builds the identical payload and calls the
identical `_checkout_post`, so the PayPal link lane is subject to the same
refusal. A previous implementation of this feature existed from 09-08 to 09-18
(`account_promotion.probe_trial_payment_methods`, removed in `20ab44e`) and left
`payment_methods: []` for **all 28** accounts it ran against (VN/JP/US,
09-07 → 09-17), 25 of them recording this exact 400. Those measurements do not
establish the cause of today's unknown results. A failed Checkout is unknown,
not evidence that the account has no payment methods.

Ruled out by experiment, each measured on a fresh account: the promo campaign id
(explicitly emptied, payload verified clean), the `impersonate` target
(chrome124/chrome136), the ChatGPT client identity headers (`oai-device-id`,
`oai-language`, `oai-session-id`, `oai-client-version`,
`oai-client-build-number`, `sec-fetch-*`, `sec-ch-ua`, `Accept-Language`, an
`oai-did` cookie), the `x-openai-target-*` headers, `checkout_ui_mode: "hosted"`
with `price_interval`/`seat_quantity`/`cancel_url`, a signed Sentinel token, and
the egress itself (residential Reliance Jio IN, residential Focus Broadband US,
and a datacenter US exit all answer the same 400). `direct_card` / `paypal` /
`upi` produce byte-identical `checkout_payload()` output, so the carrier method
never reaches the create request.

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
  `sms_tool/promotion_states.py` and pinned cross-language by
  `tests/fixtures/promotion_status_cases.json`. Filtering, sorting and
  branching must key off the state, never the label.
- `promotion_display` — the composed string the desktop actually renders:
  `promotion_status` plus the optional eligibility suffix, joined by `" · "`.
  It is composed once on the Python side and WPF reads it, falling back to the
  bare `promotion_status` when it is absent; no presentation layer re-derives
  the rule. The single owner is
  `promotion_states.promotion_status_with_eligibility`, deliberately kept in
  that dependency-free module so the read path never imports a provider module.
  A row with no suffix must not gain a dangling separator. The suffix may be the
  literal `支付资格未知`; it is an opaque string to every consumer, so WPF must
  never parse it.

`promotion_marker_is_stale` in the same module is the single owner of the
"401 promotion marker cleared by a later verified AT-200" rule;
`desktop_read` applies it at display time and `account_recovery` at
persistence time. Both sides of the desktop read payload surface
`promotion_state` alongside `promotion_status`. The stale rule also clears the
eligibility suffix, so an invalidated marker never leaves an orphan token list
behind.

### Payment eligibility suffix

`sms_tool/accounts/account_payment_eligibility.py` owns the suffix. It is a thin
wrapper over `payment_capability.payment_method_capability_probe` so the
promotion probe (`account_promotion.py`) and its multi-account runner
(`accounts/promotion_batch.py`) never import the five provider modules directly. The
handshake creates Checkout. `cs_*` sessions read Stripe init and optionally
Elements specs; `oaics_*` sessions read explicit custom Checkout fields. An
opaque `cpmt_*` ID does not imply `card`. `checkout_kind` and
`evidence_sources` identify the evidence used; failed or missing evidence
stays unknown. The safe `pay_link.probe_payment_method` path uses the same
capability implementation, while link extraction may continue into payment
side effects. Callers reach the probe through module-qualified
access (`from .. import payment_capability`), because a `from X import f`
binding makes `patch("sms_tool.payment_capability.f")` silently ineffective —
the project patches source modules.

Billing context is set explicitly from the account's recorded registration
country (`billing_country`, `billing_currency`, `locale`). `payment_method_types`
varies with billing country. The payment egress gate verifies the proxy's
observed exit country before Checkout; a missing, unverified or mismatched exit
returns a failed observation with the `支付资格未知` badge, not a method list.
Before that check, known proxy credential templates (`region-XX`, `geo-XX`,
`country-XX`, IPWO custom zones and supported password-based countries) are
retargeted to the account's billing country without rotating the sticky
session. Unknown formats are not rewritten. The rewritten country is a request
to the provider, not proof of its actual exit; the observed-exit gate still
rejects a mismatched or unreachable proxy before Checkout. No full proxy URL
is persisted in the capability result.
Checkout creation and the `oaics_*` session read use the same supported
Chrome 124 browser profile, saved device ID, account Cookie and verified
checkout proxy for one probe. A conflicting `oai-did` Cookie and saved device
ID stop the probe before Checkout with `checkout_identity_mismatch`; no
replacement device ID or proxy session is generated during the probe.
When bundled explicitly with a promotion check, a failed plan probe does not
create Checkout, and a 401 clears eligibility because its token is invalid.
The standalone command only writes `payment_capability`, never
`promotion_status`, `promotion_state` or `promotion_updated_at`. It requires
an explicit account selection and runs serially without automatic retries.

The sanitized observation is persisted under the `payment_capability` key of
`raw_json`. `account_models.AccountSessionModel.safe_snapshot()` is the only
key-level gate for that payload: a key absent from that whitelist is silently
dropped the next time `upsert_account` rebuilds the row. The write side is
three-state — `None` leaves the stored value untouched, `{}` clears it because
the access token died, and a dict replaces it. Credential-shaped keys are
stripped before storage. The batch summary groups only known `error_stage`,
`error_code`, HTTP status and billing country, not raw upstream errors or
proxy URLs.

Checkout creation can be rate limited (`429
checkout_creation_rate_limited`). The probe classifies this as retryable but
does not immediately retry within an account attempt.

Scan results report remote `ok` independently of local `persistence`
(`session_saved`, `account_saved`, `persisted`, and a generic `error_code`).
The batch counts `persist_failed` separately from remote `failed`; a partial
save makes the batch `ok` false and the desktop command exit nonzero.
Promotion batch `success` and per-account `ok` still describe the remote plan
probe; its `account_completed`/`batch_completed` events report failure when
the corresponding local save did not complete. The desktop promotion summary
shows `保存失败` separately from the plan result.

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
