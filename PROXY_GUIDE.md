# Proxy Guide

This project reads proxy settings from the local `config.json`. Keep real proxy
ports, provider names, and local client profile paths out of Git. Start from
`config.example.json`, then edit `config.json` on each machine.

## Recommended Local Setup

1. Copy the sample config:

```powershell
Copy-Item config.example.json config.json
```

2. Configure the default proxy for registration and checkout requests:

```json
{
  "proxy": {
    "default": "socks5h://127.0.0.1:7897"
  }
}
```

3. Configure PayPal/Stripe stage routing if the environment needs different
   exits for different stages:

```json
{
  "paypal": {
    "proxies": ["socks5h://127.0.0.1:7897"],
    "stage_proxies": {
      "checkout": "socks5h://127.0.0.1:7897",
      "stripe_init": "socks5h://127.0.0.1:7897",
      "payment_method": "socks5h://127.0.0.1:7897",
      "confirm": "direct"
    }
  }
}
```

`direct` means no proxy for that stage. Empty strings are ignored.

## Clash Verge Notes

If you use Clash Verge or another local proxy client, create a local listener in
that client and point `config.json` at the listener port. The exact profile file
path is machine-specific and should not be committed.

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

## Verification

Check the configured PayPal proxy without running a real checkout:

```powershell
python -m sms_tool.gen_pp_link --dry-run
```

Check a local SOCKS5 exit manually:

```powershell
curl.exe --proxy socks5h://127.0.0.1:7897 https://ipinfo.io/json
```

If the local listener is not reachable, fix the proxy client first. Retrying the
registration or PayPal flow will not repair a missing local listener.

## Runtime Boundary

- `config.example.json` is committed and contains only placeholders.
- `config.json` is local-only and may contain real proxy ports or credentials.
- `sms_tool/gen_pp_link.py` reads proxy settings from the project-root
  `config.json`.
- WPF uses the same backend scripts and does not store proxy settings in code.
