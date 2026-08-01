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

Use `--payment-probe-only` for a no-payment JIT-authentication and
registration-country-matrix pass; it never invokes a payment adapter. Reusing
the same `--payment-batch-id` resumes the atomic checkpoint only when the hashed
execution mode, matrix, proxy, retry, and JIT settings still match. Reports never
include access tokens or authenticated proxy URLs.

Each worker runs the JIT AT gate immediately before checkout. HTTP 401 goes
directly to mailbox OTP OAuth, and a replacement AT is persisted only after a
second HTTP 200 probe. `account_deactivated` is permanent and is not retried.
MoMo accepts
separate checkout, promotion, provider, approve, and redirect proxies. Kakao
prints a final structured JSON object for both success and conclusive failures;
the manager no longer infers Kakao state from free-form log URLs.
