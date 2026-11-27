"""Shared browser-like headers for OpenAI auth protocol calls."""

from __future__ import annotations

import hashlib
import random
import re
import secrets
import threading
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version as package_version
from urllib.parse import urlparse

from ..geo.profiles import MARKET_PROFILES, WINDOWS_TIMEZONE_BY_IANA
from ..kernel.native_loader import load_native_module
from .fingerprint_profiles import (
    AUTH_FINGERPRINT_PROFILES,
    _CPU_CHOICES_BY_PLATFORM,
    _DPR_CHOICES_BY_PLATFORM,
    _HARDWARE_PROFILES as _HARDWARE_PROFILES,
    _HEAP_CHOICES_BY_PLATFORM,
    _MACOS_IMPERSONATE as _MACOS_IMPERSONATE,
    _MEM_CHOICES_BY_PLATFORM,
    _SCREEN_CHOICES as _SCREEN_CHOICES,
    _SCREEN_CHOICES_BY_PLATFORM,
    _VENDOR_BY_FAMILY as _VENDOR_BY_FAMILY,
    _browser_family,
    _platform_class,
    browser_family as browser_family,
    hardware_profile_for_family,
    validate_protocol_fingerprint as validate_protocol_fingerprint,
)

_NATIVE_AUTH_HEADERS = load_native_module("auth-headers-rs", "auth_headers_rs", "RUST_ANYTHING_UNIT_AUTH_HEADERS")
_NATIVE_BROWSER_VERSION = getattr(_NATIVE_AUTH_HEADERS, "_browser_version", None)
_NATIVE_FULL_VERSION_LIST = getattr(_NATIVE_AUTH_HEADERS, "_full_version_list", None)
_NATIVE_ACCEPT_ENCODING_FOR = getattr(_NATIVE_AUTH_HEADERS, "accept_encoding_for", None)
_NATIVE_ORIGIN_FROM_REFERER = getattr(_NATIVE_AUTH_HEADERS, "origin_from_referer", None)


AUTH_IMPERSONATE = "firefox144"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"
DEFAULT_SEC_CH_UA = ""
#: The web build the `chatgpt`-family requests claim (`data-seq` / `data-build` of
#: `chatgpt.com/auth/login`, read live 2026-10-12).  The previous pair
#: (8370486 / prod-fb4a8a2a...) was months stale; the reference project tracks the page the same way.
CHATGPT_CLIENT_VERSION = "26.1022.91109"
_OBSERVED_CHATGPT_CLIENT_VERSION = ""
_OBSERVED_CLIENT_VERSION_LOCK = threading.Lock()
_CHATGPT_DOC_VERSION_PATTERNS = (
    re.compile(r'data-seq=["\']([0-9]+\.[0-9]+\.[0-9]+)["\']', re.IGNORECASE),
    re.compile(r'"clientVersion"\s*:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', re.IGNORECASE),
    re.compile(r'data-build=["\']([A-Za-z0-9._-]{6,64})["\']', re.IGNORECASE),
)


def observe_chatgpt_document(html: str | None) -> str:
    """Passively observe ChatGPT's current web client version from login HTML."""
    text = str(html or "")
    if not text:
        return ""
    for pattern in _CHATGPT_DOC_VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            version = match.group(1).strip()
            if version:
                with _OBSERVED_CLIENT_VERSION_LOCK:
                    global _OBSERVED_CHATGPT_CLIENT_VERSION
                    _OBSERVED_CHATGPT_CLIENT_VERSION = version
                return version
    return ""


def chatgpt_client_version() -> str:
    """Return the dynamically observed ChatGPT client version, or the pinned fallback."""
    with _OBSERVED_CLIENT_VERSION_LOCK:
        observed = _OBSERVED_CHATGPT_CLIENT_VERSION
    return observed or CHATGPT_CLIENT_VERSION


def _browser_version(impersonate: str) -> str:
    """Extract the numeric major version from an impersonate token (e.g. '144')."""
    if _NATIVE_BROWSER_VERSION is not None and isinstance(impersonate, str):
        return _NATIVE_BROWSER_VERSION(impersonate)
    match = re.match(r"^[a-z]+(\d+)", str(impersonate or "").lower())
    return match.group(1) if match else ""


_AUTH_FINGERPRINT_LOCAL = threading.local()

# 协议路径地理档案 = geo/profiles.MARKET_PROFILES 的渲染视图（单一事实源，
# 与 browser_fingerprint_pool.BROWSER_LOCALE_PROFILES 同源派生）。
_GEO_PROFILES = {
    country: {
        "timezone": profile["timezone_iana"],
        "lang": profile["lang"],
        "lang_full": profile["lang_full"],
    }
    for country, profile in MARKET_PROFILES.items()
}


def set_fingerprint_geo(
    country: str | None = "",
    *,
    timezone: str | None = "",
    lang: str | None = "",
    lang_full: str | None = "",
) -> dict[str, str]:
    """Bind the current account fingerprint's locale to the proxy exit region.

    ``timezone`` / ``lang`` / ``lang_full`` override the curated table entry and
    exist because ``_GEO_PROFILES`` only covers a handful of markets: a measured
    exit anywhere else used to fall back to the UTC/en-US default while still
    *claiming* that country -- the "Brazilian IP, US Eastern clock" mismatch
    this whole resolver chain was built to remove.  Callers that already
    resolved the exit (``fingerprint_pool``) pass the measurement through here
    instead of letting it be recomputed and then discarded.

    Every parameter is ``str | None`` because the body already treats ``None`` as
    "no override" (``value or ""``) and callers pass the pool's optional
    measurement straight through.  The narrower ``str`` annotation made those
    calls type errors that the runtime had always accepted.
    """
    code = str(country or "").strip().upper()
    profile = dict(_GEO_PROFILES.get(code) or {"timezone": "UTC", "lang": "en-US", "lang_full": "en-US,en;q=0.9"})
    for key, value in (("timezone", timezone), ("lang", lang), ("lang_full", lang_full)):
        trimmed = str(value or "").strip()
        if trimmed:
            profile[key] = trimmed
    _AUTH_FINGERPRINT_LOCAL.geo_profile = profile
    _AUTH_FINGERPRINT_LOCAL.geo_country = code
    return profile


def set_fingerprint_device(device_id: str = "") -> str:
    """Bind the per-account device id so the Sentinel device profile is stable.

    Registration calls this once the account's ``oai-device-id`` is known so the
    derived screen/hardware/heap readings are reproducible for that account (and
    reproducible again on relogin/recovery) yet distinct from other accounts.
    """
    seed = str(device_id or "").strip()
    _AUTH_FINGERPRINT_LOCAL.device_seed = seed
    # A newly bound account starts a fresh browsing context, so drop any cached
    # device profile: the next build recomputes a stable time_origin for it.
    _AUTH_FINGERPRINT_LOCAL.device_profiles = {}
    return seed


def _device_seed() -> str:
    for attr in ("device_seed", "session_id"):
        value = str(getattr(_AUTH_FINGERPRINT_LOCAL, attr, "") or "").strip()
        if value:
            return value
    return ""


def _device_profile(seed: str, platform_class: str = "windows") -> dict[str, object]:
    """Deterministically derive a device profile from ``seed`` (device id).

    ``platform_class`` comes from :func:`_platform_class` and selects the screen /
    DPR / CPU / memory tables, so an iOS or macOS profile can no longer report a
    Windows desktop measurement.  The profile is memoized per ``(seed, class)`` on
    the thread — a real browsing context has a single ``performance.timeOrigin``,
    and recomputing it on every header build would both drift the value per
    request and call ``time.time`` repeatedly, so the first build anchors it.
    """
    cls = str(platform_class or "windows").strip().lower()
    if cls not in _SCREEN_CHOICES_BY_PLATFORM:
        cls = "windows"
    cache = getattr(_AUTH_FINGERPRINT_LOCAL, "device_profiles", None)
    if not isinstance(cache, dict):
        cache = {}
        _AUTH_FINGERPRINT_LOCAL.device_profiles = cache
    cache_key = f"{seed}|{cls}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    digest = hashlib.sha256(seed.encode("utf-8")).digest() if seed else b"\x00" * 8
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    screen = rng.choice(_SCREEN_CHOICES_BY_PLATFORM[cls])
    profile = {
        "screen": screen,
        "device_pixel_ratio": rng.choice(_DPR_CHOICES_BY_PLATFORM[cls]),
        "hardware_concurrency": rng.choice(_CPU_CHOICES_BY_PLATFORM[cls]),
        "device_memory": rng.choice(_MEM_CHOICES_BY_PLATFORM[cls]),
        "js_heap_size_limit": rng.choice(_HEAP_CHOICES_BY_PLATFORM[cls]),
        "platform_class": cls,
    }
    # performance.timeOrigin is "when this page/context was created": a static
    # 2024 constant across every account is itself a tell, so anchor it to now
    # minus a seeded jitter.  The lower bound is 25 s because ``p[13]``
    # (``performance.now()``) below ~8 s reads as "this page was opened just
    # now" and the reference project's HAR measurement (``p[13]=27894.9``)
    # records the server answering such a page with a short ``dx``.
    now_ms = int(time.time() * 1000)
    browsing_ms = rng.randint(25_000, 900_000)
    profile["time_origin"] = now_ms - browsing_ms
    # performance.now() is ms elapsed since time_origin.  Derived from the SAME
    # draw, never sampled independently: a real browser satisfies
    # ``Date.now() ≈ timeOrigin + performance.now()``, and the two values travel
    # in the same proof (``p[17]`` / ``p[13]``), so two independent randoms made
    # the identity false on 98.8 % of measured accounts (largest gap 887 s).
    profile["performance_now"] = round(float(browsing_ms), 3)
    cache[cache_key] = profile
    return profile


def _auth_fingerprint_config():
    try:
        from ..config import CFG

        raw_get = getattr(CFG, "raw_get", None)
        _raw_email_cfg: object = raw_get("email_registration") if callable(raw_get) else CFG.get("email_registration")
        email_cfg: Mapping = _raw_email_cfg if isinstance(_raw_email_cfg, Mapping) else {}
        fallback_raw: object = raw_get("auth_fingerprint") if callable(raw_get) else CFG.get("auth_fingerprint")
        value = email_cfg.get("auth_fingerprint") or fallback_raw or {}
        return value if isinstance(value, Mapping) else {"profile": value}
    except Exception:
        return {}


def high_entropy_client_hints_enabled() -> bool:
    """``email_registration.auth_fingerprint.restore_high_entropy_hints`` (default **False**).

    The five high-entropy ``Sec-CH-UA-*`` headers were removed on 2026-10-31
    because the 2026-10-28 HAR shows a real Chrome sends none of them to
    chatgpt.com / auth.openai.com (neither origin ever sends ``Accept-CH`` for
    them; the only ``Accept-CH`` in that capture came from bat.bing.com). That is
    evidence from one HAR in one browser, so the removal shipped with this
    switch: restoring the headers is the control arm that turns "59/59 accounts
    alive at 120 min" from a no-regression signal into a comparison. See
    ``docs/audits/scans/scan-2026-10-31-har-passwordless-codex-webview.md`` §3/§9.

    Read through the same mapping as the profile selection so an A/B can flip it
    per batch without touching code. Strings are parsed here rather than with
    ``bool()`` because the frozen production config hands back whatever JSON held
    (``"false"`` must not read as true).
    """
    value = _auth_fingerprint_config().get("restore_high_entropy_hints")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def supported_profile_names() -> set[str]:
    """Profile names the **active transport** can actually impersonate.

    Single definition for the three call sites that used to import curl_cffi's
    ``BrowserType`` directly (profile configuration, ``current_auth_fingerprint``
    and ``auth_impersonate``).  With ``wreq`` selected those must filter on
    wreq's preset list instead, or a profile the backend cannot run would stay
    selectable and either fail closed late or silently send a different
    ClientHello under the same UA.  See :mod:`sms_tool.transport.http_backend`.
    """
    try:
        from .http_backend import supported_impersonations

        supported = supported_impersonations()
    except Exception:  # broad-except: capability probing must never break selection
        supported = set()
    if supported:
        return supported
    try:
        from curl_cffi.requests.impersonate import BrowserType

        return {item.value for item in BrowserType}
    except Exception:  # broad-except: no curl_cffi means "no evidence", not a crash
        return {AUTH_IMPERSONATE}


#: Legacy Chrome releases kept in ``AUTH_FINGERPRINT_PROFILES`` for explicit
#: config/test compatibility but excluded from automatic default rotation.
_LEGACY_DEFAULT_ROTATION_EXCLUDED = frozenset({"chrome124", "chrome131", "chrome136"})


def _configured_auth_profiles():
    cfg = _auth_fingerprint_config()
    configured = cfg.get("profiles")
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.replace(";", ",").split(",") if item.strip()]
    if not isinstance(configured, (list, tuple)):
        configured = []
    names = [str(item or "").strip().lower() for item in configured]
    supported = supported_profile_names()
    names = [name for name in names if name in AUTH_FINGERPRINT_PROFILES and name in supported]
    defaults = [
        name
        for name in AUTH_FINGERPRINT_PROFILES
        if name in supported and name not in _LEGACY_DEFAULT_ROTATION_EXCLUDED
    ]
    if not defaults:
        defaults = [name for name in AUTH_FINGERPRINT_PROFILES if name in supported]
    return names or defaults or [AUTH_IMPERSONATE]


# P0-2: weighted-random selection. Firefox stays the majority share because
# Chrome is challenged (HTTP 403) at ChatGPT's Cloudflare edge (see the note on
# AUTH_FINGERPRINT_PROFILES); the reference project's 35% Chrome weight is
# deliberately NOT copied. Each family's weight is split evenly across its
# versions, so adding more Firefox versions raises diversity without diluting
# Firefox's overall share.
# P2-2: removed the ghost "edge": 7 entry — _browser_family does recognise
# edge-prefixed names, but AUTH_FINGERPRINT_PROFILES has no edge member, so the
# weight was never reached.  If an edge profile is added later, add it here too.
_FAMILY_WEIGHTS = {"firefox": 50, "chrome": 25, "safari": 18}


def fingerprint_profile_weights(names) -> dict[str, float]:
    """Return a {name: weight} map for the given profile names.

    Each family's weight is divided across its members so the family's aggregate
    share is preserved regardless of version count. Unknown families fall back to
    the chrome weight so a typo never silently zeroes a profile out.
    """
    names = [str(n) for n in names]
    fam_counts = Counter(_browser_family(n) for n in names)
    weights: dict[str, float] = {}
    for name in names:
        fam = _browser_family(name)
        base = _FAMILY_WEIGHTS.get(fam, _FAMILY_WEIGHTS["chrome"])
        weights[name] = base / max(1, fam_counts[fam])
    return weights


def select_auth_fingerprint(rotate=False):
    cfg = _auth_fingerprint_config()
    mode = str(cfg.get("mode") or "fixed").strip().lower()
    names = _configured_auth_profiles()
    configured = str(cfg.get("profile") or AUTH_IMPERSONATE).strip().lower()
    if rotate and mode in {"rotate", "random", "per_account"}:
        weights = fingerprint_profile_weights(names)
        previous = getattr(_AUTH_FINGERPRINT_LOCAL, "profile_name", "")
        # Avoid repeating the immediately-previous profile for a more even spread,
        # but fall back to the full set if there is only one candidate.
        candidates = [name for name in names if name != previous] or names
        name = random.SystemRandom().choices(candidates, weights=[weights[n] for n in candidates], k=1)[0]
    else:
        name = configured if configured in names else names[0]
    _AUTH_FINGERPRINT_LOCAL.profile_name = name
    return dict(AUTH_FINGERPRINT_PROFILES[name])


def set_auth_fingerprint(name: str) -> None:
    """Explicitly set the current thread's auth fingerprint profile by name.

    When ``name`` is not a known profile, the call is a no-op so callers
    can pass pool-generated identifiers without a hard dependency on the
    canonical profile table.
    """
    profile_name = str(name or "").strip().lower()
    if profile_name in AUTH_FINGERPRINT_PROFILES:
        _AUTH_FINGERPRINT_LOCAL.profile_name = profile_name


def current_auth_fingerprint():
    name = getattr(_AUTH_FINGERPRINT_LOCAL, "profile_name", "")
    supported = supported_profile_names()
    if name in AUTH_FINGERPRINT_PROFILES and name in supported:
        return dict(AUTH_FINGERPRINT_PROFILES[name])
    # Keep the configured browser identity visible to all protocol layers even
    # when the process is running with an older curl_cffi. Registration's
    # network preflight rejects that environment before any mailbox is used;
    # silently downgrading here would make TLS, client hints, and Sentinel
    # disagree about the browser. This also keeps dry-run/unit-test callers
    # deterministic while capability checks remain authoritative for live use.
    cfg = _auth_fingerprint_config()
    configured = str(cfg.get("profile") or AUTH_IMPERSONATE).strip().lower()
    if configured in AUTH_FINGERPRINT_PROFILES:
        _AUTH_FINGERPRINT_LOCAL.profile_name = configured
        return dict(AUTH_FINGERPRINT_PROFILES[configured])
    if name not in AUTH_FINGERPRINT_PROFILES or name not in supported:
        return select_auth_fingerprint(rotate=True)
    return dict(AUTH_FINGERPRINT_PROFILES[name])


def auth_impersonate():
    requested = str(current_auth_fingerprint()["impersonate"])
    supported = supported_profile_names()
    if requested in supported:
        return requested
    # Non-registration callers (for example stored-session workspace checks)
    # may run before the registration preflight. Keep those probes usable with
    # an older curl_cffi while the registration path still fails closed via
    # curl_cffi_capabilities().
    names = _configured_auth_profiles()
    for name in names:
        if name in supported:
            return name
    return next(iter(supported), requested)


def auth_user_agent():
    return current_auth_fingerprint()["user_agent"]


def auth_fingerprint_capabilities() -> dict[str, list[str]]:
    """Return configured profiles the **active backend** can provide."""
    cfg = _auth_fingerprint_config()
    raw = cfg.get("profiles")
    if isinstance(raw, str):
        configured = [item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()]
    elif isinstance(raw, (list, tuple)):
        configured = [str(item or "").strip().lower() for item in raw if str(item or "").strip()]
    else:
        profile = str(cfg.get("profile") or "").strip().lower()
        configured = [profile] if profile else [AUTH_IMPERSONATE]
    installed = supported_profile_names()
    known = set(AUTH_FINGERPRINT_PROFILES)
    available = sorted(name for name in configured if name in known and name in installed)
    missing = sorted(name for name in configured if name not in known or name not in installed)
    return {"configured": configured, "available": available, "missing": missing}


#: ``(major, minor)`` range of curl_cffi this fingerprint table is measured
#: against.  The upper bound is not decoration: every Chrome ``sec_ch_ua``
#: literal below is a *measurement* of what the installed library emits, so a
#: bracket that admits a version nobody re-measured is a silent UA/hint split.
#: 2026-11-19: widened 0.16 → 0.17 together with the dependency bump; the
#: ``test_every_chrome_hint_literal_matches_curl_cffi`` suite re-measures every
#: literal against the installed build, which is what makes the widening safe.
CURL_CFFI_MIN_MINOR = 15
CURL_CFFI_MAX_MINOR = 17


def curl_cffi_capabilities() -> dict[str, object]:
    """Report the installed curl_cffi version and browser profile support."""
    try:
        installed_version = package_version("curl_cffi")
    except PackageNotFoundError:
        installed_version = ""
    profiles = auth_fingerprint_capabilities()
    try:
        parts = tuple(int(part) for part in installed_version.split(".")[:2])
    except (TypeError, ValueError):
        parts = ()
    version_ok = bool(parts and parts[0] == 0 and CURL_CFFI_MIN_MINOR <= parts[1] <= CURL_CFFI_MAX_MINOR)
    return {"version": installed_version, "version_ok": version_ok, **profiles}


def _timezone_offset_minutes(tz_name: str) -> int:
    """UTC offset in minutes for the profile's declared timezone.

    Single authority: ``sms_tool.geo.clock``.  On Windows the IANA database is
    only available when the ``tzdata`` package is installed; without it this
    degrades to ``0`` and the clock module logs a one-time warning, which is
    still better than disagreeing with the requirements token silently.
    """
    try:
        from ..geo.clock import offset_minutes_for_timezone

        return offset_minutes_for_timezone(tz_name, 0)
    except Exception:
        return 0


def _full_version_list(sec_ch_ua: str, full_version: str = "") -> str:
    """Expand a ``Sec-CH-UA`` brand list into ``Sec-CH-UA-Full-Version-List``.

    Real Chrome keeps the exact GREASE brand name/order from ``sec-ch-ua`` while
    filling ``Google Chrome`` / ``Chromium`` with the real 4-component build
    number (e.g. ``146.0.7680.178``) and the GREASE brand with ``<v>.0.0.0``.
    """
    if _NATIVE_FULL_VERSION_LIST is not None and isinstance(sec_ch_ua, str) and isinstance(full_version, str):
        return _NATIVE_FULL_VERSION_LIST(sec_ch_ua, full_version)
    text = str(sec_ch_ua or "").strip()
    if not text:
        return ""
    real_build = str(full_version or "").strip()
    parts = []
    for chunk in text.split(","):
        brand, _, version = chunk.partition(";v=")
        brand = brand.strip()
        major = version.strip().strip('"')
        if not brand or not major:
            continue
        brand_name = brand.strip('"').lower()
        if major.count(".") == 3:
            full = major
        elif real_build and brand_name in {"chromium", "google chrome"} and real_build.startswith(f"{major}."):
            full = real_build
        else:
            full = f"{major}.0.0.0"
        parts.append(f'{brand};v="{full}"')
    return ", ".join(parts)


def _timezone_display_name(tz_name: str) -> str:
    """OS-style timezone label for a IANA id (``Asia/Tokyo`` → ``Japan Standard Time``).

    Single authority: :data:`sms_tool.geo.profiles.WINDOWS_TIMEZONE_BY_IANA`, the
    same table the browser path uses for
    ``Intl.DateTimeFormat().resolvedOptions().timeZoneName``.  An unknown zone
    degrades to the IANA id, which is what every profile did before this helper
    existed — never to an empty string, because a missing label is more
    conspicuous than a slightly unusual one.
    """
    value = str(tz_name or "").strip()
    if not value:
        return ""
    return str(WINDOWS_TIMEZONE_BY_IANA.get(value) or value)


def sentinel_fingerprint() -> dict[str, object]:
    """Return the browser environment shared by auth headers and Sentinel SDK."""
    fingerprint = current_auth_fingerprint()
    impersonate = str(fingerprint.get("impersonate") or "")
    family = _browser_family(impersonate)
    geo = getattr(_AUTH_FINGERPRINT_LOCAL, "geo_profile", None) or set_fingerprint_geo("")
    session_id = str(getattr(_AUTH_FINGERPRINT_LOCAL, "session_id", "") or uuid.uuid4())
    _AUTH_FINGERPRINT_LOCAL.session_id = session_id
    platform_class = _platform_class(impersonate)
    device = _device_profile(_device_seed() or session_id, platform_class)
    is_chrome = family == "chrome"
    # Platform-class constants come from the family table, not a hardcoded
    # "Win32". This keeps the UA (which names the OS) and the reported platform
    # in agreement; P0-2 populates the table for non-desktop families.
    hw = hardware_profile_for_family(impersonate)
    return {
        **fingerprint,
        "screen": device["screen"],
        "lang": geo["lang"],
        "lang_full": geo["lang_full"],
        "navigator_platform": hw["navigator_platform"],
        "navigator_vendor": hw["navigator_vendor"],
        "hardware_concurrency": device["hardware_concurrency"],
        "device_memory": device["device_memory"],
        "max_touch_points": hw["max_touch_points"],
        "device_pixel_ratio": device["device_pixel_ratio"],
        "timezone": geo["timezone"],
        # ``Intl.DateTimeFormat().resolvedOptions().timeZoneName`` is an OS-style
        # name ("Japan Standard Time"), never the IANA id.  The protocol path
        # never set it, so ``sentinel/runner.py`` fell back to the IANA string —
        # and ``_requirements_token`` formatted ``%Z`` instead, i.e. the two
        # halves of one registration disagreed ("(Asia/Tokyo)" vs "(JST)") and
        # neither matched a real browser.  One table, shared with the browser
        # lane (`geo.profiles.WINDOWS_TIMEZONE_BY_IANA`).
        "timezone_name": _timezone_display_name(geo["timezone"]),
        # The Sentinel PoW payload carries ``timezoneOffsetMinutes`` next to the
        # declared IANA zone (``sentinel/runner.py``).  The protocol profile
        # never set it, so every registration sent ``0`` — a UTC offset on a
        # New York clock, which is a contradiction the server can check for
        # free.  Derived from the same zone name via the same helper the
        # requirements token uses, so the two halves of the proof agree.
        "timezone_offset_minutes": _timezone_offset_minutes(geo["timezone"]),
        "session_id": session_id,
        "js_heap_size_limit": device["js_heap_size_limit"],
        "time_origin": device["time_origin"],
        "performance_now": device["performance_now"],
        # Sec-CH-UA-* client hints are Chromium-only; Firefox emits none of them.
        "sec_ch_ua_full_version_list": (
            _full_version_list(
                str(fingerprint.get("sec_ch_ua") or ""),
                str(fingerprint.get("full_version") or ""),
            )
            if is_chrome
            else ""
        ),
        "sec_ch_ua_arch": "x86" if is_chrome else "",
        "sec_ch_ua_bitness": "64" if is_chrome else "",
        "sec_ch_ua_model": "",
        "sec_ch_ua_platform_version": hw["sec_ch_ua_platform_version"],
    }


#: First ``(major, minor)`` version of each engine that advertises ``zstd`` in
#: ``Accept-Encoding``.  Chrome/Edge 123 (2024-03), Firefox 126 (2024-05), and
#: Safari 26.3+ / 27.0 (2026-02).
_ZSTD_SINCE: dict[str, tuple[int, int]] = {
    "chrome": (123, 0),
    "edge": (123, 0),
    "firefox": (126, 0),
    "safari": (26, 3),
}
_ACCEPT_ENCODING_ZSTD = "gzip, deflate, br, zstd"
_ACCEPT_ENCODING_NO_ZSTD = "gzip, deflate, br"


def _safari_version_tuple(impersonate: str) -> tuple[int, int] | None:
    token = str(impersonate or "").strip().lower().removeprefix("safari").removesuffix("_ios")
    if not token:
        return None
    if "_" in token:
        major_s, _, minor_s = token.partition("_")
    elif len(token) > 2:
        major_s, minor_s = token[:2], token[2:]
    else:
        major_s, minor_s = token, "0"
    try:
        return (int(major_s), int(minor_s or 0))
    except (TypeError, ValueError):
        return None


def accept_encoding_for(impersonate: str) -> str:
    """``Accept-Encoding`` matching the impersonated engine, not the newest one.

    Chrome 123+, Edge 123+, Firefox 126+, and Safari 26.3+ (including Safari 27)
    advertise ``zstd``; older Safari builds (17.0 / 18.0 / 18.4 / 26.0) omit it.
    """
    if _NATIVE_ACCEPT_ENCODING_FOR is not None and isinstance(impersonate, str):
        return _NATIVE_ACCEPT_ENCODING_FOR(impersonate)
    family = _browser_family(impersonate)
    floor = _ZSTD_SINCE.get(family)
    if floor is None:
        return _ACCEPT_ENCODING_NO_ZSTD
    if family == "safari":
        ver = _safari_version_tuple(impersonate)
        return _ACCEPT_ENCODING_ZSTD if (ver is not None and ver >= floor) else _ACCEPT_ENCODING_NO_ZSTD
    major = _browser_version(impersonate)
    try:
        supported = (int(major), 0) >= floor
    except (TypeError, ValueError):
        supported = True
    return _ACCEPT_ENCODING_ZSTD if supported else _ACCEPT_ENCODING_NO_ZSTD


def datadog_trace_headers() -> dict[str, str]:
    # Trace ids ride in every request as ``traceparent``; a predictable value
    # lets a third party join or forge this client's traces, so they come from
    # the CSPRNG rather than the Mersenne Twister.
    trace_id = str(secrets.randbits(64))
    parent_id = str(secrets.randbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def origin_from_referer(referer: str = "") -> str:
    if _NATIVE_ORIGIN_FROM_REFERER is not None and isinstance(referer, str):
        return _NATIVE_ORIGIN_FROM_REFERER(referer)
    try:
        parsed = urlparse(str(referer or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def _extra_header_value(extra: dict | None, name: str) -> str:
    if not isinstance(extra, dict):
        return ""
    target = name.lower()
    for key, value in extra.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def openai_auth_headers(
    did: str = "",
    *,
    referer: str = "",
    origin: str = "",
    accept: str = "application/json",
    sentinel: dict | None = None,
    sentinel_token: str = "",
    sentinel_so_token: str = "",
    extra: dict | None = None,
    include_trace: bool = True,
    family: str = "auth",
    session_id: str = "",
    flow_invocation_id: str = "",
) -> dict[str, str]:
    referer = str(referer or "").strip() or _extra_header_value(extra, "referer")
    origin = str(origin or "").strip() or _extra_header_value(extra, "origin")
    fingerprint = current_auth_fingerprint()
    environment = sentinel_fingerprint()
    browser_family = _browser_family(str(fingerprint.get("impersonate") or ""))
    headers = {
        "Accept": accept,
        "Accept-Language": str(environment["lang_full"]),
        "Accept-Encoding": accept_encoding_for(str(fingerprint["impersonate"])),
        "User-Agent": fingerprint["user_agent"],
    }
    # Sec-CH-UA-* client hints are Chromium-only. Firefox emits none, so only
    # attach them when the active fingerprint is Chrome-family.
    #
    # 🔴 Only the three **low-entropy** hints below go on the wire. The
    # high-entropy ones (``full-version-list`` / ``arch`` / ``bitness`` /
    # ``model`` / ``platform-version``) used to be sent too and that was wrong:
    # Chrome releases a high-entropy hint only after the **origin itself** asked
    # for it with ``Accept-CH`` (or shipped ``ch-ua-*=()`` in Permissions-Policy
    # to suppress it). The 2026-10-28 HAR of a full passwordless registration
    # has 827 entries and **zero** high-entropy hint headers: chatgpt.com and
    # auth.openai.com never send ``Accept-CH`` for them (the only ``Accept-CH``
    # in the whole capture comes from bat.bing.com, a third party we never call).
    #
    # ``sentinel_fingerprint()`` keeps producing the same values: those feed the
    # Sentinel SDK's ``navigator.userAgentData`` (JS-side, always available to
    # the page), not an HTTP header. Deleting them there would change the proof.
    if browser_family == "chrome":
        headers.update(
            {
                "sec-ch-ua": fingerprint["sec_ch_ua"],
                "sec-ch-ua-mobile": fingerprint["sec_ch_ua_mobile"],
                "sec-ch-ua-platform": fingerprint["sec_ch_ua_platform"],
            }
        )
        if high_entropy_client_hints_enabled():
            # Control arm only (default off). Restores exactly what the 2026-10-31
            # landing removed, so an A/B differs in nothing else: same UA, same
            # TLS impersonation, same Sentinel proof, same five values.
            headers.update(
                {
                    "sec-ch-ua-full-version-list": str(environment["sec_ch_ua_full_version_list"]),
                    "sec-ch-ua-arch": str(environment["sec_ch_ua_arch"]),
                    "sec-ch-ua-bitness": str(environment["sec_ch_ua_bitness"]),
                    "sec-ch-ua-model": str(environment["sec_ch_ua_model"]),
                    "sec-ch-ua-platform-version": str(environment["sec_ch_ua_platform_version"]),
                }
            )
    if referer:
        headers["Referer"] = str(referer)
    resolved_origin = str(origin or "").strip() or origin_from_referer(referer)
    if resolved_origin:
        headers["Origin"] = resolved_origin
    header_family = str(family or "auth").strip().lower()
    if header_family == "chatgpt":
        # Real-browser HAR captures (chatgpt.com + auth.openai.com) confirm that
        # ``oai-device-id`` and ``oai-session-id`` are emitted ONLY on
        # ``chatgpt.com/backend-api/*`` and ``/backend-anon/*`` requests.
        # ``auth.openai.com/api/accounts/*`` (family="auth") and NextAuth
        # ``/api/auth/*`` (family="nextauth") never send either header; keeping
        # them off non-chatgpt lanes removes a trivially detectable wire leak.
        did = str(did or "").strip()
        if did:
            headers["oai-device-id"] = did
        stable_session_id = str(session_id or environment.get("session_id") or "").strip()
        if stable_session_id:
            headers["oai-session-id"] = stable_session_id
    invocation_id = str(flow_invocation_id or "").strip()
    if invocation_id:
        headers["x-access-flow-invocation-id"] = invocation_id
    if include_trace:
        headers.update(datadog_trace_headers())
    if sentinel or sentinel_token or sentinel_so_token:
        try:
            from ..codex.sentinel import attach_sentinel

            sentinel_data = dict(sentinel or {})
            if sentinel_token:
                sentinel_data["sentinel_token"] = sentinel_token
            if sentinel_so_token:
                sentinel_data["sentinel_so_token"] = sentinel_so_token
            attach_sentinel(headers, sentinel_data)
        except Exception:
            pass
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
    if header_family == "nextauth":
        # The five high-entropy hint names are no longer emitted by the chrome
        # branch above; they stay in this strip list because ``extra`` is merged
        # before this point, so a caller that passes one through ``extra`` still
        # cannot smuggle it onto a NextAuth request.
        for key in (
            "sec-ch-ua-full-version-list",
            "sec-ch-ua-arch",
            "sec-ch-ua-bitness",
            "sec-ch-ua-model",
            "sec-ch-ua-platform-version",
            "oai-device-id",
            "oai-session-id",
            "traceparent",
            "tracestate",
            "x-datadog-origin",
            "x-datadog-parent-id",
            "x-datadog-sampling-priority",
            "x-datadog-trace-id",
            "openai-sentinel-token",
            "openai-sentinel-so-token",
        ):
            headers.pop(key, None)
    elif header_family == "chatgpt":
        headers.update(
            {
                # The 2026-10-23 HAR carries only `oai-client-version` on
                # chatgpt.com backend requests; the old build-number header is
                # no longer sent by the web client.
                "oai-client-version": chatgpt_client_version(),
            }
        )
        # The 2026-10-28 HAR sends `oai-language` on every chatgpt.com
        # backend-api request (`me`, `accounts/check`, sentinel prepare and
        # finalize alike), valued with the short locale tag (`hi-IN`), not the
        # weighted `Accept-Language` string. Same source as the Accept-Language
        # first component, so the two cannot disagree.
        locale_tag = str(environment.get("lang") or "").strip()
        if locale_tag:
            headers["oai-language"] = locale_tag
    return headers


def nextauth_headers(did: str = "", **kwargs) -> dict[str, str]:
    return openai_auth_headers(did, family="nextauth", include_trace=False, **kwargs)


def chatgpt_headers(did: str = "", **kwargs) -> dict[str, str]:
    return openai_auth_headers(did, family="chatgpt", **kwargs)


def openai_auth_headers_lower(did: str = "", extra: dict | None = None, **kwargs) -> dict[str, str]:
    headers = openai_auth_headers(did, extra=extra, **kwargs)
    return {str(k).lower(): v for k, v in headers.items()}
