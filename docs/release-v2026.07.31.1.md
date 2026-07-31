# v2026.07.31.1

## Documentation refresh

- Updated the operator README with the current desktop workflows for account liveness, AT state display, JIT AT refresh, stable AT 200 registration targets, and resumable batch protocol payment.
- Documented the MoMo five-stage proxy chain, Kakao structured result contract, eligibility matrix fields, canary behavior, and token-free checkpoint reports.
- Corrected the registration/authentication boundary: Agent Identity is no longer a registration stage and is available only through an explicit SUB2API import path.
- Updated the architecture and directory maps with `payment_auth.py`, `payment_batch.py`, the WPF batch-payment surface, and the current module ownership rules.
- Added release procedure guidance for same-day patch tags and pre-upload SHA-256 verification.

## Validation

- Documentation changes are ASCII/UTF-8 clean and pass `git diff --check`.
- Release assets are rebuilt from the committed tree with `scripts/build_installer.ps1` and must be uploaded together with the matching SHA-256 manifest.
