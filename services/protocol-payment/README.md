# Protocol Payment Extractors

This directory vendors the protocol-only extractors used by
`sms_tool.payment_link_manager`:

- `pix/`: adapted from `F:\epsoft\pix` (callable PIX runner)
- `ideal/`, `kakao/`, `blik/`, `twint/`: adapted from
  `ideal-link-extractor-open-source-20260712`
- `direct_card/`: 直卡 checkout short-link extractor adapted from
  `F:\epsoft\link\直卡提链by Simon.py`. Builds a
  `chatgpt.com/checkout/<entity>/<cs_id>` custom-checkout link via a US checkout /
  TR promo-update / zero-amount-verify flow. Driven through its own CLI
  (`--credential-file`, `--checkout-proxy`, `--update-proxy`).
- `momo/`: MoMo scannable-QR extractor adapted from
  `F:\epsoft\link\momo-qr-extract-share-20260728`. `ac_paylink_core.py` +
  `momo_qr_extract.py` run the VN checkout → Stripe init → force ₫0 → MoMo PM →
  confirm → ChatGPT approve → follow redirect → `payment.momo.vn` QR flow;
  `run_momo.py` is the thin runner the manager drives (single normalized JSON,
  decodes the `data:image` QR to a PNG under `--qr-out-dir`).

Runtime tokens, proxy seeds, logs, dumps and state files must not be committed.
The unified manager passes tokens through environment variables and creates a
temporary proxy-seed file for each run.
