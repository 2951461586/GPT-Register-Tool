"""Dynamic proxy helpers for phone registration flows.

The phone-registration path must not buy SMS activations until the selected
OpenAI/auth proxy is known to be usable.  This module keeps the provider
formatting rules in one place: match ``region-XX`` to the phone country, refresh
sticky ``sid-`` values per attempt, auto-detect HTTP vs SOCKS proxy schemes, and
cache short-lived probe results.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from .config import CFG
from .paths import runtime_file

DEFAULT_PROBE_TTL_SECONDS = 300
DEFAULT_PROBE_TIMEOUT_SECONDS = 12

# SMS-Activate/SMSBower common country ids used by this project and the
# standalone phone protocol.  Unknown ids fall back to configured country_name
# or an already-ISO value.
COUNTRY_ID_TO_ISO = {
    "1": "UA",
    "2": "KZ",
    "4": "PH",
    "6": "ID",
    "12": "US",
    "16": "GB",
    "25": "LA",
    "31": "ZA",
    "33": "CO",
    "38": "GH",
    "39": "AR",
    "41": "CM",
    "66": "PK",
    "73": "BR",
    "117": "PT",
    "151": "CL",
}

COUNTRY_NAME_TO_ISO = {
    "argentina": "AR",
    "brazil": "BR",
    "cameroon": "CM",
    "chile": "CL",
    "colombia": "CO",
    "ghana": "GH",
    "indonesia": "ID",
    "japan": "JP",
    "kazakhstan": "KZ",
    "laos": "LA",
    "pakistan": "PK",
    "philippines": "PH",
    "portugal": "PT",
    "south africa": "ZA",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "usa": "US",
}

_TRANSIENT_CACHE: dict[str, dict[str, Any]] = {}
# Guards ``_TRANSIENT_CACHE`` and the on-disk probe cache file.  Batch
# registration probes proxies from several worker threads concurrently, so the
# in-memory dict and the file write must be serialized.
_CACHE_LOCK = threading.RLock()


def phone_proxy_cfg() -> dict:
    cfg = CFG.get("phone_reuse") if isinstance(CFG.get("phone_reuse"), dict) else {}
    nested = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    merged = dict(nested)
    for key in (
        "proxy",
        "proxies",
        "proxy_template",
        "proxy_api_url",
        "white_api_url",
        "api_url",
        "proxy_match_phone_country",
        "proxy_random_sid",
        "proxy_probe_cache_ttl_seconds",
        "proxy_probe_timeout_seconds",
        "proxy_fallback_regions",
        "proxy_stop_on_unavailable",
    ):
        if key in cfg and key not in merged:
            merged[key] = cfg[key]
    return merged


def normalize_proxy_url(proxy: str, default_scheme: str = "http") -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    # Delegate to the unified, single-authority parser first; fall back to the
    # historical permissive logic (e.g. http proxy without a port) when the
    # strict parser refuses (returns None) so behaviour stays backward-compatible.
    from .proxy_entry import parse_proxy, proxy_to_url

    entry = parse_proxy(value, default_scheme=default_scheme)
    if entry is not None:
        return proxy_to_url(entry)
    scheme = ""
    rest = value
    if "://" in value:
        scheme, rest = value.split("://", 1)
    parts = rest.split(":")
    # Provider UI often emits host:port:user:pass.  Convert it to the standard
    # URL form expected by requests/curl_cffi/Playwright.
    if "@" not in rest and len(parts) == 4 and "." in parts[0] and parts[1].isdigit():
        host, port, user, password = parts
        return f"{scheme or default_scheme}://{quote(user, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"
    if not scheme:
        return f"{default_scheme}://{rest}"
    return value


def redact_proxy_url(proxy: str | None, empty_placeholder: str = "DIRECT") -> str:
    """Return a log-safe proxy URL with credentials replaced by ``***:***``.

    ``empty_placeholder`` lets callers signal a missing proxy (``"DIRECT"`` for
    paypal-proxy paths, ``""`` for phone-registration paths).
    """
    value = normalize_proxy_url(proxy)
    if not value:
        return empty_placeholder
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not host:
            return "***"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        auth = "***:***@" if parsed.username is not None else ""
        return urlunsplit((parsed.scheme or "http", f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return "***"


def redact_proxy_text(value: Any, proxy: str | None = None) -> str:
    """Redact proxy-credential substrings from an arbitrary text."""
    text = str(value or "")
    raw = str(proxy or "").strip()
    normalized = normalize_proxy_url(proxy)
    if normalized:
        redacted_canonical = redact_proxy_url(normalized, empty_placeholder="")
        if raw:
            text = text.replace(raw, redacted_canonical)
        # Replace both the exact normalized form and common suffix variants
        # (trailing slash / ?query) that log paths usually carry.
        text = text.replace(normalized, redacted_canonical)
        if normalized.endswith("/"):
            text = text.replace(normalized[:-1], redacted_canonical)
    # Embedded ``protocol://user:pass@host`` patterns — keep scheme so the
    # result is still recognisable as a proxy URL.
    text = re.sub(
        r"(?i)\b((?:https?|socks5h?)://)[^/@\s]+@",
        r"\1***:***@",
        text,
    )
    # ``host:port:user:pass`` form (4 colon-separated fields) — keep ``host:port``.
    text = re.sub(
        r"(?i)\b([a-z0-9.\-]+:\d+):[^\s'\"]+:[^\s'\"]+",
        r"\1:***:***",
        text,
    )
    return text


def _rebuild_proxy_url(parsed: Any, username: str, password: str) -> str:
    from .proxy_entry import rebuild_proxy_credentials

    return rebuild_proxy_credentials(parsed, username, password)


def match_proxy_region(proxy: str, iso_code: str) -> str:
    """Retarget the exit region while preserving the sticky session id.

    Thin wrapper over the single-authority ``proxy_entry.retarget_region`` after
    normalizing to canonical URL form.
    """
    from .proxy_entry import retarget_region

    return retarget_region(normalize_proxy_url(proxy), iso_code)


def refresh_proxy_sid(proxy: str) -> str:
    """Refresh the sticky session id (region unchanged).

    Thin wrapper over the single-authority ``proxy_entry.rotate_session``.
    """
    from .proxy_entry import rotate_session

    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return ""
    return rotate_session(normalized, "")


def phone_country_iso(country: Any = "", provider: str = "", cfg: dict | None = None) -> str:
    value = str(country or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return value.upper()
    if value in COUNTRY_ID_TO_ISO:
        return COUNTRY_ID_TO_ISO[value]
    cfg = cfg if isinstance(cfg, dict) else {}
    for key in ("country_iso", "iso", "country_code"):
        raw = str(cfg.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z]{2}", raw):
            return raw.upper()
    name = str(cfg.get("country_name") or "").strip().lower()
    return COUNTRY_NAME_TO_ISO.get(name, "")


def _configured_proxy_api_url() -> str:
    pcfg = phone_proxy_cfg()
    for key in ("api_url", "proxy_api_url", "white_api_url"):
        value = str(pcfg.get(key) or "").strip()
        if value:
            return value
    cfg = CFG.get("phone_reuse") if isinstance(CFG.get("phone_reuse"), dict) else {}
    for key in ("proxy_api_url", "white_api_url"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return ""


def _api_url_for_region(api_url: str, country_iso: str) -> str:
    api_url = str(api_url or "").strip()
    region = str(country_iso or "").strip().upper()
    if not api_url or not region:
        return api_url
    parsed = urlsplit(api_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "region" in query:
        query["region"] = region
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    # Also support template-style URLs.
    return api_url.replace("{region}", region).replace("{REGION}", region)


def fetch_proxy_from_api(api_url: str, country_iso: str = "") -> dict:
    url = _api_url_for_region(api_url, country_iso)
    if not url:
        return {"ok": False, "error": "proxy_api_url_missing"}
    timeout = _probe_timeout_seconds()
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 CodexPhoneProxyAPI/1.0"})
        response.raise_for_status()
        text = response.text.strip()
    except Exception as exc:
        return {"ok": False, "api_url": url, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        proxy = normalize_proxy_url(line)
        return {"ok": True, "api_url": url, "proxy": proxy, "raw": line}
    return {"ok": False, "api_url": url, "error": "proxy_api_empty_response", "body": text[:240]}


def configured_base_proxy(explicit_proxy: str | None = None) -> str:
    if explicit_proxy:
        return normalize_proxy_url(str(explicit_proxy).strip())
    pcfg = phone_proxy_cfg()
    for key in ("proxy", "proxy_template"):
        value = str(pcfg.get(key) or "").strip()
        if value:
            return normalize_proxy_url(value)
    proxies = pcfg.get("proxies") or []
    if isinstance(proxies, str):
        proxies = [item.strip() for item in proxies.split(",") if item.strip()]
    if proxies:
        return normalize_proxy_url(str(proxies[0]).strip())
    default_proxy = ((CFG.get("proxy") or {}).get("default") or "").strip()
    return normalize_proxy_url(default_proxy) if default_proxy else ""


def build_phone_proxy(base_proxy: str, country_iso: str = "", *, refresh_sid: bool = True, match_region: bool = True) -> str:
    proxy = normalize_proxy_url(base_proxy)
    if match_region and country_iso:
        proxy = match_proxy_region(proxy, country_iso)
    if refresh_sid:
        proxy = refresh_proxy_sid(proxy)
    return proxy


def _cache_path() -> Path:
    return runtime_file(CFG, "phone_proxy_probe_cache.json")


def _cache_ttl_seconds() -> int:
    pcfg = phone_proxy_cfg()
    try:
        return max(0, int(pcfg.get("proxy_probe_cache_ttl_seconds", DEFAULT_PROBE_TTL_SECONDS) or 0))
    except Exception:
        return DEFAULT_PROBE_TTL_SECONDS


def _load_probe_cache() -> dict:
    path = _cache_path()
    with _CACHE_LOCK:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _TRANSIENT_CACHE.update(data)
            except Exception:
                pass
        return dict(_TRANSIENT_CACHE)


def _save_probe_cache() -> None:
    path = _cache_path()
    with _CACHE_LOCK:
        temp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(_TRANSIENT_CACHE, ensure_ascii=False, indent=2)
            # Atomic replace so concurrent worker probes never observe or leave a
            # half-written cache file.  The temp name is unique per process+call
            # to avoid collisions even if the lock is ever bypassed.
            temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, path)
        except Exception:
            if temp is not None:
                try:
                    temp.unlink()
                except Exception:
                    pass


def _probe_timeout_seconds() -> int:
    pcfg = phone_proxy_cfg()
    try:
        return max(3, int(pcfg.get("proxy_probe_timeout_seconds", DEFAULT_PROBE_TIMEOUT_SECONDS) or DEFAULT_PROBE_TIMEOUT_SECONDS))
    except Exception:
        return DEFAULT_PROBE_TIMEOUT_SECONDS


def _probe_proxy_live(proxy: str, expected_country: str = "") -> dict:
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy}
    timeout = _probe_timeout_seconds()
    response = session.get(
        "http://ip-api.com/json?fields=status,message,country,countryCode,query",
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 CodexPhoneProxyProbe/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") and data.get("status") != "success":
        raise RuntimeError(str(data.get("message") or data))
    country_code = str(data.get("countryCode") or "").upper()
    if expected_country and country_code and country_code != expected_country.upper():
        raise RuntimeError(f"exit_country_mismatch:{country_code}!={expected_country.upper()}")
    return {"ok": True, "ip": data.get("query", ""), "country_code": country_code, "country": data.get("country", "")}


def probe_proxy(proxy: str, expected_country: str = "", *, use_cache: bool = True) -> dict:
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return {"ok": True, "proxy": "", "scheme": "direct", "cache": False}
    key = json.dumps({"proxy": proxy, "expected": str(expected_country or "").upper()}, sort_keys=True)
    now = time.time()
    ttl = _cache_ttl_seconds()
    if use_cache and ttl > 0:
        cache = _load_probe_cache()
        cached = cache.get(key)
        if isinstance(cached, dict) and now - float(cached.get("ts") or 0) <= ttl:
            result = dict(cached.get("result") or {})
            result["cache"] = True
            return result
    try:
        live = _probe_proxy_live(proxy, expected_country)
        result = {**live, "proxy": proxy, "scheme": urlsplit(proxy).scheme, "cache": False}
    except Exception as exc:
        result = {"ok": False, "proxy": proxy, "scheme": urlsplit(proxy).scheme, "error": f"{type(exc).__name__}: {str(exc)[:240]}", "cache": False}
    if ttl > 0:
        with _CACHE_LOCK:
            _TRANSIENT_CACHE[key] = {"ts": now, "result": result}
            _save_probe_cache()
    return result


def _best_proxy_probe_error(attempts: list[dict]) -> str:
    if not attempts:
        return "proxy_probe_failed"
    for item in attempts:
        error = str(item.get("error") or "")
        if "forbidden ip" in error.lower() or "403" in error:
            return error
    for item in attempts:
        error = str(item.get("error") or "")
        if error:
            return error
    return "proxy_probe_failed"


def probe_proxy_with_scheme_detection(proxy: str, expected_country: str = "", *, use_cache: bool = True) -> dict:
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return {"ok": True, "proxy": "", "scheme": "direct"}
    candidates = [proxy]
    if proxy.startswith(("socks5h://", "socks5://")):
        candidates.append(proxy.replace("socks5h://", "http://", 1).replace("socks5://", "http://", 1))
    elif proxy.startswith("http://"):
        candidates.append(proxy.replace("http://", "socks5h://", 1))
    attempts = []
    for candidate in dict.fromkeys(candidates):
        result = probe_proxy(candidate, expected_country, use_cache=use_cache)
        attempts.append(result)
        if result.get("ok"):
            result["attempts"] = attempts
            return result
    return {
        "ok": False,
        "proxy": candidates[0],
        "scheme": urlsplit(candidates[0]).scheme,
        "attempts": attempts,
        "error": _best_proxy_probe_error(attempts),
    }


def _region_candidates(primary_iso: str, base_proxy: str) -> list[str]:
    primary = str(primary_iso or "").strip().upper()
    pcfg = phone_proxy_cfg()
    configured = pcfg.get("proxy_fallback_regions") or []
    if isinstance(configured, str):
        configured = [item.strip().upper() for item in configured.split(",") if item.strip()]
    found = re.search(r"region-([A-Za-z]{2})", base_proxy or "")
    original = found.group(1).upper() if found else ""
    result = []
    for item in [primary, *configured, original]:
        if item and item not in result:
            result.append(item)
    return result or [""]


def select_phone_proxy(
    explicit_proxy: str | None = None,
    country: Any = "",
    provider: str = "",
    country_cfg: dict | None = None,
    *,
    refresh_sid: bool = True,
    use_cache: bool = True,
    probe: bool = True,
) -> dict:
    pcfg = phone_proxy_cfg()
    country_iso = phone_country_iso(country, provider=provider, cfg=country_cfg)
    api_fetch = {}
    api_url = "" if explicit_proxy else _configured_proxy_api_url()
    if api_url:
        api_fetch = fetch_proxy_from_api(api_url, country_iso)
        base = str(api_fetch.get("proxy") or "") if api_fetch.get("ok") else ""
        if not base:
            return {
                "ok": False,
                "proxy": "",
                "base_proxy": "",
                "country_iso": country_iso,
                "attempts": [],
                "error": api_fetch.get("error", "proxy_api_failed"),
                "api": api_fetch,
            }
    else:
        base = configured_base_proxy(explicit_proxy)
    if not base:
        return {"ok": True, "proxy": "", "base_proxy": "", "country_iso": country_iso, "attempts": [], "direct": True}
    match_region = bool(pcfg.get("proxy_match_phone_country", True))
    dynamic_session = bool(
        re.search(r"-sid-[A-Za-z0-9]+(?=-t-|-|:|@|$)", base)
        or re.search(r"-[A-Za-z]{2}-[A-Za-z0-9]+-(?:\d+m|\d+h)(?=[:@/]|$)", base)
    )
    random_sid = refresh_sid and (bool(pcfg.get("proxy_random_sid", True)) or dynamic_session)
    attempts = []
    for region in _region_candidates(country_iso, base):
        candidate = build_phone_proxy(base, region, refresh_sid=random_sid, match_region=match_region)
        if not probe:
            return {"ok": True, "proxy": candidate, "base_proxy": base, "country_iso": country_iso, "region": region, "attempts": []}
        checked = probe_proxy_with_scheme_detection(candidate, region if match_region and region else "", use_cache=use_cache)
        checked["region"] = region
        attempts.append(checked)
        if checked.get("ok"):
            return {
                "ok": True,
                "proxy": checked.get("proxy") or candidate,
                "base_proxy": base,
                "country_iso": country_iso,
                "region": region,
                "attempts": attempts,
                "ip": checked.get("ip", ""),
                "country_code": checked.get("country_code", ""),
                "api": api_fetch,
            }
    return {
        "ok": False,
        "proxy": "",
        "base_proxy": base,
        "country_iso": country_iso,
        "attempts": attempts,
        "error": attempts[-1].get("error", "phone_proxy_unavailable") if attempts else "phone_proxy_unavailable",
        "api": api_fetch,
    }


def apply_proxy_to_session(session: Any, proxy: str) -> None:
    if not session or not proxy:
        return
    try:
        session.proxies = {"http": proxy, "https": proxy}
    except Exception:
        pass


def looks_like_proxy_error(error: Any) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "proxyerror",
            "proxy error",
            "unable to connect to proxy",
            "tunnel connection failed",
            "forbidden ip",
            "socks",
            "connecttimeout",
            "connection refused",
            "failed to establish a new connection",
        )
    )
