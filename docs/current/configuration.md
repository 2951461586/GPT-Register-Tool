# Configuration Ownership

## Decision: shards remain authoritative

The runtime uses `proxy.json`, `runtime.json` and `payment.json`. Keep
`config.json` support for existing installations and first-load migration.
Do not delete a local legacy file automatically.

## Where the files live

Config shards and the default mailbox pool (`mailbox_tokens.txt`) live in a
per-user directory **outside the repository**:
`%LOCALAPPDATA%\GPTRegisterTool\config` (`$XDG_CONFIG_HOME/GPTRegisterTool/config`
off Windows). `GPT_REGISTER_CONFIG_DIR` overrides it; the WPF shell exports the
same value to every Python process it starts, so both sides always agree.

* Resolution: `sms_tool/kernel/paths.py:config_dir()` and C# `ApplicationPaths.ConfigDirectory`.
* Migration: when the directory is the implicit default, files left in the repo
  root (`config.json`, `proxy.json`, `runtime.json`, `payment.json`,
  `mailbox_tokens.txt` and their `.bak`) are **moved** once, never overwriting an
  existing target. An explicit `GPT_REGISTER_CONFIG_DIR` never pulls files out of
  the checkout. Both languages implement the same rule, so whichever starts
  first leaves nothing for the other.
* A relative `email_registration.token_file` is relative to this directory
  (`resolve_config_file`); a legacy repo-root file is still honoured until moved.
* The test suite sets `GPT_REGISTER_CONFIG_DIR` to a temporary directory seeded
  from `config.example.json`, so it never sees operator credentials. Under
  `pytest -n 4` **every xdist worker gets its own** temporary directory: the
  controller imports `tests/conftest.py` too and its environment is inherited by
  the worker subprocesses, so a single shared directory would make all four
  workers run the first-load migration at once — and a worker that reads while
  only `proxy.json` is published sees a config with no `chatgpt` section
  (`ConfigError: chatgpt must be an object`, TROUBLESHOOTING §18). An explicit
  `GPT_REGISTER_CONFIG_DIR` set by the operator is never replaced.
* `sessions/` and `runtime/` stay in the project root (they are gitignored
  runtime output, not configuration).

## Bootstrapping a fresh machine

`python -m sms_tool --init-config` writes `proxy.json`, `runtime.json` and
`payment.json` from `config.example.json` and exits. It exists because the two
older paths both had a sharp edge:

* copying the example to `config.json` only worked by triggering the legacy
  first-load migration, which then moved the file; and
* doing nothing left the directory empty, and `load_runtime_config` raises on an
  empty config (`chatgpt must be an object`) **before** the CLI parses arguments
  — so `--doctor`, the command that is supposed to explain the problem, could
  not run either.

It is safe to re-run: an existing shard is reported `skipped` and left byte-for-
byte alone, so a partially configured machine (an operator edited `proxy.json`
but never had `payment.json`) is completed rather than reset. The example is
looked up in the config directory first, then beside the package, so a portable
install works without a checkout.

`--init-config` is handled ahead of the real parser for the same reason: it must
run on the machine whose config is the thing that is broken. `--doctor` reports a
missing shard as `config_shards: warn` and names this command in the hint.

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

## Desktop theme preference

The WPF theme button saves `Light` or `Dark` in
`runtime/desktop_theme.txt` after applying the selected theme. On the next
launch, WPF reads that preference before creating its main window. A missing,
unreadable or invalid file falls back to the system theme. This desktop-only
preference does not enter the shared Python configuration shards.

## Browser pool memory valve stays off by default (decision 2026-10-21)

`registration.browser_process_pool.max_total_rss_mb` is a **RSS watchdog**, not a
soft limit: when the pool's process tree exceeds it, the watchdog recycles a
browser that may be mid-registration. So the default stays `0` (off) in both the
code (`PoolConfig.max_total_rss_mb`) and `config.example.json` -- the same
reasoning as ADR-0013 for the other opt-in valves: turning it on for existing
installations without a canary can abort a live registration.

What was missing was the number to configure, not the switch. Measured
2026-10-20 (3 repetitions): one headless Chromium process tree settles at
**~478 MB**, four at **~1.9 GB**; Camoufox is Gecko and heavier. The
recommendation is therefore `max_concurrent * 512 MB` -- 2048 MB for the default
4-way pool -- and it is not written into the example config. It is surfaced
three ways instead:

* `PoolConfig.recommended_max_total_rss_mb()` (the arithmetic, in one place);
* a one-per-pool warning when the valve is off with more than one browser;
* `pool.stats()["recommended_max_total_rss_mb"]`, so an operator can see the
  number for the pool they actually run.

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
  `ConfigStore.cs` and the unread-key allowlist in `tests/config/test_config_usage.py`.
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
in `tests/payment/test_paypal_sms_lane_removed.py`.

### Known self-inconsistency on the phone line (accepted, not a regression)

The phone line pairs a **Chilean number pool** (`phone_reuse.smsbower.country`
= `151`) with a **US egress**, while `phone_reuse.proxy_match_phone_country` is
`false`, so nothing reconciles number country with egress country.

This predates the 2026-09-11 VN migration and was reviewed on 2026-09-13.
It is **deliberately left as-is** because phone-line success is acceptable.
Do not "fix" it by flipping `proxy_match_phone_country` without measuring —
that flag changes the egress used by *every* phone verification, not just
mismatched ones. The three options considered are recorded in
`docs/audits/scans/scan-2026-09-12-localflow-register-reference.md` (finding P4).

## Unified proxy lanes and the optional `proxy.lanes` declaration (2026-09-28)

The three egress lanes resolve through three deliberately different modules
(Rule 19 keeps them apart):

| Lane | Resolver | Legacy declaration keys |
| --- | --- | --- |
| registration (protocol) | `proxy_routing.proxy_pool_for` | `proxy.registration` / `proxy.pool` / `proxy.default` |
| registration (browser) | `proxy_routing.proxy_pool_for` | `proxy.browser_registration_pool` / `proxy.browser_pool` |
| mailbox / OTP | `mailbox._mailbox_proxy_candidates` | `mailbox_proxy` / `mailbox_proxy_pool` |
| payment qualification | `proxy_routing.canonical_lane_pool` (`payment_qualification` lane), `promotion` fallback | `payment_qualification.proxy_pool` |
| payment | `payment_routing` + per-method sections | `protocol_payments.proxy_pools`, `<method>.stage_proxies` |

`sms_tool/proxy/registry.py` is the single read/manage surface over all three
(`resolve` / `census`, credential-free). It **fronts** the resolvers; it does
not replace them.

When `proxy.lanes` is present it is **authoritative** for the matching lane;
when absent (the default) every resolver uses its legacy keys unchanged, so
existing shards keep working byte-for-byte. All three live paths honour it:
`proxy_pool_for` for registration, `mailbox._mailbox_proxy_candidates` for the
OTP poller, and `PaymentRoutePlanner._named_pools` for payment region pools.

```jsonc
"proxy": { "lanes": {
  "protocol_registration": ["http://..."],
  "browser_registration":  ["http://..."],
  "mailbox":               ["http://proxy.example:8080"],
  "payment_qualification": ["http://stripe-reachable.example:8080"],
  "payment": { "pools": { "US": ["..."], "JP": ["..."] }, "default": ["..."] }
}}
```

`payment_qualification` should be **Stripe-reachable** (`GET https://api.stripe.com/`),
not merely ChatGPT-reachable. Since 2026-10-27 a failed Stripe preflight no
longer aborts the batch by default: it emits a `batch_warning` with the first
denial (host + status + reason) and the run continues, because the server's
`oaics_` custom-checkout path never calls Stripe and can still answer. Set
`payment_qualification.stripe_preflight: "strict"` to abort instead, or `false`
to skip the probe. When the previous verdict for **every** selected account came
from an `oaics_` checkout, the preflight is skipped entirely
(`stripe_preflight.reason=oaics_only`). All five `proxy.lanes` keys above are
accepted by `validate_config` (`sms_tool/config/schema.py`), so a desktop save
cannot produce a config the backend then refuses to load.

`proxy.lanes.payment.pools` names are the ones referenced by
`protocol_payments.methods.<m>.stage_routes` / `stage_proxy_pools`.

The desktop **设置 → 基础网络** page edits the same canonical lanes directly:
`protocol_registration`, `browser_registration`, `mailbox`, and
`payment_qualification` each have their own box/pool field. A non-empty save
writes the lane (creating the `proxy.lanes.<lane>.pool` object when absent) and
mirrors the legacy keys; a blank save leaves the lane untouched. The payment
lane remains owned by the protocol-payment dialog and has no field here.

## No invented proxy default (decision 2026-10-25)

An empty proxy field means "nothing configured here", never "use a localhost
proxy". Both sides used to substitute a hardcoded `http://127.0.0.1:7897`: the
desktop read path (`MainWindow.Helpers`) and the settings writer
(`SettingsService.Save`, which then persisted it into `proxy.registration`,
`proxy.default` and `proxy.pool`), and the catalog prefilled the same value.

That mattered because `cli.py` sets `args.proxy_explicit = bool(args.proxy)`, and
an explicit scalar suppresses the lane pools. A desktop-invented value therefore
made **every** desktop run "explicit", pointed fresh installs at a proxy that
need not exist, and hid the configured `protocol_registration_pool` /
`browser_registration_pool` behind it.

Current behaviour:

* Long `sms_tool/commands/accounts.py` probes, registration and mailbox reads
take the configured value only. Blank means the desktop passes no `--proxy` and
no `--proxy-pool`, and the backend resolves the route itself: `proxy.default`,
then the lane pools, then `registration_network_preflight`'s named reason code
(exit 2). A missing route now fails with a reason instead of dialling a local
proxy.
* `config.example.json` ships `proxy.registration`, `proxy.health`,
`proxy.default`, the registration lane pools and
`account_health.proxy_pool`/`account_health.proxies.*` **empty**. Only
`proxy.pool` and `proxy.lanes.payment` keep placeholder entries (a non-loopback
`proxy.example` host for the stage proxies), so copying the template cannot pin
this machine's port.
* Health lanes keep their own precedence: for `liveness`, `promotion` and
`health_browser`, `account_health.proxies.<lane>` and `account_health.proxy_pool`
outrank the registration-pool fallback (`proxy_routing.proxy_pool_for`).

Inspect everything with the credential-free census:

```powershell
python scripts/proxy_census.py
python scripts/proxy_census.py --json --methods paypal,upi,kakao,momo
```

The census reports `registration`, `mailbox`, `payment_qualification` and
`payment`, each with its `source` key, and flags any pool whose credentials it
shares with registration (`lane_isolation_ok`). The qualification row is the one
to read first when a batch aborts with `abort_reason=stripe_unreachable`:
`source=unset (promotion fallback)` means no dedicated exit is configured, not
that the configured one failed.

`proxy` remains owned by the `proxy` shard (`SHARD_OWNERSHIP`), so `lanes`
routes to `proxy.json` and needs no schema change.

## Protocol fingerprint pool knobs (`registration.fingerprint_pool`)

The **protocol** registration path selects from
`fingerprint_pool.FingerprintPool`, whose knobs live under
`registration.fingerprint_pool`. The browser path uses
`browser_fingerprint_pool` instead and does not read this section.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `mode` | `"random"` \| `"round_robin"` | `"random"` | weighted-random family selection vs deterministic order (tests/operators) |
| `verify_hint` | bool | `true` | measure the exit even when the credential advertises a region; a disagreement is logged and the measurement wins. The shared resolver caches per proxy, so it costs one probe per exit (not per account). Set `false` only for a provider proven to honour its region token |
| `allowed_countries` | array of ISO-3166 alpha-2 | `[]` | narrow the geo-bound selection; empty means no filter. Non-matching draws fall back to the closest profile with a one-time warning |

```jsonc
"registration": {
  "fingerprint_pool": {
    "mode": "random",
    "verify_hint": true,
    "allowed_countries": []
  }
}
```

Owned by the `runtime` shard (`SHARD_OWNERSHIP`), so it lives in `runtime.json`.
`validate_config` rejects a wrong type, an unknown `mode`, or a non-ISO
`allowed_countries` entry instead of silently falling back to the default pool.
The pool is a process-lifetime singleton keyed by this section, so changing it
mid-process does not rebuild an already-created pool.

## Payment qualification (`payment_qualification`)

The read-only 支付资格 probe (`sms_tool/payment/qualification.py`) reads one
optional section. `--payment-qualification-method` / `-country` on the CLI
override these values per run.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `method` | string or array | `"auto"` | `auto` derives candidates from the measured exit country; a comma list / array forces methods. Unknown ids become an `unknown_method` row, not a crash |
| `country` | ISO-3166 alpha-2 or `""` | `""` | Force the billing/request country. Empty measures the exit first, then falls back to `registration_country` / `identity_context.country` |
| `probe_all_methods` | bool | `false` | `false` stops after the first eligible method (each candidate is its own Checkout create). `true` restores the full diagnostic grid; the CLI equivalent is `--payment-qualification-all-methods` |
| `proxy_pool` | array of proxy URLs | `[]` | Dedicated egress for the probe (also accepted as `proxy.lanes.payment_qualification`). When set it is placed **ahead** of the desktop's registration proxy, which every desktop/CLI run also passes; it is no longer appended behind it. Empty falls back to the promotion lane |
| `max_proxy_attempts` | int | `3` | Per-account egress attempts on transport/risk failures, clamped 1..32. Raise it only after the pool is known to contain reachable exits |
| `stripe_preflight` | bool or `"warn"` / `"strict"` | `true` (= `warn`) | Probe `https://api.stripe.com/` through the candidate pool before any account. `warn` (default) emits a `batch_warning` naming the first denial and keeps probing — an `oaics_` custom checkout can still produce a verdict; `strict` aborts with `abort_reason=stripe_unreachable`; `false` skips the probe. Skipped as well when every selected account's last verdict came from an `oaics_` checkout. Any unrecognised value degrades to `warn` |
| `stripe_preflight_cache_seconds` | number | `300` | Reuse the last Stripe-preflight verdict for the same candidate set (credential-free hash of the exits + source) instead of re-probing it. Three retries in two minutes used to send 9×3 requests through a pool that had already answered (measured 2026-10-27). `0` disables the cache; changing the pool changes the key, so a fixed pool takes effect immediately |

`--refresh-timeout` is the **per-request** HTTP timeout. The per-account bound is
that value × `max_proxy_attempts` (45 × 3 = 135 s by default): each attempt's
request timeout is capped by the time left, and when the budget is gone the probe
stops with `error_code=account_budget_exhausted` instead of dialling another exit.
Measured 2026-10-27, one account occupied 72 s while the operator read the field
as a run budget.

States (`payment_qualification_state`) are `eligible` / `ineligible` /
`auth_invalid` / `egress_denied` / `probe_failed` / `unknown`. `egress_denied`
means the **exit** refused the target host (`503 Target host denied`,
`Tunnel connection failed`); it is not an account verdict and not a method
verdict, so the badge reads 出口被拒 and the batch counts it as an environment
failure (breaker + rotation) rather than 其他失败. The completion line reports it
in its own `出口被拒 N` field, so the per-state counters add up to the account
count. When the preflight already proved every candidate denies Stripe, an
`egress_denied` result stops the rotation for that account (another exit in a
proven-dead pool cannot flip a host-level ACL); a risk refusal still rotates,
because it happens on `chatgpt.com` and is exit-dependent.

Owned by the `runtime` shard. The persisted badge lives in
`raw_json.payment_qualification` (`store/markers.py`) and is surfaced by
`desktop_read` as `payment_qualification_display` / `_state`.

## ASN allocation and dynamic generation (`proxy.*`)

Both features are **opt-in and off by default**. They never change an existing
pool until an operator enables them.

```jsonc
"proxy": {
  "asn_allocation": { "enabled": false },
  "dynamic_generation": {
    "enabled": false,
    "base_proxy": "",
    "country": "",
    "asns": [],
    "count": 1,
    "session_minutes": 60,
    "max_attempts": 30,
    "quality_min": 80
  }
}
```

* `asn_allocation.enabled` makes the registration batch read the persisted
  `proxy_asn` mapping, order candidates by the ASN round-robin ledger, and
  record each attempt's hit rate. The mapping is written by
  `--proxy-screen` / the desktop screening action (`AsnRegistry.configure`), or
  measured in-process when the fingerprint pool probes the exit. With nothing
  measured the feature is inert (the pool order is unchanged).
* `dynamic_generation.enabled` rotates a fresh sticky session from
  `base_proxy`, dual-pass screens it with the same gates as `--proxy-screen`,
  and requires its ASN to be in `asns` (empty = any residential/mobile). It
  retries up to `max_attempts`; a generation failure falls back to the static
  pool rather than consuming mailboxes.

## Examples and credentials

`config.example.json` uses placeholders or local/documentation endpoints.
The precommit guard rejects literal public-IP URLs in example templates and
checks staged bytes rather than a later unstaged replacement. A public IP is
deployment information, not proof by itself that an authenticated secret leaked.

Local API keys, mailbox credentials, proxy passwords and session data stay in
ignored local files. Never copy them into examples, reports or CI artifacts.
