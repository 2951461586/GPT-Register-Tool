# Test Layout

The test suite is intentionally small and offline by default. Run it with:

```powershell
python -m unittest discover -s tests
```

## Files

- `test_entrypoints.py` verifies process entrypoints and lazy optional command seams.
- `test_registration_concurrency.py` covers mailbox parsing and batch registration worker behavior.
- `test_storage_dedup.py` covers SQLite account upsert and email normalization behavior.
- `test_gen_pp_link.py` covers hosted Stripe/PayPal link generation error handling.
- `test_paypal_browser_auto.py` covers the project-local PayPal browser adapter without launching a browser.
- Account/session seed loading is centralized in `sms_tool.account_seed`; payment tests should patch that seam or the adapter-specific alias instead of duplicating SQLite/session setup.
- `test_paypal_nocard.py` covers the explicit no-card PayPal agreement payment module.
- `test_proxy_pool.py` covers the local SOCKS5 proxy pool.
- `test_cpa_import.py` covers CPA payload normalization and import routing.

Network and live-browser smoke tests must stay opt-in through environment variables or explicit local commands.
