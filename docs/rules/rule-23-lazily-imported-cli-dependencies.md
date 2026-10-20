# Rule 23 — Lazily imported CLI dependencies must self-heal; a one-shot latch must not outlive a patched symbol

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 23`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `grep -n "_heavy_deps_loaded" sms_tool/cli.py` returns nothing; `pytest -q tests/registration tests/workbench` exits 0; `pytest tests/workbench/test_cli_heavy_deps.py`
- **Source section**: CLI; Rule 15

## Rule

**Lazily imported CLI dependencies must self-heal; a one-shot latch must not outlive a patched symbol.** `sms_tool/cli._load_heavy_deps` resolves `load_mailbox_pool` / `remail_enabled` / `_build_session_file` / `_mailbox_snapshot` / `run_email` by presence on every call and keeps **no** `_heavy_deps_loaded` latch. `patch.object` restores a patched symbol to its pre-patch value, so a latch set while one symbol was a mock left the real symbol `None` for the rest of the process — `--one-click-sms` then called `None(...)`. Resolving again is a `sys.modules` hit after the first import, so the cost is unchanged, and a symbol that is already resolved (or deliberately patched) is never overwritten. The 2026-10-18 defect was order-dependent: `tests/registration` before `tests/workbench` failed while the full suite passed, because an earlier test had already resolved the symbols outside a patch; `ci.yml` now runs that shard order explicitly.
