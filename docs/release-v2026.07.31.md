# v2026.07.31

## Desktop UI

- Added a production batch protocol-payment workspace for selected accounts, with payment method, concurrency, retries, canary size, batch ID, resume, and report controls.
- Exposed JIT access-token probing and mailbox OTP OAuth refresh, including HTTP status, token age, remaining lifetime, and refreshed-token state.
- Added an editable registration-region and payment-eligibility matrix, per-account decision results, and phase-aware proxy configuration for MoMo and Kakao.
- Added the ReMail stable-AT target workflow with AT 200 target count, mailbox purchase cap, cost cap, and registration batch ID.
- Added account-list columns for registration region, registration batch, and persistence status, plus AT states for acquired, missing, and HTTP 401 invalid.

## Registration and authentication

- Treats an AT probe returning HTTP 200 as the registration success and persistence boundary.
- Refreshes HTTP 401 access tokens directly through mailbox OTP OAuth, probes the replacement, and persists only replacements that return HTTP 200.
- Classifies `account_deactivated` as a permanent failure and excludes dead ReMail history from later acquisition runs.
- Adds stable AT probing, configurable stage concurrency, rotating authentication fingerprints, and Sentinel prewarming, metrics, concurrency control, and circuit breaking.

## Protocol payment

- Added the production batch payment executor with bounded retries, atomic checkpoint reports, resumable batch IDs, canary pausing, and per-method worker limits.
- Added registration-region and payment-eligibility matrix routing across checkout, promotion, provider, approval, and redirect phases.
- Split MoMo proxy routing into explicit phases and strengthened QR extraction and structured outcome reporting.
- Added a structured Kakao response contract and canary-friendly eligibility decisions.

## Validation

- Full Python suite: `510 passed, 1 skipped, 7 subtests passed`.
- Python bytecode compilation and focused desktop mailbox-pool regression tests completed successfully.
- WPF Release build completed with zero warnings and zero errors.
- Canonical desktop publish completed at `dist/net10/SmsWorkbench.exe`, followed by a successful process startup smoke test.
- Installer, portable ZIP, and SHA-256 manifest were rebuilt for this release.
