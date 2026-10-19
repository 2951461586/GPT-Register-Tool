# Rule 16 — A hub module's private symbols must not gain new cross-module consumers

> Part of [Boundary Rule Checks](../../architecture.md#boundary-rule-checks).
> The **number is a contract**: tests, scripts and other docs cite `Rule 16`,
> so a renumbered rule is a broken reference, not a cosmetic change.

- **Enforced by**: `python scripts/mailbox_private_import_ratchet.py` exits 0, and `scripts/mailbox_private_import_baseline.json` lists every triple under its module
- **Source section**: Ownership Matrix; Dependency Direction

## Rule

A hub module's private symbols must not gain new cross-module consumers. A hub is a module whose public surface is effectively empty while other modules reach into it anyway — its `_`-prefixed names are a de facto interface, and nothing else counts them. Six are watched: `mailbox` (~40 top-level functions, **every one `_`-prefixed**), `session_refresh`, `mail_otp`, `sentinel_tokens`, `http_utils` and `utils`; a 2026-09-17 survey found each has a private-definition count close to its consumed-symbol count, i.e. they too are hubs rather than modules with a thin private tail. The ratchet records today's `(importer, watched_module, symbol)` triples **per module** and fails only when a set grows; it does not demand that existing consumers be rewritten. Baselines are per module on purpose: with one grand total, a reduction in `mailbox` would mask new coupling onto `utils`.
