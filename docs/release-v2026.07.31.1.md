# v2026.07.31.1

## Documentation refresh

- Updated the operator README with the current desktop workflows for account liveness, AT state display, JIT AT refresh, stable AT 200 registration targets, and resumable batch protocol payment.
- Documented the MoMo five-stage proxy chain, Kakao structured result contract, eligibility matrix fields, canary behavior, and token-free checkpoint reports.
- Corrected the registration/authentication boundary: Agent Identity is no longer a registration stage and is available only through an explicit SUB2API import path.
- Updated the architecture and directory maps with `payment_auth.py`, `payment_batch.py`, the WPF batch-payment surface, and the current module ownership rules.
- Added release procedure guidance for same-day patch tags and pre-upload SHA-256 verification.

## Maintenance and code changes

- Removed the unused Kakao role-level proxy-state helpers (`select_verified_proxy`, `record_role_success`, `record_role_failure`, `remove_seed_when_all_roles_removed`, `role_seed_usable`, `role_seed_record`); the seed-level path is now the single source of proxy health tracking.
- Added the `PP_STRIPE_PUBLISHABLE_KEY` environment override for the shared Stripe publishable-key fallback in `gen_pp_link.py` and `ac_paylink_core.py`, and log a warning when checkout returns no key so the fallback is observable.
- The Sentinel SDK download now emits an explicit version-rotated hint on HTTP 403/404, so a stale `OPENAI_SENTINEL_VERSION` is diagnosable instead of silently dropping the registration OTP.
- Added `scripts/preflight_env.py` to verify Node.js, Playwright Chromium, and key Python packages before first run.
- Added regression tests: `tests/test_kakao_extract.py` (seed-failure classification, dead-helper guard) and expanded `tests/test_momo_qr_extract.py` (decision matrix, artifact scanners, redirect follower).

## Validation

- Documentation changes are ASCII/UTF-8 clean and pass `git diff --check`.
- `python -m pytest -q` passes (541 passed, 1 skipped, 7 subtests) with the new regression tests.
- Release assets are rebuilt from the committed tree with `scripts/build_installer.ps1` and must be uploaded together with the matching SHA-256 manifest.
