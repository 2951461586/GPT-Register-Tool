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

## SOCKS5 Pool Server

`start_proxy_pool.py` serves a local SOCKS5 endpoint that rotates over a pool
of upstreams with health checks:

```powershell
# Upstreams from the proxy shard (proxy.pool), SOCKS5 entries only:
python start_proxy_pool.py

# Or explicit upstreams:
python start_proxy_pool.py --upstreams "socks5://127.0.0.1:7897,socks5://127.0.0.1:17912"
```

Without `--upstreams` or `proxy.pool` SOCKS5 entries the server exits with an
error. The historical silent fallback to `socks5://127.0.0.1:7897` (via the
`config.json proxy_pool.upstreams` key that never existed) was removed on
purpose: a pool that quietly serves your local clash listener instead of the
configured provider is worse than a loud failure.

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
