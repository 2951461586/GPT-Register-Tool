# Protocol Payment Extractors

This directory vendors the protocol-only extractors used by
`sms_tool.payment_link_manager`:

- `pix/`: adapted from `F:\epsoft\pix` (callable PIX runner)
- `ideal/`, `kakao/`, `blik/`, `twint/`: adapted from
  `ideal-link-extractor-open-source-20260712`

Runtime tokens, proxy seeds, logs, dumps and state files must not be committed.
The unified manager passes tokens through environment variables and creates a
temporary proxy-seed file for each run.
