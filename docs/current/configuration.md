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

## Examples and credentials

`config.example.json` uses placeholders or local/documentation endpoints.
The precommit guard rejects literal public-IP URLs in example templates and
checks staged bytes rather than a later unstaged replacement. A public IP is
deployment information, not proof by itself that an authenticated secret leaked.

Local API keys, mailbox credentials, proxy passwords and session data stay in
ignored local files. Never copy them into examples, reports or CI artifacts.
