# Configuration Ownership

## Decision: shards remain authoritative

The runtime uses `proxy.json`, `runtime.json` and `payment.json`. Keep
`config.json` support for existing installations and first-load migration.
Do not delete a local legacy file automatically.

Default load order:

1. If **any** shard exists, merge existing shards in order:
   `proxy.json` → `runtime.json` → `payment.json`.
2. Later conflicting values win; nested mappings merge recursively.
3. Missing shard keys are **not** backfilled from legacy `config.json`.
4. Only when no shards exist, read legacy `config.json` and migrate to shards.
5. Explicit `load_runtime_config(path)` reads the requested file.

Python `config.py` and WPF `ConfigStore.cs` share this precedence. Editing
`config.json` beside existing shards therefore does not change active settings.
Back up local files before migration or repair. This change does not rewrite
any operator configuration.

## Manifest versus validation

`config_schema.json` is the cross-language **shard ownership manifest**, not
JSON Schema. Keep its name for consumers. `validate_config` and driver preflight
provide runtime checks; they are not a complete JSON Schema validator.

`RuntimeConfig` and `runtime_config_scope` are the production configuration
Interface. `CFG` reads the current scope, but its compatibility overrides are
shared mutable state. Avoid writes to `CFG` in concurrent workflows.

## Egress regions are three independent lines (decision 2026-09-13)

`proxy.registration`, `phone_reuse.proxy` and `paypal_browser` each carry their
own region. **None is derived from another**: no code path copies one into the
other, and changing the registration egress does not move the phone or PayPal
lines. The separation is deliberate — the lines are bought from different
vendors and rotated on different schedules.

What this means in practice:

* A region migration (for example moving registration to VN) is **not** a
  system-wide egress change. The phone and PayPal lines stay where they are
  unless edited explicitly. A mixed-region setup is expected, not a bug.
* `config.json` is a dead file while any shard exists (see
  [Decision: shards remain authoritative](#decision-shards-remain-authoritative)).
  Its proxy list keeps pre-migration values indefinitely, so it is the **wrong
  place** to read the current egress — read `proxy.json`.
* `paypal_browser.country` has **no Python-side consumer today**. The PayPal
  orchestrator reads `paypal_auto` (see `sms_tool/paypal/orchestrator.py`), not
  `paypal_browser`; the latter only appears in the shard-ownership manifest,
  `ConfigStore.cs` and the unread-key allowlist in `tests/test_config_usage.py`.
  Editing it is a no-op for the runtime.

### Known self-inconsistency on the phone line (accepted, not a regression)

The phone line pairs a **Chilean number pool** (`phone_reuse.smsbower.country`
= `151`) with a **US egress**, while `phone_reuse.proxy_match_phone_country` is
`false`, so nothing reconciles number country with egress country.

This predates the 2026-09-11 VN migration and was reviewed on 2026-09-13.
It is **deliberately left as-is** because phone-line success is acceptable.
Do not "fix" it by flipping `proxy_match_phone_country` without measuring —
that flag changes the egress used by *every* phone verification, not just
mismatched ones. The three options considered are recorded in
`docs/audits/scan-2026-09-12-localflow-register-reference.md` (finding P4).

## Examples and credentials

`config.example.json` uses placeholders or local/documentation endpoints.
The precommit guard rejects literal public-IP URLs in example templates and
checks staged bytes rather than a later unstaged replacement. A public IP is
deployment information, not proof by itself that an authenticated secret leaked.

Local API keys, mailbox credentials, proxy passwords and session data stay in
ignored local files. Never copy them into examples, reports or CI artifacts.
