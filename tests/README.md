# Test Layout

The test suite is offline by default. Run it with:

```powershell
python -m pip install -r requirements-dev.txt -c constraints.txt
python -m pytest -q -n 4 --dist loadfile
python -m pytest -q tests/registration tests/workbench
python -m compileall -q sms_tool
dotnet test GPTRegisterTool.slnx -c Release --nologo
python scripts/config_schema_check.py
python scripts/ipc_schema_check.py
python scripts/result_schema_check.py
python scripts/experiment_ratchet.py
python scripts/rust_crates_guard.py --cargo-check
python scripts/spec_validate.py
python scripts/docs_consistency_scan.py
```

Live Checkout, Stripe confirmation, browser, mailbox-provider, proxy, and
payment requests are never part of the default suite.

## Files

- `tests/workbench/test_entrypoints.py` verifies process entrypoints and lazy optional command seams.
- `tests/workbench/test_cli_one_click_sms.py` covers selected-mailbox CLI seams and one-click SMS command assumptions.
- `tests/accounts/test_account_scan.py` covers account scan classification and phone-probe semantics.
- `tests/accounts/test_account_liveness.py` covers the canonical quota endpoint, response classification, and usage parsing.
- `tests/accounts/test_account_recovery.py` covers local status persistence, explicit OAuth recovery, and terminal deactivation handling.
- `tests/accounts/test_probe_account_liveness_script.py` covers the operator script's shared liveness contract and output classification.
- `tests/registration/test_registration_concurrency.py` covers mailbox parsing and batch registration worker behavior.
- `tests/registration/test_registration_stage_concurrency.py` covers stage-to-resource mapping, bounded admission, and wait metrics.
- `tests/registration/test_camoufox_sandbox.py` and `test_camoufox_bridged_geo.py` cover Camoufox content-sandbox isolation, `camoufox.fingerprints.Screen`, and bridged-proxy GeoIP alignment.
- `tests/mailbox/test_chatai_mailbox_graph.py` covers Chatai/Microsoft Graph mailbox proxy/scope behavior.
- `tests/mailbox/test_cfworker_client.py` covers the `sms_tool.providers.mailbox_cfworker` implementation and OTP extraction.
- `tests/mailbox/test_email_otp_filtering.py` covers message recipient, subject, and body OTP filtering.
- `tests/accounts/test_account_events.py` covers storage fact dispatch without provider coupling.
- `tests/accounts/test_storage_dedup.py` covers SQLite account upsert and email normalization behavior.
- `tests/governance/test_config_schema_check.py` and `test_ipc_schema_check.py` guard cross-language manifests.
- `tests/governance/test_result_schema_check.py` guards `result_schema.json`: a builder's result keys and every declared C# reader's reads are checked against it in both directions.
- `tests/governance/test_experiment_ratchet.py` guards `scripts/experiment_ledger.json`: every `registration.*` A/B toggle has a kind, default, status and runbook id, and the ledger still matches the code and `config.example.json`.
- `tests/governance/test_rust_stats_consistency.py`, `test_protocol_payment_rust_parity.py`, and `test_wave*_rust_parity.py` guard all 57 `rust/*-rs/` PyO3 crates, zero-`unsafe` policy, doc totals, and dual-mode Python-vs-Rust parity.
- `tests/governance/test_docs_consistency.py` guards current release pointers and documented provider paths.
- `tests/payment/test_extract_server.py` covers the unified external paylink extraction server adapter for all 15 catalog payment methods.
- `tests/payment/test_protocol_pay.py` covers offline protocol card payment codecs (`protocol_codec.py`), billing catalog, Stripe classifiers, and `protocol_pay.py` workflows.
- `tests/payment/test_checkout_contract.py` covers canonical Checkout/Stripe init payloads, response normalization, and payment-method evidence extraction.
- `tests/payment/test_payment_capability.py` and `test_payment_capability_batch.py` cover read-only Checkout + Stripe init probes, matrix routing, and capability-aware Canary decisions.
- `tests/payment/test_payment_result_contract.py` covers `cancelled`, `unknown`, and `timed_out` terminal states plus normalized `retryable`/`error_stage` fields.
- Account/session seed loading is centralized in `sms_tool.store.account_seed`; payment tests should patch that seam or the adapter-specific alias instead of duplicating SQLite/session setup.
- `tests/payment/test_paypal_protocol.py` and `test_paypal_reverse_pure.py` cover BA/EC extraction, Stripe redirect parsing, and PayPal reverse form parsing.
- `tests/accounts/test_codex_oauth.py` covers OAuth/passwordless/add-phone routing decisions.
- `tests/accounts/test_session_refresh.py` covers ordered RT/cookie/browser/Codex session recovery and candidate-token validation boundaries.
- `tests/proxy/test_proxy_pool.py` covers the local SOCKS5 proxy pool.
- `tests/accounts/test_cpa_import.py` covers CPA payload normalization and import routing.
- `tests/SmsWorkbench.Tests/PaymentMethodsTests.cs` covers the desktop payment-method catalog, aliases, country defaults, and single/batch availability.
- `tests/SmsWorkbench.Tests/PaymentBatchServiceTests.cs`, `PaymentBatchViewModelTests.cs`, and `ProtocolCardPaymentViewModelTests.cs` cover payment batch routing, protocol card payment workflows, config persistence, and CLI argument mapping.

## Test ownership rules

- Put shared cross-language fixtures under `tests/fixtures/`; fixtures must be
  synthetic and secret-free.
- Assert public behavior and contract fields. Tests whose only purpose is to
  assert that a retired private helper remains absent should be removed after
  the migration release.
- Patch the owning seam (`store.account_seed`, Checkout transport, browser bridge,
  persistence adapter) instead of provider internals from an unrelated test.
- Provider tests import implementations directly from `sms_tool.providers`.
- Keep one focused test owner for each contract. Cross-module integration tests
  should verify routing only, not duplicate every owner-module unit case.
- Network and live-browser smoke tests must stay opt-in through environment
  variables or explicit local commands.

Generated `__pycache__`, `.pytest_cache`, `TestResults`, `.trx`, coverage output,
and .NET `bin/obj` directories may be removed after validation. They are not
source and must not be included in commits or release archives.
