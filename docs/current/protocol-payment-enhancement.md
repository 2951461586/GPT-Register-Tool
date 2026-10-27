# Protocol Payment Architecture

> **Scope.** 现行契约。设计提案与改动前的 recon 已归档到
> [`docs/audits/plans/plan-2026-10-06-protocol-payment-enhancement.md`](../../audits/plans/plan-2026-10-06-protocol-payment-enhancement.md)。
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

<!-- ratchet-numbers:begin payment -->
| Extractor pair | AST-identical functions |
| --- | ---: |
| `ideal:blik` | 13 |
| `ideal:twint` | 26 |
| `momo:kakao` (negative control) | 0 |
| `twint:blik` | 10 |

Same-named definitions against `common/`: **231 delegating**, **0 verbatim copies**, **32 deliberate forks**.
<!-- ratchet-numbers:end payment -->

What deliberately stays per-extractor: anything whose *values* are provider
state rather than a parameter. Batch 7 made four of those visible as forks and
they stay local: `stripe_init` in BLIK (returns `client_elements_session_id` and
reads `enable_save` from `saved_payment_value()`), in Kakao (different signature
and ko-KR payload) and in MoMo (delegates to `paylink.stripe_init_payment_page`),
plus `stripe_elements_session_params` in BLIK (requires every ctx key). Rule 19 in
`docs/architecture.md` records the same-name forks, including kakao's
`proxy_chain_key`, which strips a trailing country code from the sid and must
not be converged. A full parameterized engine is still gated on the differential
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
* **One Checkout per probed method, stopped at the first eligible one.**
  `payment_qualification.probe_all_methods` (CLI
  `--payment-qualification-all-methods`) restores the full diagnostic grid.
  Every candidate is a separate create, so the default is bounded.
* **Unknown method ids are rows, not exceptions.** `_select_candidates` emits
  an `unknown_method` row and the probe continues.
* **Country-agnostic methods use the exit currency.** `direct_card` / `paypal`
  take `payment.wire.CURRENCY_MAP[country]` (falling back to the catalogue
  currency only when the country is unmapped).
* **Per-method errors are proxy-redacted before persistence.**
  `_safe_error` masks `http(s)` and `socks5` URL credentials; `_run_method`
  scrubs the exact candidate proxy as a second gate.
* **Egress attribution is retained.** Batch rows carry
  `proxy_fingerprint` / `proxy_host` / `proxy_attempts` (credential-free) for
  “which exit produced this verdict” diagnosis.
* **The batch breaker uses the shared vocabulary.** `_env_failure_class`
  returns `network` for transport/risk blocks and `account` for an invalid AT;
  `BatchCircuitBreaker` only trips on `network` / `auth_state`.
* **Stripe is a separate egress gate.** `_stripe_preflight` probes
  `https://api.stripe.com/` through the candidate pool before any account. A
  proxy can answer `chatgpt.com` with 200 and still deny Stripe at the tunnel
  (measured 2026-10-27: all nine proxylane exits returned `503 Target host
  denied`). No Stripe-reachable candidate aborts the batch with
  `stripe_unreachable` instead of spending Checkout creates.
  `payment_qualification.stripe_preflight: false` is diagnostic-only.
* **A dedicated pool wins over the registration pool.**
  `payment_qualification.proxy_pool` (or `proxy.lanes.payment_qualification`)
  is used for qualification; when empty it falls back to the promotion lane.
  This exists because the registration pool is a ChatGPT edge, not a Stripe
  edge.
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
