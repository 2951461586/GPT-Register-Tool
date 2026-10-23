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
