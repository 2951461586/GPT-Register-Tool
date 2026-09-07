# v2026.07.31.2

## Desktop settings expansion

- Expanded the desktop settings UI to expose the backend capabilities added in v2026.07.31.1: AT-stability入库 gating (probe count/delay/timeout), stage concurrency (network / AT-probe / payment gates), Sentinel (version, max concurrency, prewarm window, circuit breaker), and formal batch payment (per-method workers, canary pause, region-eligibility matrix).
- ReMail mailbox-pool and long-term (`purchase`) mailbox enhancements on the desktop registration and pool surfaces.
- Added desktop ReMail registration tests and expanded ReMail mailbox tests.

## Kakao protocol payment: sticky-session proxy support

- `proxy_for_country` now appends a per-region suffix to the sticky `sid` when deriving the three-region chain. Sticky-session proxies (e.g. cliproxy) key the exit IP off the `sid`, so without this every region was pinned to the first exit (KR checkout would drag VN promotion onto the KR IP). Each region now gets an independent exit: KR checkout / VN promotion / KR provider.
- `proxy_chain_key` strips only the region suffix (the 2-letter country code) and keeps the base `sid`. This keeps the three-region derivations of one seed on the same sticky chain, while multiple redundant seeds that differ only by base `sid` are no longer collapsed by `load_proxy_seeds` dedup.
- `payment_link_manager` lets the Kakao extractor use its own multi-seed pool file (`proxy_seeds.txt`) for redundancy and failure rotation — one seed's TLS/exit hiccup rotates to the next instead of failing the whole batch.

## Tooling

- Added `scripts/probe_account_liveness.py`: a lightweight account-liveness probe that reuses the canonical `/backend-api/wham/usage` contract per account without doing an email-OTP relogin, and reports the 2xx / 401 / inconclusive ratio.

## Chores

- Removed the vendored `ppgateway.exe` binary.
- CLI, `config.example.json`, installer script, and doc map updates.

## Validation

- `python -m pytest -q` passes (546 passed, 1 skipped, 7 subtests).
- Kakao sticky-proxy fix verified live: three-region exit preflight (KR/VN/KR) all pass, 5-seed pool rotates on TLS hiccups, and the full checkout → Stripe init (kakao_pay detected) → promotion chain runs. Remaining blocker for actual QR issuance is account-region binding (JP-registered accounts get 403 on KR/VN exits), which is an account-provisioning constraint, not a code issue.
- Release assets are rebuilt from the committed tree with `scripts/build_installer.ps1` and must be uploaded together with the matching SHA-256 manifest.
