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
* `paypal_auto` itself is **not in `config.py`'s shard map** (only
  `paypal_browser`, `paypal` and `paypal_nocard` are), so `CFG.get("paypal_auto")`
  resolves to nothing and `auto_pay` returns `paypal_auto not configured`
  before it picks a card. The whole PayPal payment lane is therefore dormant,
  which is why the 2026-09-22 removal below has no runtime effect today.

## PayPal checkout SMS has no number source (decision 2026-09-22)

The static phone-pool mode was removed across all three lanes it existed in:

| Lane | What it fed | Where the reader was |
| --- | --- | --- |
| Registration SMS | `phone_reuse.phone_pool` | `sms_tool/phone_reuse.py` |
| Registration SMS fallback | `paypal_auto.phone_number` + `sms_api_url` | `sms_tool/codex_phone.py` |
| PayPal checkout SMS | `paypal_auto.phone_numbers` / `phone_number` | `sms_tool/paypal/config_picker.py` |

All three handed out numbers pointing at activations this codebase never
created, so nothing ever completed or cancelled them — the number kept billing
after the flow had moved on. Numbers now come only from a rental pool built by
`phone_reuse.create_phone_pool`, which owns the full lifecycle.

**PayPal checkout SMS has no rental replacement.** This was measured, not
assumed: a repo-wide search for the rental client in the PayPal package returns
nothing. The plumbing still carries a `phone` / `sms_api_url` pair, so wiring a
source in is a change to `sms_tool/paypal/orchestrator.py` alone, but until then
an SMS gate there fails with `no_number_source` rather than polling an empty URL
for the full 120s timeout. See `docs/TROUBLESHOOTING.md` §12.

The failure is deliberately asymmetric: `flow_steps` and `paypal_reverse` detect
the gate on the page before deciding, so they raise; `nodriver_paypal` clicks
"Send Code" blind and cannot tell "no gate" from "gate I cannot satisfy", so it
warns and continues instead of failing every payment. Both behaviours are pinned
in `tests/test_paypal_sms_lane_removed.py`.

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
