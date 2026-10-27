# Protocol Payment Architecture

> **Scope.** 现行契约。设计提案与改动前的 recon 已归档到
> [`docs/audits/plans/plan-2026-10-06-protocol-payment-enhancement.md`](../audits/plans/plan-2026-10-06-protocol-payment-enhancement.md)。
> 本文只描述当前代码实际如何路由、执行与归一提链结果。

## 1. Shared Flow Executor And Routing

Protocol-payment extraction now uses three common modules:

- `payment/flow.py` defines the canonical stage vocabulary and method profiles.
- `payment/routing.py` compiles named pools and stage routes into one immutable, redacted `PaymentRoutePlan`.
- `payment/executor.py` owns execution states, cancellation/unknown outcomes, normalization, and progress history.

CLI and batch paths compile the route plan before JIT authentication and pass
the same object through probes, retries, adapters, and transports. Checkout and
Approve legacy pools are still accepted, but selection and country-session
rotation are centralized in the planner.

## 2. Extractor Consolidation (services/protocol-payment)

The seven subprocess extractors were grown by copy-paste; two ratchets now bound
that duplication and a shared layer absorbs the parts proven provider-neutral:

- `scripts/extractor_parity_report.py` freezes, per file pair, the number of
  top-level functions that stay AST-identical after provider tokens are erased.
  It may only go down.
- `scripts/provider_decoupling_ratchet.py` freezes, per provider file, how many
  same-named definitions delegate to `common/` versus diverge from it.
- `common/extractor_runtime.py` owns the provider-neutral runtime helpers:
  `normalize_pre_proxy_url`, `proxy_chain_key` and the country-selector regex.
  iDEAL/TWINT/BLIK import them instead of carrying a copy.
- `common/stripe_setup.py` (batch 7, 2026-10-20) owns the Stripe wire setup that
  iDEAL/TWINT carried byte-identically and BLIK copied: `stripe_init`,
  `stripe_elements_session_params`, `payment_accept_language`,
  `saved_payment_value` and `random_runtime_version`. Every provider-specific
  piece is a parameter — the env prefix, the locale callables, the session
  factory and the `dump_http` wrapper — so each extractor keeps a same-named
  thin wrapper and its own patch targets.
- `common/checkout_session.py` (batch 9, 2026-10-28) owns `create_checkout` and
  `warmup_approve_context`. Its seams are the two country/currency callables
  (blik resolves unknown countries through its own `COUNTRY_CURRENCY` +
  `default_payment_country()`, iDEAL/TWINT go through `provider_profile`), the
  module's `DEFAULT_STRIPE_PK`, and the **env-var names** of the coupon-fallback
  and warm-up-dump switches (`IDEAL_*` for ideal *and* blik, `TWINT_*` for
  twint). The hooks are rebuilt per call rather than cached in a module
  constant, so a rebind of `log` / `dump_http` / the redaction registry still
  takes effect exactly as it did before the move.

<!-- ratchet-numbers:begin payment -->
| Extractor pair | AST-identical functions |
| --- | ---: |
| `ideal:blik` | 12 |
| `ideal:twint` | 25 |
| `momo:kakao` (negative control) | 0 |
| `twint:blik` | 9 |

Same-named definitions against `common/`: **237 delegating**, **0 verbatim copies**, **34 deliberate forks**.
<!-- ratchet-numbers:end payment -->

What deliberately stays per-extractor: anything whose *values* are provider
state rather than a parameter. Batch 7 made four of those visible as forks and
they stay local: `stripe_init` in BLIK (returns `client_elements_session_id` and
reads `enable_save` from `saved_payment_value()`), in Kakao (different signature
and ko-KR payload) and in MoMo (delegates to `paylink.stripe_init_payment_page`),
plus `stripe_elements_session_params` in BLIK (requires every ctx key). Rule 19 in
`docs/architecture.md` records the same-name forks, including kakao's
`proxy_chain_key`, which strips a trailing country code from the sid and must
not be converged. Batch 9 recorded two more of the same kind: `create_checkout`
in **kakao** (`(session, token)` → a three-value return, KRW / ko-KR) and in
**momo** (an eight-parameter proxy-rotating variant returning a six-tuple) share
the name but not the function, so they stay deliberate forks. That is the same
call batch 7 made for `stripe_init`, not a migration backlog item. A full parameterized engine is still gated on the differential
replay required by
`docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

## 4. 支付资格查询（read-only）

`payment/qualification.py` answers “can this account reach a zero-due Checkout
for a method its **exit country** supports?”. It creates a Checkout and calls
Stripe init, then stops; it never creates a payment method, confirms, or
approves. The batch wrapper is `payment/qualification_batch.py`; persistence is
`store/markers.mark_payment_qualification_status` and the desktop reads
`payment_qualification_display` / `_state`.

Load-bearing rules:

* **Country comes from a measurement, not a hint.** `_measured_country` calls
  the shared resolver with `hint=""` and `need_timezone=True`; a hint-only
  answer is rejected. The server compares `billing_details.country` with the
  request's egress, so trusting `region-XX` is exactly the
  `Billing country must match request country` failure this probe exists to
  prevent.
* **One Checkout per probed *contract*, not per method.**
  `CheckoutRequestContract.checkout_payload()` carries only
  `billing_details.country/currency`, plan, entry point and the promo campaign —
  **not** the payment method — so a per-method loop bought the same answer twice
  (measured 2026-10-27: `grabpay` and `direct_card` from a PH exit send byte-identical
  creates). `_contract_key` groups candidates by (probe transport, country, currency);
  the first probe's published-method inventory answers the rest, and those rows are
  marked `evidence_source=shared_checkout` + `derived_from`. Only the *availability*
  differs between them — the currency/zero-due verdict is contract-level, which is
  exactly the split `build_capability_probe_result` already makes. A method that is
  not in the published list is a conclusive `payment_method_unavailable`, not a
  failure. PayPal and GCash own separate probes, so they never share a checkout;
  evidence without a method list (a failure, an unnamed `oaics` channel id) is
  never used to derive. `payment_qualification.probe_all_methods` (CLI
  `--payment-qualification-all-methods`) is the diagnostic grid: every method pays
  for its own create. The default still stops at the first eligible *probe*; the
  siblings of that same contract are reported from the one answer it already got.
* **Unknown method ids are rows, not exceptions.** `_select_candidates` emits
  an `unknown_method` row and the probe continues.
* **Country-agnostic methods use the exit currency, and refuse an unknown one.**
  `direct_card` / `paypal` take `payment.wire.CURRENCY_MAP[country]`. When the exit
  country is not in that map there is no valid billing currency, so the method is
  skipped as `currency_unknown` (badge 币种未知, state `unknown`) instead of being
  billed in the catalogue default (PH/PHP) and paying for a guaranteed rejection
  reported as 检测失败.
* **Per-method errors are proxy-redacted before persistence.**
  `_safe_error` masks `http(s)` and `socks5` URL credentials; `_run_method`
  scrubs the exact candidate proxy as a second gate.
* **Egress attribution is retained.** Batch rows carry
  `proxy_fingerprint` / `proxy_host` / `proxy_attempts` (credential-free) for
  “which exit produced this verdict” diagnosis.
* **The batch breaker uses the shared vocabulary.** `_env_failure_class`
  returns `network` for transport/risk blocks and `account` for an invalid AT;
  `BatchCircuitBreaker` only trips on `network` / `auth_state`.
* **Stripe is a separate egress gate, and no longer a hard one.**
  `_stripe_preflight` probes `https://api.stripe.com/` through the candidate
  pool before any account. A proxy can answer `chatgpt.com` with 200 and still
  deny Stripe at the tunnel (measured 2026-10-27: all nine proxylane exits
  returned `503 Target host denied`). A failed preflight now **warns and keeps
  going** (`batch_warning` + `summary.warnings` + first denial host/status),
  because the `oaics_` custom checkout answers without Stripe — measured the
  same day, one account returned `可用·PayPal` that way through the very pool
  that denied Stripe. `stripe_preflight: "strict"` restores the hard abort
  (`abort_reason=stripe_unreachable`), `false` skips the probe, and a selection
  whose previous verdicts were all `oaics_` skips it automatically
  (`reason=oaics_only`).
* **A dedicated pool is tried first.**
  `payment_qualification.proxy_pool` (or `proxy.lanes.payment_qualification`)
  is passed as *explicit* egress input, so it outranks the registration proxy
  that the desktop always also passes; the desktop pool stays as a fallback.
  When empty, the promotion lane is used unchanged. This exists because the
  registration pool is a ChatGPT edge, not a Stripe edge.
* **Rotation moves exactly one step per attempt.** The batch resolves the whole
  egress order once (`_routing_inputs`: dedicated pool, then the desktop route)
  and hands one candidate per attempt with `pinned_proxy=True`, which puts that
  candidate at the head instead of letting the dedicated pool demote it. Without
  the flag the single-account probe's dedicated-pool promotion (the rule above)
  made every rotation re-dial `dedicated[0]`: `max_proxy_attempts` was a no-op
  and `proxy_host` named an exit that was never used. The single-account/CLI path
  keeps the promotion, because there the caller hands the *desktop* proxy.
* **An exit refusal has its own state.** `egress_denied` (markers: `Target host
  denied` / `Tunnel connection failed` / `403 forbidden ip` / `407`) sits between
  `auth_invalid` and the generic `probe_failed`, both in `qualification_state`
  and in `_aggregate_state`. The badge reads 出口被拒, `_summary` and the
  completion line carry an `出口被拒 N` counter (so the counters add up to
  `total`), the desktop result dialog counts it separately, and the batch
  breaker treats it as `network`. Before this it was indistinguishable from a
  dead token or an unavailable method.
* **A proven-dead pool is not re-dialled.** `_retryable_transport` treats
  `egress_denied` as exit-level (another exit may differ in a mixed pool), but
  `_probe_with_rotation` stops after the first denial when the batch preflight
  already found 0/N Stripe-reachable candidates. Measured 2026-10-27: two
  accounts burnt 3 attempts each (54 s + 43 s) against a pool the preflight had
  just proven dead. A risk refusal (`400 unusual activity`) keeps rotating: it
  happens on `chatgpt.com` and *is* exit-dependent. The rule "an HTTP answer
  proves the exit worked, so do not rotate" reads `status_code`, which
  `check_payment_qualification` now carries from the first non-eligible row
  (`_method_row` keeps `http_status` / `retryable`) — before that the field was
  dropped and the rule could never see a status.
* **The egress preflight probes the head first.** `_stripe_preflight` dials
  `candidates[0]` alone; only a denial widens to the rest in parallel, so a
  healthy pool costs **one** request instead of nine. The `chatgpt.com` preflight
  samples `PREFLIGHT_SAMPLE` and reports `sampled` / `pool` separately, so the
  abort line says "抽样的 3 条（池内共 9 条）" instead of implying all nine died.
* **The account badge keeps the failure's flavour.** `_aggregate_label` consults
  `is_risk_blocked()` for `probe_failed`, so a risk refusal renders 风控拦截
  instead of the generic 检测失败 (measured: bo***@icloud.com, 2026-10-27 17:45).
* **The preflight verdict is cached per candidate set.** `_stripe_preflight`
  keys a 5-minute (configurable, `0` disables) cache on a credential-free hash of
  the sorted exits + their source, so an operator retry does not re-probe a pool
  that already answered. Changing the pool invalidates the key.
* **The account budget is derived, not configured.** `--refresh-timeout` stays
  the per-request HTTP timeout; each attempt is additionally capped by the time
  left of `timeout × max_proxy_attempts`, and an exhausted budget returns
  `error_code=account_budget_exhausted` instead of starting another request.
* **`max_proxy_attempts` is configurable.** Default 3, clamped 1..32; a
  proxy-level failure may need the full pool, but raising it only helps when
  the pool actually contains reachable exits.
* **A transient failure preserves the last conclusive verdict.**
  `payment_qualification.last_conclusive` is written by the store marker; the
  desktop renders `检测失败（上次：可用·PayPal）` instead of erasing a good
  badge.
* **The badge names the checkout kind.** `checkout_kind` / `probe_checkout_kind`
  is retained on every method row: `可用·PayPal（自定义）` for an `oaics_`
  custom checkout and `可用·PayPal（Stripe）` for a `cs_*` path that passed
  Stripe init. A custom-checkout success is not evidence that the Stripe path
  works through the same exit.
* **PayPal failures keep their real stage.** `_paypal_capability_probe` wraps
  checkout-create and Stripe-init exceptions in `CapabilityProbeError`, so the
  row reports `checkout_create` / `checkout_transport_failed` or
  `stripe_init` / `stripe_init_transport_failed` instead of a generic
  `capability_probe`.

There is deliberately no separate durable job queue. Each account's verdict is
persisted the moment it finishes (`mark_payment_qualification_status`), so a
restart loses only the in-flight accounts; rerunning with the same email list is
the resume path. This keeps the desktop task model (one IPC run, per-account
progress events) without a second source of truth.
