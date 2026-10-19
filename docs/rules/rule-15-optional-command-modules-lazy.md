# Rule 15 — Optional command modules are lazy seams: importing `sms_tool.cli` must not start a command or pull payment/browser dependencies as a side effect

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 15`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `pytest tests/workbench/test_cli_import_surface.py`; `grep -n "^from \.\(paypal\|payment_batch\|paypal_link\|pay_link\|playwright\|commands\|batch_runner\|storage\)" sms_tool/cli.py` returns nothing
- **Source section**: CLI

## Rule

Optional command modules are lazy seams: importing `sms_tool.cli` must not start a command or pull payment/browser dependencies as a side effect. The seven command adapters are `_LazyModule` proxies and `batch_runner` / `storage` / `commands.helpers` are per-symbol resolvers, so a 30 ms help screen does not pay for `requests` / `curl_cffi` / `gen_pp_link` / `upi_link`. The guard is an import-surface test, not a grep: it runs a child interpreter and asserts those modules are absent from `sys.modules`.
