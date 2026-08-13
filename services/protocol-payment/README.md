# Protocol Payment Extractors

This directory vendors the protocol-only extractors used by
`sms_tool.payment_link_manager`:

- `pix/`: adapted from `F:\epsoft\pix` (callable PIX runner)
- `ideal/`, `kakao/`, `blik/`, `twint/`: adapted from
  `ideal-link-extractor-open-source-20260712`
- `direct_card/`: vendored direct-card checkout short-link extractor. Builds a
  `chatgpt.com/checkout/<entity>/<cs_id>` custom-checkout link via a US checkout /
  TR promo-update / zero-amount-verify flow. Driven through its own CLI
  (`--credential-file`, `--checkout-proxy`, `--update-proxy`).
- `momo/`: vendored MoMo scannable-QR extractor. `ac_paylink_core.py` +
  `momo_qr_extract.py` run the VN checkout → Stripe init → force ₫0 → MoMo PM →
  confirm → ChatGPT approve → follow redirect → `payment.momo.vn` QR flow;
  `run_momo.py` is the thin runner the manager drives (single normalized JSON,
  decodes the `data:image` QR to a PNG under `--qr-out-dir`).

GoPay, GCash, and GrabPay are not vendored subprocess extractors. GoPay and
GrabPay share `sms_tool/wallet_provider.py` and `sms_tool/wallet_transport.py`;
GCash owns the separate `sms_tool/gcash_provider.py` and
`sms_tool/gcash_transport.py` custom-payment-method path. The shared ChatGPT
Checkout/Stripe init request contract lives in `sms_tool/checkout_contract.py`;
vendored and native adapters should reuse that contract instead of introducing
another payload shape.

Runtime tokens, proxy seeds, logs, dumps and state files must not be committed.
The unified manager passes tokens through environment variables and creates a
temporary proxy-seed file for each run.

For batch validation, first probe account access tokens and only pass non-401
accounts to the MoMo runner. Treat `ready_with_qr` plus a URL/QR artifact as
success; authenticated accounts may still return `account_trial_ineligible`,
`card_only_full_price`, or `approve_result_blocked`. Generated reports and QR
files are runtime artifacts and must remain ignored.

The maintained batch entrypoint is now:

```powershell
python -m sms_tool --extract-payment-link --payment-method momo --email-file runtime/canary.txt --payment-canary 5 --workers 2
```

For a resumable production cohort, add a stable ID and bounded retries:

```powershell
python -m sms_tool --extract-payment-link --payment-method momo --email-file runtime/eligible.txt --payment-batch-id momo_vn_20260731 --workers 2 --payment-retries 1
```

Use `--payment-probe-only` for a side-effect-limited capability pass. After JIT
authentication and registration-country-matrix validation it creates one
ChatGPT Checkout and calls Stripe init, then stops before payment-method
creation, confirm, approve, polling, or provider redirect. The result preserves
the discovered amount, currency, and payment-method catalog and classifies the
method as `eligible`, `ineligible`, or `unknown`. A conclusive unavailable method
or non-zero offer does not pause a Canary; a systemic `unknown` capability result
does. Reusing the same `--payment-batch-id` resumes the atomic checkpoint only
when the hashed execution mode, matrix, proxy, retry, and JIT settings still
match. Reports never include access tokens or authenticated proxy URLs.

The manager exposes three wallet methods through two adapter contracts:

- GoPay: shared-wallet adapter, ID Checkout, TH Promotion/Update, IDR, and
  provider hosts owned by GoPay/Gojek/Midtrans.
- GrabPay: shared-wallet adapter, PH checkout, PHP, and provider hosts owned by
  Grab/GrabPay.
- GCash: custom-payment-method adapter, PH checkout, PHP, and provider hosts
  owned by GCash/Mynt.

The GoPay/GrabPay flow is Checkout → optional GoPay Promotion/Update → Stripe
init → wallet PM → confirm → ChatGPT approve → poll → validated provider
redirect. GCash has an independent custom-method sequence and tests. Capability
probes reuse each method's real pre-side-effect preparation: the GoPay probe
runs Checkout → Promotion/Update → Stripe init, while GrabPay stops after
Checkout → Stripe init. Both stop before wallet-PM creation or authorization
and are covered by fixtures under `tests/fixtures/wallet_provider/`. Start a
new profile with a one-account capability Canary before enabling a full cohort:

```powershell
python -m sms_tool --extract-payment-link --payment-method gopay --email-file runtime/canary.txt --payment-probe-only --payment-canary 1 --workers 1
```

### Unified Result Contract

The manager has five terminal states: `completed`, `failed`, `cancelled`,
`unknown`, and `timed_out`. Every normalized result includes `retryable` and
`error_stage`. `cancelled` is not retryable. `unknown` also sets
`requires_reconciliation=true` and is never automatically retried because a
side-effecting request may already have reached the provider. `timed_out` is
retryable unless an adapter has stronger outcome evidence. A successful link or
QR extraction means an artifact is ready; it does not assert that the customer
completed the remote wallet payment.

Each worker runs the JIT AT gate immediately before checkout. HTTP 401 enters the
shared recovery order: OAuth refresh token, existing ChatGPT session cookie,
protocol email-OTP login (curl_cffi), then Codex OAuth PKCE. Browser-based
re-login has been removed; recovery is protocol-only. A replacement AT is
persisted only after a second HTTP 200 probe. `account_deactivated` is permanent
and is not retried.
The desktop batch protocol-payment flow exposes two payment-owned pools:
`checkout_proxy_pool` (Checkout/JIT, normally the billing country) and
`approve_proxy_pool` (the discount/Approve country, JP or TR by default).
The pool owner, rather than a per-stage setting, selects the route: Checkout
owns Stripe/provider/confirm/redirect, while Approve also supplies the GoPay
promotion/update request. GoPay keeps its internal TH promotion default and
ID billing/provider defaults; the matrix may select JP or TR for final
Approve. MoMo may accept separate checkout, promotion, provider, approve, and
redirect proxies. Kakao
prints a final structured JSON object for both success and conclusive failures;
the manager no longer infers Kakao state from free-form log URLs.

PayPal merchant-return reconciliation is deliberately separate from extraction.
`sms_tool/paypal_reconciliation.py` validates and follows only the allowlisted
`pm-redirects.stripe.com → pay.openai.com → chatgpt.com/checkout/verify`
chain through a caller-supplied transport, and returns secret-free
`conclusive`/`unknown`/`failed` evidence. It does not change
`generate_payment_link()` or turn a return-chain result into a newly extracted
link.
