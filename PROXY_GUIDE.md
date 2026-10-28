# Proxy Guide

Proxy configuration is shard-based. The merged config (`sms_tool.config.load_merged_config`)
reads `proxy.json` / `runtime.json` / `payment.json` when any shard exists, and
falls back to the legacy root `config.json` only when no shard does. Shards are
authoritative — see `docs/current/configuration.md`. Keep real proxy ports,
provider names, credentials and local client profile paths out of Git.

## Where Proxy Settings Live

| Key (merged view) | Shard | Used for |
| --- | --- | --- |
| `proxy.default` | `proxy.json` | Default egress for registration/checkout requests |
| `proxy.pool` | `proxy.json` | Health-checked proxy pool consumed by registration, remail and the SOCKS5 pool server |
| `proxy.pool_server` | — | (flags only) `start_proxy_pool.py` listen/timeout settings are CLI flags |
| `paypal.proxies` / `paypal.stage_proxies` | `payment.json` | PayPal/Stripe per-stage exits (`direct` = no proxy) |

`proxy_entry.py` is the single parsing authority (boundary rule 13 in
`docs/architecture.md`): `parse_proxy` / `retarget_region` / `rotate_session`
/ `infer_region`. Provider region templates (rola-style `-VN` credential
suffixes etc.) are recognized there — never hand-build provider URLs elsewhere.

## Pool Server

`start_proxy_pool.py` serves a local **SOCKS5** endpoint that rotates over a
pool of upstreams with health checks. Clients always speak SOCKS5 to the
listener; how each upstream is dialed depends on that entry's own scheme:

| Upstream scheme | Dialed with |
| --- | --- |
| `socks5`, `socks5h` | SOCKS5 handshake (RFC 1928 + RFC 1929 auth); `socks5h` passes hostnames through un-resolved for remote DNS |
| `http`, `https` | HTTP `CONNECT` tunnel (`https` wraps the tunnel in TLS) |

```powershell
# Upstreams from the proxy shard (proxy.pool), any supported scheme:
python start_proxy_pool.py

# Or explicit upstreams:
python start_proxy_pool.py --upstreams "http://user:pass@host:8080,socks5://127.0.0.1:7897"
```

HTTP upstreams are not an optional extra: **every residential provider we buy
hands out `http://` endpoints** — the live `proxy.pool` is 30/30 `http`. A
SOCKS5-only filter therefore emptied a perfectly good pool down to zero
upstreams and exited with "no upstreams configured". Entries whose scheme the
pool cannot dial are skipped with a warning; scheme-less entries default to
`socks5`, which keeps older pool files working.

Without `--upstreams` or a usable `proxy.pool` entry the server exits with an
error. The historical silent fallback to `socks5://127.0.0.1:7897` (via the
`config.json proxy_pool.upstreams` key that never existed) was removed on
purpose: a pool that quietly serves your local clash listener instead of the
configured provider is worse than a loud failure.

### Sticky sessions

By default the listener rotates upstream **per connection** — right for fetch
work, wrong for a registration that must present one exit for its whole flow.
Pass `--sticky-session-ttl <seconds>` to pin a client that offers SOCKS5
username/password auth to one upstream for that window (sliding: each reuse
refreshes it):

```powershell
python start_proxy_pool.py --sticky-session-ttl 900
```

The **username** is the session key; the password is accepted but ignored
(there is no user database). Point a long flow at it as
`socks5h://<session-name>@127.0.0.1:18080`, using a distinct name per account so
exits stay isolated. Clients that send no credentials keep per-connection
rotation, so the flag is purely additive. The default is `0` (off). Because any
username is accepted, the server logs a warning when sticky mode is enabled on a
non-loopback listen address.

### Failover across sessions on one endpoint

A rotating provider hands out many sticky sessions through **one** `host:port`
(the live registration pool is ten lajiao `region-VN` entries on
`us.lajiaohttp.net:2000` that differ only in the sid). The retry loop therefore
excludes upstreams by their whole identity — `scheme://user:pass@host:port` —
and not by address, so one blocked session falls through to its siblings. Until
2026-10-26 it keyed on the address, which made the first failure exclude all ten
and answer `general failure` after a single attempt. Two upstreams on the same
endpoint with the same credentials are the same route; two sessions that differ
only in the sid are not.

### Edge-aware health check

By default an upstream is "healthy" once the pool can complete a SOCKS5/HTTP
tunnel to `cloudflare.com:443` — which says nothing about whether Cloudflare
will *serve* `chatgpt.com` through that exit. Add `--health-edge-probe` to make
the probe issue a real HTTPS request and apply the same clean/blocked
classification `scripts/proxy_pool_probe.py` uses, so a 403-challenged exit is
marked unhealthy and rotated out:

```powershell
python start_proxy_pool.py --health-edge-probe
```

Off by default: the tunnel-only probe is unchanged. Each probe costs one HTTP
request per upstream, and `--health-timeout` defaults to 15s in this mode
instead of 5s.

### Stats endpoint (HTTP, loopback only)

`--stats-port` (default `18081`) serves `/health` (200/503) and a JSON inventory
of every upstream — address, scheme, health counters, sticky-session count.

Two rules are enforced by the server, not by convention:

* **It binds to `127.0.0.1` regardless of `--host`.** The endpoint has no
authentication, so exposing the SOCKS5 listener on a LAN address must not
publish the upstream inventory with it. `--stats-host` is the explicit opt-in,
and it logs a warning when it is non-loopback.
* **Labels are credential-free.** A `proxy.pool` entry written in URL form is
`scheme://user:pass@host:port`; its label (used by the stats payload *and* the
health/relay log lines) is masked to `scheme://***:***@host:port`. Nothing that
carries a password reaches `/stats`, the log sinks, or `repr(upstream)`.

```powershell
# default: http://127.0.0.1:18081/health
curl.exe http://127.0.0.1:18081/health
```

### Desktop transport: no proxy credentials in argv

The WPF host calls the Python backend with an argument list, and a residential
pool entry is `scheme://user:pass@host:port`. On Windows every same-user process
can read another process's command line (Task Manager, WMI `Win32_Process`, ETW),
so the launcher moves the two proxy routes out of argv immediately before the
process starts:

| planner emits | launcher sends | backend reads |
| --- | --- | --- |
| `--proxy <url>` | `SMS_TOOL_PROXY=<url>` | env, then flag |
| `--proxy-pool <a\nb>` | `SMS_TOOL_PROXY_POOL=<a\nb>` | env, then flag |
| `--checkout-proxy-pool <a\nb>` | `SMS_TOOL_CHECKOUT_PROXY_POOL=<a\nb>` | env, then flag |
| `--approve-proxy-pool <a\nb>` | `SMS_TOOL_APPROVE_PROXY_POOL=<a\nb>` | env, then flag |

The two payment-lane pools are in the table because they carry the **same**
secret in a bigger carrier: the protocol-payment batch is planned with
`--checkout-proxy-pool` / `--approve-proxy-pool`, whose value is the whole
region-mixed IPWO pool — dozens of `scheme://user:pass@host:port` lines joined
by newlines. Leaving them on argv kept exactly the leak the transport exists to
close, and that payload is also the one most likely to approach the 32K Windows
command-line limit that the environment carrier does not have. Both flags are
also declared in `sensitive_policy.json`'s `sensitive_options`, so the desktop's
redacted argument log masks them instead of printing them.

The plan stays pure and the flags keep working (the CLI, scripts and every
existing caller are unchanged); only the process boundary changed. Precedence is
**flag over environment**, and the backend pops each variable after reading it so
it is not inherited by the child processes it starts (the Sentinel node runner,
browser drivers). The names are pinned on both sides by
`tests/fixtures/proxy_env_transport.json`, and `ProxyArgumentTransport.Routes`
(C#) / `cli._PROXY_TRANSPORT_ROUTES` (Python) are the single lists a new
credential-bearing flag must join.

`--proxy-pool` is still one newline-joined value. That is what made the argv
route attractive in the first place (one token for the whole pool), and it is
also why a large pool could approach the 32K command-line limit — a limit the
environment carrier does not have.

## Verification

Check the configured checkout/approve/update proxy exits without running a
real checkout:

```powershell
python -m sms_tool.cli --test-payment-proxies
```

Verify configured proxies (merged shards) from the operator tool — proxy
credentials are redacted in its output:

```powershell
python verify_proxy.py
```

Can this exit actually serve ChatGPT?  A proxy can be reachable (ipinfo OK) yet
be answered with a Cloudflare 403 at the ChatGPT edge.  The tunnel-only health
check in `start_proxy_pool.py` cannot see that, so probe the pool **before** a
run.  Both probes are read-only — one anonymous GET per proxy, no account, no
mailbox, no checkout — and redact credentials in all stdout:

```powershell
# the configured proxies, ipinfo + edge verdict (clean/blocked/degraded/dead)
python verify_proxy.py --chatgpt

# the whole registration pool; write the clean subset as a pool file
python scripts/proxy_pool_probe.py --workers 8
python scripts/proxy_pool_probe.py --out runtime/pool_clean.txt --require-clean
```

`--out` contains credentials (that is what a usable pool file is), so keep it
out of Git; the default `runtime/` location is already gitignored.  Build the
registration pool only from entries reported `clean`.

Check a local SOCKS5 exit manually:

```powershell
curl.exe --proxy socks5h://127.0.0.1:7897 https://ipinfo.io/json
```

If a local listener is not reachable, fix the proxy client first. Retrying the
registration or PayPal flow will not repair a missing local listener.

## Pool Diagnostics, ASN Ledger, Dynamic Generation

Three read-only commands share one owner (`sms_tool/proxy/admin.py`); each
prints one JSON document and masks credentials in every line and payload:

| Command | Question it answers |
| --- | --- |
| `--proxy-list [--proxy-read-lane <lane>]` | What is configured, with cached geo/health only (no network) |
| `--proxy-test --proxy-country PH` | Does each exit reach the ChatGPT edge, and where does it exit from |
| `--proxy-screen --proxy-country PH --proxy-quality-min 70` | Dual-pass residential screening (stability/country/ASN/quality) plus the ASN ranking |

The same three actions are available in the desktop **代理池** window (read-only).
The desktop deliberately does not write config: materialise a usable pool with
`python scripts/proxy_screen.py --country PH --quality-min 70 --out runtime/pool_clean.txt`
and point the lane at that file (the `--out` file contains credentials and is
gitignored under `runtime/`).

### ASN ledger (`proxy.asn_allocation`)

`--proxy-screen` and the desktop screening action write every residential/mobile
sample into `runtime/asn_performance.sqlite3` (`AsnRegistry.configure`): the ASN
row plus a **credential-free** `proxy_key -> ASN` mapping (endpoint + session
hash, never the password). With `proxy.asn_allocation.enabled: true` the batch
then:

* orders registration candidates by the persisted ASN round-robin cursor;
* records each attempt's hit rate / transport outcome against the measured ASN.

It is off by default and fail-open: with no measured ASN the pool order is
unchanged. The mapping also survives a process restart, unlike the in-memory geo
cache.

### Stripe reachability is not ChatGPT reachability

A residential pool can return HTTP 200 on `chatgpt.com` and still deny
`api.stripe.com` at the proxy tunnel (measured 2026-10-27 on all nine proxylane
ports). Payment qualification therefore has two gates: the ChatGPT edge probe
and a Stripe preflight (`payment_qualification.stripe_preflight`).

Since 2026-10-27 the Stripe gate **warns by default instead of aborting**: a
checkout served as an `oaics_` custom session is answered without Stripe, so a
Stripe-denied pool can still produce verdicts (measured the same day). The run
emits a `batch_warning` naming the first denial (host / status / reason) and
keeps going. Opt into the old fast failure with:

```jsonc
"payment_qualification": {
  "proxy_pool": ["http://stripe-reachable.example:8080"],
  "stripe_preflight": "strict"   // "warn" (default) | "strict" | false
}
```

`payment_qualification.proxy_pool` (or `proxy.lanes.payment_qualification`) is
tried **before** the registration proxy the desktop also passes; put a
Stripe-capable exit here. When every selected account's last verdict came from
an `oaics_` checkout the preflight is skipped automatically, and the verdict is
cached for the same exit set (`stripe_preflight_cache_seconds`, default 300) so
re-running does not hammer a denying pool.

When the exit itself refuses the target host (`503 Target host denied`), the
account badge is **出口被拒 / egress denied** — not 检测失败. The desktop summary
counts it separately because the fix is a different exit, not a different
account. A quick manual check:

```powershell
curl.exe --proxy "http://user:pass@host:port" https://api.stripe.com/
```

`503 Target host denied` / `Tunnel connection failed` means the exit is not
usable for Stripe even though it can reach ChatGPT.

### Dynamic generation (`proxy.dynamic_generation`)

Off by default. When enabled, the batch rotates a fresh sticky session from
`base_proxy` with `proxy.entry.rotate_session`, screens it with the same
dual-pass gate as `--proxy-screen`, and requires the measured ASN to be in
`asns` (empty = any residential/mobile). `max_attempts` bounds the loop; a
generation failure falls back to the static pool instead of consuming mailboxes.
`base_proxy` must use a provider template that `proxy.entry` already understands
(`region-XX` / `geo-XX` / `custom_zone_XX` / rola `country-XX` / Kookeey).

## Clash Verge Notes

If you use Clash Verge or another local proxy client, create a local listener
in that client and point `proxy.default` at the listener port. The exact
profile file path is machine-specific and should not be committed.

Example listener shape:

```yaml
listeners:
  - name: checkout-exit
    type: mixed
    port: 7897
    proxy: Selected-Exit
```

Example routing rule shape:

```yaml
rules:
  - DOMAIN-SUFFIX,stripe.com,Selected-Exit
  - DOMAIN-SUFFIX,stripe.network,Selected-Exit
  - DOMAIN-SUFFIX,openai.com,Selected-Exit
  - DOMAIN-SUFFIX,chatgpt.com,Selected-Exit
```

## Runtime Boundary

- `config.example.json` is committed and contains only placeholders; shards
  (`proxy.json` etc.) are local-only and may contain credentials.
- Backup snapshots (`proxy.json.bak-*`) are gitignored by design — they hold
  credentials.
- WPF uses the same backend scripts and does not store proxy settings in code;
  the desktop proxy inputs are normalized by `ProxyInputNormalizer.cs`, whose
  output `proxy_entry.parse_proxy` must accept (pinned by
  `tests/fixtures/proxy_input_cases.json`).

## Unified Proxy Registry

Three egress lanes used to resolve independently:

| Lane | Resolver | Default config keys |
| --- | --- | --- |
| registration | `proxy_routing.proxy_pool_for` | `proxy.registration` / `proxy.pool` / `proxy.default` |
| mailbox / OTP | `mailbox._mailbox_proxy_candidates` | `mailbox_proxy` / `mailbox_proxy_pool` |
| payment | `payment_routing` + per-method sections | `protocol_payments.proxy_pools`, `<method>.stage_proxies` |

`sms_tool/proxy/registry.py` is the single surface over all three. It does not
replace the resolvers (their differences are deliberate, Rule 19) — it fronts
them with one vocabulary (`registration` / `mailbox` / `payment`), one resolve
call, and one credential-free census. The census also reports
`payment_qualification` (its own resolve, outside `LANES`), so the exit that
gates the read-only qualification batch is visible without reading
`payment_qualification.proxy_pool` by hand.

```powershell
python scripts/proxy_census.py                       # text table
python scripts/proxy_census.py --json                # machine-readable
python scripts/proxy_census.py --methods paypal,upi,kakao,momo
```

### Optional single declaration point

When `proxy.lanes` is present it is **authoritative** for the matching lane;
when absent (the default) every resolver uses its legacy keys unchanged, so
existing shards keep working byte-for-byte.

```jsonc
"proxy": {
  "lanes": {
    "protocol_registration": ["http://user:pass@host:port", ...],
    "browser_registration":  ["..."],
    "mailbox":               ["http://127.0.0.1:7897"],
    "payment_qualification": ["http://stripe-reachable.example:8080"],
    "payment": {
      "pools":   { "US": ["..."], "JP": ["..."] },
      "default": ["..."]
    }
  }
}
```

- `protocol_registration` / `browser_registration` are honoured by
  `proxy_pool_for` (registration batch, health and promotion probes).
- `mailbox` is honoured by `mailbox._mailbox_proxy_candidates` (the OTP poller).
- `payment_qualification` is the dedicated pool for the read-only payment
  qualification probe (`payment_qualification.proxy_pool` is accepted as the
  scalar/list form). It must be Stripe-reachable, not merely ChatGPT-reachable.
  `validate_config` accepts this lane; before 2026-10-27 it did not, so the
  desktop's 支付资格专用代理池 field wrote a lane the backend then rejected at
  load time.
- `payment` region pools are honoured by `PaymentRoutePlanner._named_pools`.
- `proxy.lanes.payment.pools` keys match the names referenced by
  `protocol_payments.methods.<m>.stage_routes` / `stage_proxy_pools`.
- Credentials are never printed by the census or the registry.

**Desktop alignment (2026-10-27).** 设置 → 基础网络 now edits
`protocol_registration`, `browser_registration`, `mailbox` and
`payment_qualification` directly. Opening the page reads the lane first; a
non-empty save writes `proxy.lanes.<lane>.pool` (creating the object when
absent) and mirrors the legacy keys; a blank save leaves the lane untouched.
`payment` remains owned by the protocol-payment dialog and has no field on this
page.

### Lane isolation

The census compares **full credentials** (distinct sticky sessions on one
endpoint are distinct exits) and reports any payment/mailbox credential that is
also in the registration pool: `lane isolation: OK` or `CROSS-LANE OVERLAP`.
Reusing a registration exit for payment/OTP is a documented violation, not a
style point — `payment.json`'s UPI stage proxies were pointed at a registration
pool entry until 2026-09-28 and now use the payment lane (`global.9http.com`).

`proxy/registry.lane_isolation()` is the narrow form of the same check (mailbox
and `payment_qualification` only, no payment-method catalogue, no network), so
the desktop ProxyPool window shows it as part of a plain listing instead of
requiring `scripts/proxy_census.py`. It separates **conflicts** from **declared
overlaps**:

```jsonc
"proxy": {
  "lanes": { "payment_qualification": ["http://same-pool.example:8080"] },
  "allow_lane_overlap": ["payment_qualification"]
}
```

An acknowledged lane stops counting as a conflict (`lane_isolation.ok` becomes
true) but stays in `overlaps` and still prints in the census with a
`[declared in proxy.allow_lane_overlap]` marker, so the decision remains
reviewable. Unknown lane names are rejected by `validate_config` — the reader
ignores names it does not recognise, so a typo would look like a declaration
while the census kept warning.

**UPI egress verified (2026-09-28)**: `upi.checkout_country` / `payment_country`
/ `target_country` are all `IN` and `billing_regions=["IN"]` (fallback only), so
`_upi_retarget_region` rewrites the payment credential to `geo-IN`. Probed
through the proxy: `geo-IN` → `ok=True country=IN`; the same credential with
`geo-JP` → `country_mismatch:JP`. So the UPI payment lane now genuinely exits
from India **and** shares no credential with the registration pool. Note the
`native_upi` adapter is a function adapter and does *not* pass through
`payment_egress.assert_egress_countries`, so this probe was run by hand — re-run
it after any payment-pool change.

### Pruning orphaned health rows

Swapping a pool leaves the old endpoints' health rows behind forever (the
tracker only ever appends). Keep only the endpoints the active pools resolve to
today (dry-run by default):

```powershell
python scripts/proxy_health_prune.py            # show orphaned endpoints
python scripts/proxy_health_prune.py --apply    # rewrite under the tracker lock
```

A partial or invalid config degrades the census to what it can resolve (legacy
mailbox resolution validates the whole config, so a census on a partial file
returns an empty mailbox pool rather than raising).
