# Rule 22 — The registration lane must not import the payment lane

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 22`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `python scripts/lane_coupling_ratchet.py` exits 0; `pytest tests/governance/test_lane_coupling_ratchet.py` (17 cases)
- **Source section**: Dependency Direction; Ownership Matrix

## Rule

**The registration lane must not import the payment lane.**  Direct imports in either scope count — a function-local import is the documented cycle-break, not an exemption.  Both offending edges found on 2026-10-18 lived *at the package root*, which is why the directory-granularity import-layer ratchet could not see them: `registration_handlers` / `registration_finalize` / `registration_drivers.browser_flow.orchestrator` imported `paypal_proxy.infer_proxy_country`, and `commands/registration.py` imported `payment_operation`.  The fix moved the definition sites (`proxy_entry.infer_proxy_country`, `operation_journal`) and kept delegating shells, so the lanes now have **zero** direct imports.  The reverse direction is deliberately not zero: the payment lane may borrow exactly three shared-infrastructure modules — `sentinel` (anti-bot client), `phone_proxy` (proxy string authority) and `auth_headers` (fingerprint profiles) — and a fourth target needs a decision in the ratchet, not a silent import.  The same ratchet freezes the non-payment importers of payment code (so a neutral facade cannot re-open the edge), pins the `registration.py` facade fan-out as a **set** rather than a count (moving an import inside the facade cannot mask a new dependency), and enforces that `cli.py` is the only root module importing `commands/` / `cli_parsers/` — the ten edges the directory ratchet reports all originate there, so that pair is an entrypoint, not a layering inversion.
