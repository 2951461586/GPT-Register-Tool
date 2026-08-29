"""Browser fingerprint pool for the headless-browser registration path.

This module is the browser-path counterpart of :mod:`fingerprint_pool`
(which only serves the protocol / curl_cffi registration path).  It mirrors
the approach used by ``turb-gpt-free-register`` (``config/browser.py`` +
``core/session.py``):

1. **Browser profile rotation pool** (``BROWSER_PROFILE_POOL``) — a list of
   desktop browser hardware profiles (screen, hardware_concurrency,
   device_memory, …).  Each registration draws one profile, either round-robin
   or deterministically seeded by the account ``device_id`` so the same
   account reproduces the same profile on relogin.
2. **Exit-IP geo detection** (``detect_proxy_exit_geo``) — queries public IP
   geo endpoints *through the registration proxy* and derives the real egress
   country + IANA timezone.  Result is cached per proxy and degrades to ``{}``
   on any failure so registration never blocks on it.
3. **Geo-consistent locale / timezone** — the detected country selects a
   ``BROWSER_LOCALE_PROFILE`` (language + accept-language + timezone) which is
   then aligned to the proxy egress.  ``build_browser_environment`` returns the
   merged, single-source-of-truth fingerprint that ``run_browser_registration``
   injects into the browser session (locale + timezone_id) and records on the
   account identity.

The protocol path already aligns its TLS fingerprint geo via
``set_fingerprint_geo(infer_proxy_country(s.proxy))``.  This module gives the
browser path the same geo-consistency plus per-account profile rotation, so a
headless registration no longer silently defaults to one fixed environment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - only on very old interpreters
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chrome desktop reference UA (used only as the geo-probe request header).
# ---------------------------------------------------------------------------
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Geo detection config (modeled on turb's AUTO_BROWSER_LOCALE_FROM_IP).
# ---------------------------------------------------------------------------
AUTO_BROWSER_LOCALE_FROM_IP = True
IP_GEO_TIMEOUT = 6.0
IP_GEO_ENDPOINTS = [
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
    "https://ipwho.is/",
]

# Country (ISO-3166 alpha-2) -> locale profile key.
COUNTRY_LOCALE_PROFILE_MAP = {
    "JP": "jp", "CN": "cn", "HK": "hk", "TW": "tw", "US": "us", "CA": "us",
    "SG": "sg", "GB": "gb", "AU": "gb", "DE": "de", "FR": "fr", "NL": "nl",
}
DEFAULT_LOCALE_PROFILE = "us"

# Locale profile key -> browser language / accept-language / timezone.
BROWSER_LOCALE_PROFILES: dict[str, dict[str, Any]] = {
    "jp": {"navigator_language": "ja-JP", "navigator_languages": ["ja-JP"], "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Tokyo", "timezone_offset_minutes": 9 * 60, "timezone_name": "Japan Standard Time"},
    "cn": {"navigator_language": "zh-CN", "navigator_languages": ["zh-CN"], "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Shanghai", "timezone_offset_minutes": 8 * 60, "timezone_name": "China Standard Time"},
    "us": {"navigator_language": "en-US", "navigator_languages": ["en-US"], "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/Los_Angeles", "timezone_offset_minutes": -7 * 60, "timezone_name": "Pacific Daylight Time"},
    "sg": {"navigator_language": "en-SG", "navigator_languages": ["en-SG"], "accept_language": "en-SG,en-US;q=0.9,en;q=0.8", "timezone_iana": "Asia/Singapore", "timezone_offset_minutes": 8 * 60, "timezone_name": "Singapore Standard Time"},
    "hk": {"navigator_language": "zh-HK", "navigator_languages": ["zh-HK"], "accept_language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6", "timezone_iana": "Asia/Hong_Kong", "timezone_offset_minutes": 8 * 60, "timezone_name": "Hong Kong Standard Time"},
    "tw": {"navigator_language": "zh-TW", "navigator_languages": ["zh-TW"], "accept_language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Taipei", "timezone_offset_minutes": 8 * 60, "timezone_name": "Taipei Standard Time"},
    "gb": {"navigator_language": "en-GB", "navigator_languages": ["en-GB"], "accept_language": "en-GB,en-US;q=0.9,en;q=0.8", "timezone_iana": "Europe/London", "timezone_offset_minutes": 1 * 60, "timezone_name": "British Summer Time"},
    "de": {"navigator_language": "de-DE", "navigator_languages": ["de-DE"], "accept_language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Berlin", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "fr": {"navigator_language": "fr-FR", "navigator_languages": ["fr-FR"], "accept_language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Paris", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "nl": {"navigator_language": "nl-NL", "navigator_languages": ["nl-NL"], "accept_language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Amsterdam", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
}

TIMEZONE_NAME_BY_IANA = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Hong_Kong": "Hong Kong Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/Los_Angeles": "Pacific Daylight Time",
    "America/New_York": "Eastern Daylight Time",
    "America/Chicago": "Central Daylight Time",
    "America/Denver": "Mountain Daylight Time",
    "Europe/London": "British Summer Time",
    "Europe/Berlin": "Central European Summer Time",
    "Europe/Paris": "Central European Summer Time",
    "Europe/Amsterdam": "Central European Summer Time",
}

# Common macOS Chrome desktop hardware profiles.  One is drawn per registration
# (round-robin or seed-stable); the HAR capture baseline is only one candidate.
BROWSER_PROFILE_POOL = [
    {"screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 6, "device_memory": 8, "js_heap_size_limit": 4395630592, "device_pixel_ratio": 2},
    {"screen_width": 1440, "screen_height": 900, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1512, "screen_height": 982, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1728, "screen_height": 1117, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1800, "screen_height": 1169, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 2056, "screen_height": 1329, "hardware_concurrency": 12, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
]

# Stable label per pool index, so the chosen profile is human-readable in logs
# and in the persisted identity context.
BROWSER_PROFILE_LABELS = [
    "desktop-1680x1050-hw6",
    "desktop-1440x900-hw8",
    "desktop-1512x982-hw8",
    "desktop-1680x1050-hw8",
    "desktop-1728x1117-hw10",
    "desktop-1800x1169-hw10",
    "desktop-2056x1329-hw12",
]


def _offset_minutes_for_timezone(tz_name: str, default: int) -> int:
    if ZoneInfo is None:
        return int(default)
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 60)
    except Exception:
        pass
    return int(default)


def _label_for_index(index: int) -> str:
    if not BROWSER_PROFILE_LABELS:
        return "browser-profile-0"
    return BROWSER_PROFILE_LABELS[index % len(BROWSER_PROFILE_LABELS)]


def _stable_index(seed: str, n: int) -> int:
    """Deterministic pool index from a seed (e.g. device_id)."""
    if n <= 0:
        return 0
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % n


# ---------------------------------------------------------------------------
# Browser profile rotation pool
# ---------------------------------------------------------------------------
class BrowserProfilePool:
    """Thread-safe rotation pool of desktop browser hardware profiles."""

    def __init__(self, profiles: list[dict[str, Any]] | None = None) -> None:
        self._profiles = list(profiles) if profiles else list(BROWSER_PROFILE_POOL)
        self._index = 0
        self._lock = threading.Lock()

    def size(self) -> int:
        return len(self._profiles)

    def next(self) -> dict[str, Any]:
        """Round-robin: next profile (wraps)."""
        with self._lock:
            if not self._profiles:
                return dict(BROWSER_PROFILE_POOL[0])
            profile = dict(self._profiles[self._index % len(self._profiles)])
            self._index += 1
        profile["browser_profile_index"] = self._index - 1
        profile["browser_fingerprint_profile"] = _label_for_index(self._index - 1)
        return profile

    def select(self, seed: str | None = None) -> dict[str, Any]:
        """Deterministic pick by seed; falls back to round-robin if no seed."""
        if not seed:
            return self.next()
        idx = _stable_index(seed, len(self._profiles) or 1)
        profile = dict(self._profiles[idx % len(self._profiles)])
        profile["browser_profile_index"] = idx
        profile["browser_fingerprint_profile"] = _label_for_index(idx)
        return profile


_SHARED_POOLS: dict[str, BrowserProfilePool] = {}
_SHARED_POOLS_LOCK = threading.Lock()


def shared_browser_profile_pool(config: Mapping[str, Any] | None = None) -> BrowserProfilePool:
    """One process-lifetime pool (config keyed, for future per-config pools)."""
    key = "default"
    if isinstance(config, Mapping):
        reg = config.get("registration") if isinstance(config.get("registration"), Mapping) else {}
        fp = reg.get("browser_profile_pool") if isinstance(reg.get("browser_profile_pool"), Mapping) else {}
        key = json.dumps(fp if isinstance(fp, Mapping) else {}, sort_keys=True, default=str)
    with _SHARED_POOLS_LOCK:
        pool = _SHARED_POOLS.get(key)
        if pool is None:
            pool = BrowserProfilePool()
            _SHARED_POOLS[key] = pool
        return pool


# ---------------------------------------------------------------------------
# Exit-IP geo detection (mirrors turb's _detect_exit_geo)
# ---------------------------------------------------------------------------
_GEO_CACHE: dict[str, dict[str, Any]] = {}
_GEO_CACHE_LOCK = threading.Lock()


def _normalize_geo_response(data: Any) -> dict[str, Any]:
    """Normalize ipinfo / ipapi / ipwho.is JSON into a common shape."""
    if not isinstance(data, dict):
        return {}
    timezone = data.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or timezone.get("name")
    country = str(
        data.get("country") or data.get("country_code") or data.get("countryCode") or ""
    ).strip().upper()
    return {
        "ip": data.get("ip") or data.get("query"),
        "country": country,
        "region": data.get("region") or data.get("regionName"),
        "city": data.get("city"),
        "timezone": str(timezone or "").strip(),
        "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
    }


def _query_geo_endpoints(proxy: str, timeout: float) -> dict[str, Any]:
    """Query geo endpoints through the proxy; return first usable normalize() result."""
    last_err: str = ""
    # Prefer curl_cffi (handles socks5 + TLS impersonation); fall back to urllib
    # for plain http/https proxies.
    for url in IP_GEO_ENDPOINTS:
        try:
            from curl_cffi.requests import get as cffi_get

            resp = cffi_get(
                url,
                proxy=proxy or None,
                impersonate="chrome",
                timeout=timeout,
                headers={"User-Agent": CHROME_UA, "Accept": "application/json"},
            )
            if resp.status_code == 200:
                geo = _normalize_geo_response(resp.json())
                if geo.get("country") or geo.get("timezone"):
                    return geo
        except Exception as exc:  # curl_cffi missing, network error, non-200…
            last_err = f"{type(exc).__name__}: {exc}"
            continue
    # urllib fallback (http/https proxies only)
    try:
        import urllib.request

        handler = urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}
        )
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(
            IP_GEO_ENDPOINTS[0],
            headers={"User-Agent": CHROME_UA, "Accept": "application/json"},
        )
        with opener.open(req, timeout=timeout) as r:
            if r.status == 200:
                geo = _normalize_geo_response(json.loads(r.read().decode("utf-8", "replace")))
                if geo.get("country") or geo.get("timezone"):
                    return geo
    except Exception as exc:
        last_err = f"urllib:{type(exc).__name__}: {exc}"
    if last_err:
        # Swallow; caller treats empty geo as "fall back to configured locale".
        pass
    return {}


def detect_proxy_exit_geo(
    proxy: str | None,
    *,
    timeout: float = IP_GEO_TIMEOUT,
    enabled: bool = AUTO_BROWSER_LOCALE_FROM_IP,
) -> dict[str, Any]:
    """Detect the proxy's real exit geo. Never raises; returns ``{}`` on miss.

    Cached per proxy string so repeated registrations through the same egress
    don't re-probe.  Disabled (or direct/unproxied) calls return ``{}`` so the
    caller keeps using the configured locale/timezone.
    """
    if not enabled or not proxy:
        return {}
    # Normalize first: proxy pools are supplied in many shapes (including the
    # common ``host:port:user:pass`` form).  curl_cffi / urllib only understand
    # the canonical ``scheme://user:pass@host:port`` URL, so without this step
    # geo detection silently fails and the fingerprint falls back to the
    # default US locale — defeating the whole geo-alignment optimization.
    from .phone_proxy import normalize_proxy_url
    norm = normalize_proxy_url(proxy) or str(proxy)
    cache_key = norm
    with _GEO_CACHE_LOCK:
        cached = _GEO_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
    geo: dict[str, Any] = {}
    try:
        geo = _query_geo_endpoints(cache_key, float(timeout))
    except Exception:
        geo = {}
    with _GEO_CACHE_LOCK:
        _GEO_CACHE[cache_key] = dict(geo)
    return dict(geo)


# ---------------------------------------------------------------------------
# Cloud / datacenter ASN detection (mirrors turb's REJECT_CLOUD_PROXY)
# ---------------------------------------------------------------------------
# Opt-in only.  A user may legitimately pin a fixed cloud egress to reproduce a
# captured HAR, so the default must never reject anything — turb ships the same
# default (``REJECT_CLOUD_PROXY = False``).
REJECT_CLOUD_PROXY = False

# Keyword list taken from turb-gpt-free-register ``config/browser.py``.  Kept
# verbatim for traceability.  The generic tails ("hosting", "server", "cloud")
# can false-positive on some ISP org strings, which is acceptable only because
# this is diagnostic: enabling it logs a warning, it never blocks a run.
CLOUD_PROXY_ORG_KEYWORDS = [
    "amazon", "aws", "google cloud", "google llc", "microsoft", "azure",
    "digitalocean", "linode", "akamai", "ovh", "hetzner", "oracle",
    "tencent", "alibaba", "aliyun", "huawei cloud", "vultr", "contabo",
    "data center", "datacenter", "hosting", "server", "cloud",
]


def classify_proxy_org(value: Any) -> str:
    """Classify an egress org as ``cloud`` / ``residential`` / ``unknown``.

    Accepts either a full geo mapping (as returned by
    :func:`detect_proxy_exit_geo`) or a bare org string.
    """
    if isinstance(value, Mapping):
        org = str(value.get("org") or "")
    else:
        org = str(value or "")
    org = org.strip().lower()
    if not org:
        return "unknown"
    if any(keyword in org for keyword in CLOUD_PROXY_ORG_KEYWORDS):
        return "cloud"
    return "residential"


def is_cloud_proxy(geo: Any, *, enabled: bool = REJECT_CLOUD_PROXY) -> bool:
    """True when the egress org looks like a cloud/datacenter ASN.

    Returns ``False`` whenever detection is disabled or the org is unknown, so
    an opt-in caller is never blocked by missing geo data.
    """
    if not enabled:
        return False
    return classify_proxy_org(geo) == "cloud"


# ---------------------------------------------------------------------------
# Locale / timezone alignment
# ---------------------------------------------------------------------------
def locale_profile_key_from_geo(geo: Mapping[str, Any] | None) -> str:
    if not geo or not AUTO_BROWSER_LOCALE_FROM_IP:
        return DEFAULT_LOCALE_PROFILE
    country = str(geo.get("country") or "").upper()
    return COUNTRY_LOCALE_PROFILE_MAP.get(country, DEFAULT_LOCALE_PROFILE)


def build_browser_environment(
    geo: Mapping[str, Any] | None = None,
    base_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Single source of truth for one browser registration's fingerprint.

    Merges the chosen hardware ``base_profile`` with the geo-aligned locale /
    timezone.  Returns a flat dict consumed by ``run_browser_registration``.
    """
    key = locale_profile_key_from_geo(geo)
    locale = dict(BROWSER_LOCALE_PROFILES.get(key, BROWSER_LOCALE_PROFILES[DEFAULT_LOCALE_PROFILE]))
    if geo and AUTO_BROWSER_LOCALE_FROM_IP:
        tz = str(geo.get("timezone") or "").strip()
        if tz:
            locale["timezone_iana"] = tz
            locale["timezone_offset_minutes"] = _offset_minutes_for_timezone(
                tz, int(locale.get("timezone_offset_minutes", 0))
            )
            locale["timezone_name"] = TIMEZONE_NAME_BY_IANA.get(tz, locale.get("timezone_name", ""))
    profile = dict(base_profile or {})
    profile.update({
        "locale_profile": key,
        "geo": dict(geo or {}),
        "navigator_language": locale["navigator_language"],
        "navigator_languages": list(locale["navigator_languages"]),
        "accept_language": locale["accept_language"],
        "timezone_iana": locale["timezone_iana"],
        "timezone_offset_minutes": int(locale["timezone_offset_minutes"]),
        "timezone_name": locale.get("timezone_name", ""),
        "fingerprint_seed": str(profile.get("fingerprint_seed") or ""),
        # P4 diagnostic: the egress org/ASN class is always carried through so
        # it shows up in logs and persisted identity even when rejection is off.
        "proxy_org": str((geo or {}).get("org") or ""),
        "proxy_org_class": classify_proxy_org(geo),
    })
    return profile


# ---------------------------------------------------------------------------
# Profile consistency self-check
# ---------------------------------------------------------------------------
# Mirrors turb-gpt-free-register's ``validate_browser_profile`` (config/browser.py)
# and aBaiFreeGPT's ``ProtocolEnvironmentProfile.validate()``.  The browser path
# deliberately does NOT own the UA / client-hints / platform — the anti-detect
# provider (Roxy, Cloak, Camoufox) does — so turb's UA/Client-Hints cross-checks
# are intentionally skipped here.  Every field the browser path actually emits is
# verified instead.
#
# Plausible ranges follow aBaiFreeGPT: screen 640..7680 x 480..4320,
# hardware_concurrency 1..128.
_SCREEN_WIDTH_RANGE = (640, 7680)
_SCREEN_HEIGHT_RANGE = (480, 4320)
_HARDWARE_CONCURRENCY_RANGE = (1, 128)
_DEVICE_MEMORY_RANGE = (1, 128)
_DEVICE_PIXEL_RATIO_RANGE = (1.0, 4.0)


def _range_issue(profile: Mapping[str, Any], key: str, bounds: tuple[float, float]) -> list[str]:
    """Return an issue for ``key`` when it is present but out of range.

    Absent keys are ignored on purpose: the browser path lets the anti-detect
    provider own whatever it does not emit (screen size for Roxy/Cloak/Camoufox,
    for example), so a missing field is normal here, not a contradiction.
    """
    value = profile.get(key)
    if value is None or value == "":
        return []
    try:
        number = float(value)
    except (TypeError, ValueError):
        return [f"{key} must be numeric, got {value!r}"]
    lo, hi = bounds
    if not (lo <= number <= hi):
        return [f"{key}={value!r} is outside the plausible range [{lo}, {hi}]"]
    return []


def validate_browser_profile(profile: Any) -> list[str]:
    """Return internal contradictions of a browser fingerprint profile.

    An empty list means the profile is internally consistent.  Uses the same
    contract as :meth:`sms_tool.fingerprint_pool.Fingerprint.validate` on the
    protocol side, so both paths can be asserted identically in tests.
    """
    if not isinstance(profile, Mapping):
        return [f"profile must be a mapping, got {type(profile).__name__}"]
    issues: list[str] = []

    language = str(profile.get("navigator_language") or "").strip()
    if not language:
        issues.append("navigator_language must not be blank")
    languages = [str(item) for item in (profile.get("navigator_languages") or [])]
    if not languages:
        issues.append("navigator_languages must not be empty")
    elif language and language not in languages:
        issues.append(
            f"navigator_language {language!r} is not in navigator_languages {languages!r}"
        )

    accept_language = str(profile.get("accept_language") or "").strip()
    if not accept_language:
        issues.append("accept_language must not be blank")
    elif language and not accept_language.lower().startswith(language.lower()):
        issues.append(
            f"accept_language {accept_language!r} does not start with "
            f"navigator_language {language!r}"
        )

    timezone = str(profile.get("timezone_iana") or "").strip()
    if not timezone:
        issues.append("timezone_iana must not be blank")
    elif "/" not in timezone:
        issues.append(f"timezone_iana {timezone!r} is not an IANA name (expected Area/City)")

    # aBaiFreeGPT: hardware_concurrency must be a curated constant (never
    # os.cpu_count()) and must sit inside a plausible range.
    issues.extend(_range_issue(profile, "screen_width", _SCREEN_WIDTH_RANGE))
    issues.extend(_range_issue(profile, "screen_height", _SCREEN_HEIGHT_RANGE))
    issues.extend(_range_issue(profile, "hardware_concurrency", _HARDWARE_CONCURRENCY_RANGE))
    issues.extend(_range_issue(profile, "device_memory", _DEVICE_MEMORY_RANGE))
    issues.extend(_range_issue(profile, "device_pixel_ratio", _DEVICE_PIXEL_RATIO_RANGE))
    return issues


def select_browser_profile(
    geo: Mapping[str, Any] | None = None,
    *,
    seed: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick a browser profile (rotation), then align its locale/tz to geo."""
    pool = shared_browser_profile_pool(config)
    base = pool.select(seed) if seed else pool.next()
    env = build_browser_environment(geo, base)
    # Carry the rotation metadata through.
    env["browser_profile_index"] = base.get("browser_profile_index", 0)
    env["browser_fingerprint_profile"] = base.get(
        "browser_fingerprint_profile", _label_for_index(base.get("browser_profile_index", 0))
    )
    env["fingerprint_seed"] = str(seed or "")
    # Self-check: surface internal contradictions (locale/timezone mismatch,
    # out-of-range hardware values, …) without ever blocking registration.
    # Validation here is diagnostic only — the same non-fatal contract turb uses.
    issues = validate_browser_profile(env)
    if issues:
        logger.debug(
            "[browser-fingerprint] profile %s has internal contradictions: %s",
            env.get("browser_fingerprint_profile"),
            issues,
        )
    # P4: opt-in cloud/datacenter egress warning.  Diagnostic only — it never
    # blocks, because a user may intentionally pin a fixed cloud egress.
    reject_cloud = False
    if isinstance(config, Mapping):
        reg = config.get("registration")
        if isinstance(reg, Mapping):
            reject_cloud = bool(reg.get("reject_cloud_proxy", False))
    if reject_cloud and is_cloud_proxy(env.get("geo"), enabled=True):
        logger.warning(
            "[browser-fingerprint] proxy egress looks like a cloud/datacenter ASN "
            "(org=%r class=%s); OpenAI may de-prioritize this account",
            env.get("proxy_org"),
            env.get("proxy_org_class"),
        )
    return env


__all__ = [
    "AUTO_BROWSER_LOCALE_FROM_IP",
    "IP_GEO_TIMEOUT",
    "IP_GEO_ENDPOINTS",
    "COUNTRY_LOCALE_PROFILE_MAP",
    "BROWSER_LOCALE_PROFILES",
    "TIMEZONE_NAME_BY_IANA",
    "BROWSER_PROFILE_POOL",
    "BrowserProfilePool",
    "shared_browser_profile_pool",
    "detect_proxy_exit_geo",
    "REJECT_CLOUD_PROXY",
    "CLOUD_PROXY_ORG_KEYWORDS",
    "classify_proxy_org",
    "is_cloud_proxy",
    "locale_profile_key_from_geo",
    "build_browser_environment",
    "validate_browser_profile",
    "select_browser_profile",
]
