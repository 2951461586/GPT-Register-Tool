# v2026.08.02

This release removes the retired GoPay integration and tightens ownership around
account health, registration concurrency, and desktop payment-method metadata.

## Removed surfaces

- Removed the GoPay desktop options, Python adapter, gRPC subprocess client,
  service implementations, protobuf contracts, launcher, configuration, and
  dedicated tests.
- Removed the now-unused `grpcio` and `protobuf` Python dependencies.
- Removed duplicate structural tests whose behavior is covered through the
  focused module interfaces.

## Architecture

- Added `sms_tool.account_liveness` as the sole owner of the canonical
  `/backend-api/wham/usage` probe, response classification, and quota parsing.
- Added `sms_tool.account_recovery` for local quota refresh persistence,
  explicit HTTP-401 OAuth recovery, and permanent-deactivation handling.
- Reduced `sms_tool.cpa_import` to CPA listing, remote quota proxying, payload
  conversion, and upload responsibilities.
- Added `sms_tool.registration_concurrency` so resource admission and wait
  metrics are independent from registration progress persistence.
- Centralized WPF payment names, aliases, countries, and single/batch
  availability in `SmsWorkbench/PaymentMethods.cs`.

## Runtime fixes

- Registration, account scan, payment JIT authentication, maintenance tooling,
  and Kakao Pay now share the same liveness semantics. HTTP 401 is terminal for
  the current AT; 403, rate limits, and transport failures remain inconclusive.
- Kakao Pay permits a foreign-region checkout bootstrap before requiring the
  zero-KRW Kakao provider result at the provider stage.
- BLIK remains available for single-account execution and is excluded from
  registration-after-payment and batch selectors.

## Validation

- `python -m pytest -q`: 524 passed, 1 skipped, 7 subtests passed.
- `dotnet test GPTRegisterTool.slnx -c Release --nologo`: 25 passed.
- Python compile check, payment-method CLI smoke test, example-config JSON parse,
  and `git diff --check`: passed.
