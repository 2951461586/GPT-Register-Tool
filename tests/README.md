# Test Layout

The test suite is offline by default. Run it with:

```powershell
python -m pytest -q
python -m compileall -q sms_tool services/protocol-payment
dotnet test GPTRegisterTool.slnx -c Release --nologo
```

Live Checkout, Stripe confirmation, browser, mailbox-provider, proxy, and
payment requests are never part of the default suite.

## Files

- `test_entrypoints.py` verifies process entrypoints and lazy optional command seams.
- `test_cli_one_click_sms.py` covers selected-mailbox CLI seams and one-click SMS command assumptions.
- `test_account_scan.py` covers account scan classification and phone-probe semantics.
- `test_account_liveness.py` covers the canonical quota endpoint, response classification, and usage parsing.
- `test_account_recovery.py` covers local status persistence, explicit OAuth recovery, and terminal deactivation handling.
- `test_probe_account_liveness_script.py` covers the operator script's shared liveness contract and output classification.
- `test_registration_concurrency.py` covers mailbox parsing and batch registration worker behavior.
- `test_registration_stage_concurrency.py` covers stage-to-resource mapping, bounded admission, and wait metrics.
- `test_chatai_mailbox_graph.py` covers Chatai/Microsoft Graph mailbox proxy/scope behavior.
- `test_mail_otp_web.py` covers the standalone `services/mail-otp-web` mailbox-line parser.
- `test_cfworker_mailbox.py` covers CFWorker mailbox endpoint fallback and OTP extraction.
- `test_email_otp_filtering.py` covers message recipient, subject, and body OTP filtering.
- `test_storage_dedup.py` covers SQLite account upsert and email normalization behavior.
- `test_gen_pp_link.py` covers hosted Stripe/PayPal link generation error handling.
- `test_checkout_contract.py` covers the canonical Checkout/Stripe init payloads, response normalization, and payment-method evidence extraction.
- `test_payment_capability.py` and `test_payment_capability_batch.py` cover the Checkout + Stripe init probe boundary, matrix forwarding, and capability-aware Canary decisions.
- `test_payment_result_contract.py` covers `cancelled`, `unknown`, and `timed_out` terminal states plus normalized `retryable`/`error_stage` fields.
- `test_wallet_provider.py`, `test_wallet_transport.py`, and `test_wallet_manager_integration.py` cover the shared GoPay/GCash/GrabPay adapter, production transport seams, and manager registration. Wire contracts use offline fixtures under `fixtures/wallet_provider/`.
- `test_codex_oauth.py` covers OAuth/passwordless/add-phone routing decisions.
- `test_paypal_links.py` covers PayPal link regeneration and persistence.
- Account/session seed loading is centralized in `sms_tool.account_seed`; payment tests should patch that seam or the adapter-specific alias instead of duplicating SQLite/session setup.
- `test_paypal_protocol.py` covers BA/EC extraction and Stripe redirect parsing.
- `test_paypal_reconciliation.py` covers the independent PayPal merchant-return allowlist, redirect state machine, outcome classification, retryability, and secret-free output.
- `test_session_refresh.py` covers ordered RT/cookie/browser/Codex session recovery and candidate-token validation boundaries.
- `test_proxy_pool.py` covers the local SOCKS5 proxy pool.
- `test_cpa_import.py` covers CPA payload normalization and import routing.
- `SmsWorkbench.Tests/PaymentMethodsTests.cs` covers the desktop payment-method catalog, aliases, country defaults, and single/batch availability.
- `SmsWorkbench.Tests/ProtocolPaymentExecutionTests.cs` covers deterministic single-account command planning and backend-result presentation without constructing a WPF window.

## Test ownership rules

- Put wire-shape examples under `tests/fixtures/<provider>/`; fixtures must be
  synthetic and secret-free.
- Assert public behavior and contract fields. Tests whose only purpose is to
  assert that a retired private helper remains absent should be removed after
  the migration release.
- Patch the owning seam (`account_seed`, Checkout transport, browser bridge,
  persistence adapter) instead of provider internals from an unrelated test.
- Keep one focused test owner for each contract. Cross-module integration tests
  should verify routing only, not duplicate every owner-module unit case.
- Network and live-browser smoke tests must stay opt-in through environment
  variables or explicit local commands.

Generated `__pycache__`, `.pytest_cache`, `TestResults`, `.trx`, coverage output,
and .NET `bin/obj` directories may be removed after validation. They are not
source and must not be included in commits or release archives.
