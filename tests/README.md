# Test Layout

The test suite is intentionally small and offline by default. Run it with:

```powershell
python -m unittest discover -s tests
```

## Files

- `test_entrypoints.py` verifies process entrypoints and lazy optional command seams.
- `test_cli_one_click_sms.py` covers selected-mailbox CLI seams and one-click SMS command assumptions.
- `test_account_scan.py` covers account scan classification and phone-probe semantics.
- `test_registration_concurrency.py` covers mailbox parsing and batch registration worker behavior.
- `test_chatai_mailbox_graph.py` covers Chatai/Microsoft Graph mailbox proxy/scope behavior.
- `test_mail_otp_web.py` covers the standalone `services/mail-otp-web` mailbox-line parser.
- `test_cfworker_mailbox.py` covers CFWorker mailbox endpoint fallback and OTP extraction.
- `test_email_otp_filtering.py` covers message recipient, subject, and body OTP filtering.
- `test_storage_dedup.py` covers SQLite account upsert and email normalization behavior.
- `test_gen_pp_link.py` covers hosted Stripe/PayPal link generation error handling.
- `test_nextsms.py` covers the NexSMS phone-provider adapter.
- `test_codex_oauth.py` covers OAuth/passwordless/add-phone routing decisions.
- `test_paypal_browser_auto.py` covers the project-local PayPal browser adapter without launching a browser.
- Account/session seed loading is centralized in `sms_tool.account_seed`; payment tests should patch that seam or the adapter-specific alias instead of duplicating SQLite/session setup.
- `test_paypal_nocard.py` covers the explicit no-card PayPal agreement payment module.
- `test_proxy_pool.py` covers the local SOCKS5 proxy pool.
- `test_cpa_import.py` covers CPA payload normalization and import routing.

Network and live-browser smoke tests must stay opt-in through environment variables or explicit local commands.
