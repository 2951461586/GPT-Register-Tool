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

`sms_tool/proxy_registry.py` is the single surface over all three. It does not
replace the resolvers (their differences are deliberate, Rule 19) — it fronts
them with one vocabulary (`registration` / `mailbox` / `payment`), one resolve
call, and one credential-free census:

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
- `payment` region pools are honoured by `PaymentRoutePlanner._named_pools`.
- `proxy.lanes.payment.pools` keys match the names referenced by
  `protocol_payments.methods.<m>.stage_routes` / `stage_proxy_pools`.
- Credentials are never printed by the census or the registry.

### Lane isolation

The census compares **full credentials** (distinct sticky sessions on one
endpoint are distinct exits) and reports any payment/mailbox credential that is
also in the registration pool: `lane isolation: OK` or `CROSS-LANE OVERLAP`.
Reusing a registration exit for payment/OTP is a documented violation, not a
style point — `payment.json`'s UPI stage proxies were pointed at a registration
pool entry until 2026-09-28 and now use the payment lane (`global.9http.com`).

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
