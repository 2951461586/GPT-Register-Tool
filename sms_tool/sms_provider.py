import json
import time
from dataclasses import dataclass

from curl_cffi import requests as curl_requests

from .config import CFG

# ==========================================
# SMS Provider (SMS-Activate compatible API)
# ==========================================
@dataclass(frozen=True)
class SMSProvider:
    name: str
    api_key: str
    base_url: str


SMS_PROVIDERS = {
    "herosms": "https://hero-sms.com/stubs/handler_api.php",
    "smsbower": "https://smsbower.page/stubs/handler_api.php",
}


def _phone_sms_cfg():
    return CFG.get("phone_sms", {})


def _resolve_sms_provider(provider_name=None):
    cfg = _phone_sms_cfg()
    name = (provider_name or cfg.get("provider") or "herosms").strip().lower()
    if name not in SMS_PROVIDERS:
        raise ValueError(f"unsupported sms provider: {name}")
    key = (
        cfg.get(f"{name}_api_key")
        or cfg.get("api_key")
        or (cfg.get("herosms_api_key") if name == "herosms" else "")
        or (cfg.get("smsbower_api_key") if name == "smsbower" else "")
        or ""
    ).strip()
    base_url = (cfg.get(f"{name}_base_url") or SMS_PROVIDERS[name]).strip()
    return SMSProvider(name=name, api_key=key, base_url=base_url)


def _sms_call(provider, action, **params):
    p = {"api_key": provider.api_key, "action": action, **params}
    try:
        r = curl_requests.get(provider.base_url, params=p, impersonate="chrome", timeout=30)
        return r.text.strip()
    except Exception as e:
        print(f"[{provider.name} error: {e}]")
        return ""


def _sms_balance(provider):
    resp = _sms_call(provider, "getBalance")
    print(f"[*] {provider.name} Balance: {resp}")
    return resp


def _sms_get_prices(provider, service="dr", country=None):
    params = {"service": service}
    if country:
        params["country"] = country
    resp = _sms_call(provider, "getPrices", **params)
    if not resp: return None
    try: return json.loads(resp)
    except: return None


def _sms_pick_best_country(provider, service="dr", preferred=None, max_price=None, min_price=None):
    """Pick best country based on configurable price range and blocked countries."""
    cfg = _phone_sms_cfg()
    if max_price is None: max_price = cfg.get("max_price", 0.08)
    if min_price is None: min_price = cfg.get("min_price", 0.04)
    blocked = set(str(v) for v in cfg.get("blocked_countries", []))
    prices = _sms_get_prices(provider, service=service)
    if not prices:
        return preferred or "6", None

    candidates = []
    for country_code, operators in prices.items():
        if str(country_code) in blocked: continue
        if not isinstance(operators, dict): continue
        for op_code, info in operators.items():
            if not isinstance(info, dict): continue
            cost = float(info.get("cost", info.get("price", 999)))
            cnt = int(info.get("count", 0))
            if cost <= max_price and cnt > 0:
                candidates.append((cost, str(country_code), op_code, cnt))

    if not candidates:
        print(f"[*] No countries under ${max_price}")
        return preferred or "6", None

    candidates.sort(key=lambda x: x[0])
    reliable = [c for c in candidates if c[0] >= min_price]
    if reliable: candidates = reliable
    else: print(f"[*] No numbers >= ${min_price}, using all available")

    if preferred:
        pc = [c for c in candidates if c[1] == preferred]
        if pc:
            best = pc[0]
            print(f"[*] Best: country={best[1]} price=${best[0]:.4f} count={best[3]} (preferred)")
            return best[1], best[2]

    best = candidates[0]
    print(f"[*] Best: country={best[1]} price=${best[0]:.4f} count={best[3]}")
    return best[1], best[2]


def _sms_get_number(provider, service="dr", country=None):
    cfg = _phone_sms_cfg()
    params = {"service": service}
    if country: params["country"] = country
    if provider.name == "smsbower":
        if cfg.get("max_price") is not None: params["maxPrice"] = cfg.get("max_price")
        if cfg.get("min_price") is not None: params["minPrice"] = cfg.get("min_price")
        for key in ("providerIds", "exceptProviderIds", "phoneException", "ref", "userID"):
            if cfg.get(key): params[key] = cfg[key]
    resp = _sms_call(provider, "getNumber", **params)
    print(f"[*] {provider.name} getNumber response: {resp}")
    if resp.startswith("ACCESS_NUMBER:"):
        parts = resp.split(":")
        if len(parts) >= 3:
            return parts[1], parts[2]
    return None, None


def _sms_get_status(provider, activation_id):
    resp = _sms_call(provider, "getStatus", id=activation_id)
    if resp.startswith("STATUS_OK:"):
        parts = resp.split(":")
        return "OK", parts[1] if len(parts) > 1 else ""
    elif resp.startswith("STATUS_WAIT_CODE"): return "WAIT_CODE", ""
    elif resp.startswith("STATUS_WAIT_RETRY"): return "WAIT_RETRY", ""
    elif resp.startswith("STATUS_WAIT_RESEND"): return "WAIT_RESEND", ""
    elif resp.startswith("STATUS_CANCEL"): return "CANCEL", ""
    return "UNKNOWN", resp


def _sms_set_status(provider, activation_id, status):
    resp = _sms_call(provider, "setStatus", id=activation_id, status=str(status))
    print(f"[*] setStatus({status}): {resp}")
    return resp


def _sms_poll(provider, activation_id, timeout=300):
    """Poll for SMS code. Does NOT auto-complete."""
    interval = 3
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        status, code = _sms_get_status(provider, activation_id)
        print(".", end="", flush=True)
        if status == "OK" and code:
            print(f" code:{code}!")
            return code
        elif status in ("CANCEL",):
            print(f" [{status}]")
            return None
    print(" timeout")
    return None

