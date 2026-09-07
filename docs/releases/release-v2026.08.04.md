# v2026.08.04

This release separates payment command orchestration from UI and provider
implementations, formalizes Checkout and terminal-result contracts, and tightens
repository cleanup and release boundaries.

## Module boundaries

- Moved protocol-payment CLI adaptation into `sms_tool.commands.payment`.
  `sms_tool.cli` remains the parser composition and compatibility entrypoint.
- Moved deterministic WPF payment command planning and backend-result
  presentation into `ProtocolPaymentExecution.cs`; `MainWindow.Payment.cs`
  remains responsible for control state and invocation.
- Kept ChatGPT Checkout/Stripe-init request and response normalization in
  `checkout_contract.py`, with side-effect-limited capability probing in
  `payment_capability.py`.
- Kept GoPay, GCash, and GrabPay behind one shared wallet provider/transport
  adapter. This GoPay implementation is a ChatGPT Checkout wallet adapter and
  does not restore the retired gRPC, ADB, phone, or balance service.
- Kept PayPal merchant-return reconciliation in a separate API so link
  extraction retains its existing interface and persistence semantics.

## Payment contracts

- Unified link results now distinguish `completed`, `failed`, `cancelled`,
  `unknown`, and `timed_out`, with structured `retryable` and `error_stage`
  fields.
- Unknown post-side-effect results require reconciliation and are not retried
  automatically.
- Matrix and Canary probe-only runs stop after Checkout and Stripe init; they do
  not create a payment method or send Confirm/Approve requests.
- `direct_card` remains available for PH/PHP Checkout link extraction. The
  machine-local bind/pay executor, browser bridge, WebView2 UI, and executor-only
  CLI flags were removed.

## Repository hygiene

- Added ignore rules for tool-local memory, test result files, coverage output,
  and .NET/Python generated artifacts.
- Documented the distinction between disposable caches/build output and local
  configuration, account data, tokens, sessions, provider state, and resumable
  payment checkpoints that must not be removed by broad cleanup commands.
- Release assets continue to be produced together by
  `scripts/build_installer.ps1`: installer, portable Windows ZIP, and SHA-256
  manifest.

## Compatibility

- Existing root entrypoints and payment-link flags remain available; retired
  direct-card executor flags are intentionally no longer accepted.
- Local `config.json`, sessions, runtime checkpoints, and provider state are not
  migrated or removed by the release.

## Release checks

- `python -m pytest -q`
- `python -m compileall -q sms_tool services/protocol-payment`
- `dotnet test GPTRegisterTool.slnx -c Release --nologo`
- Parse `config.example.json` as JSON and run `git diff --check`.
- Build all Windows assets once with
  `scripts/build_installer.ps1 -Version v2026.08.04` and verify the generated
  SHA-256 manifest before upload.
