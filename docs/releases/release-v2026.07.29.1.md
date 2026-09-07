# v2026.07.29.1

## Desktop UI

- Fixed the account-list context menu layout with stable icon, spacing, text, and separator alignment.
- Consolidated mailbox, import, and network settings into clearer groups.
- Split basic network settings into registration proxy, ordered registration pool, mailbox receive proxy, and protocol payment proxy pool.
- Removed protocol proxy credentials from payment-dialog history; the dialog now uses the configured pool unless the operator enters an explicit override.

## Proxy routing

- Registration probes the ordered proxy pool, falls back between providers, and refreshes the dynamic Session for every worker.
- Mailbox polling remains isolated on `mailbox_proxy`, which defaults to local port 7897.
- Protocol payment extraction probes its own ordered pool, rotates provider country and Session credentials for the selected payment region, and falls back to the next provider when required.
- Registration-triggered and manual payment-link generation now use the protocol payment pool rather than inheriting the registration proxy.

## Reliability and security

- Added regression coverage for Kookeey, Cliproxy, and Zoorproxy dynamic Session formats and ordered fallback behavior.
- Added payment-pool coverage for MoMo VN routing and normalized `host:port:user:pass` proxy input.
- Kept real proxy credentials only in ignored local configuration; examples, docs, tests, logs, and release assets contain placeholders or redacted endpoints.

## Validation

- Full suite: `451 passed, 1 skipped, 7 subtests passed`.
- Live egress probes: all three registration providers produced JP exits; both protocol payment providers produced VN exits for MoMo after country and Session rotation.
- Desktop publish completed at `dist/net10/SmsWorkbench.exe`; UI automation confirmed fixed 232 x 38 context-menu rows with aligned full-width separators and the split network fields in Settings.
- Installer, portable ZIP, and SHA-256 manifest were rebuilt for this release.
