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

from .geo.profiles import MARKET_PROFILES, WINDOWS_TIMEZONE_BY_IANA


AUTH_IMPERSONATE = "firefox144"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"
DEFAULT_SEC_CH_UA = ""
#: The web build the `chatgpt`-family requests claim (`data-seq` / `data-build` of
#: `chatgpt.com/auth/login`, read live 2026-10-12).  The previous pair
#: (8370486 / prod-fb4a8a2a...) was months stale; the reference project tracks the page the same way.
CHATGPT_CLIENT_VERSION = "26.1022.91109"

# Firefox (first) is the primary impersonation. At ChatGPT's edge, Cloudflare
# challenges Chrome TLS/HTTP2 with HTTP 403 while Firefox passes, so the protocol
# path defaults to firefox144. Chrome is retained as a fallback pool entry for
# A/B comparison and recovery. Firefox does not emit Sec-CH-UA client hints, so
# its sec_ch_ua is intentionally blank (see family-aware header building below).
#
# P0-2: the pool was a 2-entry round-robin (firefox144 / chrome146 only), which
# made the Nth account's fingerprint trivially predictable and left hundreds of
# device-derived hardware silhouettes sharing just two browser versions. The
# matrix below spans the Firefox / Chrome / Safari(macOS) / Safari(iOS) families
# that curl_cffi actually supports; selection is weighted-random (see
# _FAMILY_WEIGHTS) with Firefox kept dominant because of the CF-edge constraint
# above. The two original profiles are kept byte-for-byte identical so existing
# persisted-account attribution (which keys on the profile *name*) is untouched.
AUTH_FINGERPRINT_PROFILES = {
    # ── Firefox family (primary) ──────────────────────────────────────────────
    "firefox144": {
        "name": "firefox144",
        "impersonate": "firefox144",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "firefox147": {
        "name": "firefox147",
        "impersonate": "firefox147",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "firefox135": {
        "name": "firefox135",
        "impersonate": "firefox135",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "firefox133": {
        "name": "firefox133",
        "impersonate": "firefox133",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    # ── Chrome family (CF-edge fallback, minority weight) ─────────────────────
    #
    # 🔴 `sec_ch_ua` is **measured, not written**: every literal below is what the
    # installed curl_cffi sends for that same impersonate (captured 2026-10-31
    # with curl_cffi 0.16.0 against a local echo server). Chrome rotates both the
    # GREASE brand (`Not(A:Brand` / `Not-A.Brand` / `Not_A Brand` / `Not:A-Brand`)
    # and the brand ORDER every few majors, so a hand-written list goes stale
    # silently -- the pre-2026-10-31 table had `"Google Chrome"` first with
    # `Not(A:Brand";v="24"` for every version, which matches no Chrome at all.
    # The test `test_every_chrome_hint_literal_matches_curl_cffi` re-measures this,
    # so a curl_cffi upgrade that changes the emissions fails the suite instead of
    # leaving the UA and the hints disagreeing.
    #
    # There is deliberately **no `chrome150` entry**: curl_cffi 0.16.0's
    # `BrowserType` stops at `chrome146`, so a chrome150 profile could only ever
    # be selected and then fail closed (`auth_fingerprint_unavailable`) in the
    # preflight, or fall back silently and send Chrome/150 hints over a 146 TLS
    # fingerprint. `chrome146` is the newest real option; bump this table when
    # curl_cffi ships a newer one.
    "chrome146": {
        "name": "chrome146",
        "impersonate": "chrome146",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome145": {
        "name": "chrome145",
        "impersonate": "chrome145",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome142": {
        "name": "chrome142",
        "impersonate": "chrome142",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome136": {
        "name": "chrome136",
        "impersonate": "chrome136",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome131": {
        "name": "chrome131",
        "impersonate": "chrome131",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome124": {
        "name": "chrome124",
        "impersonate": "chrome124",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    # ── Safari family, macOS (WebKit, no Sec-CH-UA client hints) ──────────────
    "safari18_0": {
        "name": "safari18_0",
        "impersonate": "safari18_0",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
    "safari17_0": {
        "name": "safari17_0",
        "impersonate": "safari17_0",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
    "safari184": {
        "name": "safari184",
        "impersonate": "safari184",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
    # ── Safari family, iOS (distinct navigator.platform + touch points) ───────
    "safari18_0_ios": {
        "name": "safari18_0_ios",
        "impersonate": "safari18_0_ios",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?1",
        "sec_ch_ua_platform": '"iOS"',
    },
    "safari17_2_ios": {
        "name": "safari17_2_ios",
        "impersonate": "safari17_2_ios",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?1",
        "sec_ch_ua_platform": '"iOS"',
    },
    "safari184_ios": {
        "name": "safari184_ios",
        "impersonate": "safari184_ios",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?1",
        "sec_ch_ua_platform": '"iOS"',
    },
}


def _browser_family(impersonate: str) -> str:
    """Classify an impersonate token as 'firefox' / 'chrome' / 'safari' / 'edge'."""
    name = str(impersonate or "").lower()
    if name.startswith("firefox"):
        return "firefox"
    if name.startswith("chrome"):
        return "chrome"
    if name.startswith("safari"):
        return "safari"
    if name.startswith("edge"):
        return "edge"
    return "chrome"


def browser_family(impersonate: str) -> str:
    """Public view of :func:`_browser_family` for other layers.

    ``sentinel/client`` has to know whether a profile is Chromium (does it have
    ``performance.memory`` and ``window.InstallTrigger``?) and
    ``openai_auth_headers`` has to know whether it may advertise ``zstd``.  Both
    used to re-derive the answer locally or not ask at all; one exported
    classifier keeps a single definition of "which engine is this".
    """
    return _browser_family(impersonate)


def _browser_version(impersonate: str) -> str:
    """Extract the numeric major version from an impersonate token (e.g. '144')."""
    match = re.match(r"^[a-z]+(\d+)", str(impersonate or "").lower())
    return match.group(1) if match else ""


# ──────────────────── Family-level hardware constants ─────────────────────────
#
# Platform-class fields must agree with the User-Agent's claimed OS. A UA that
# says "macOS" but reports navigator.platform "Win32" (and sec_ch_ua_platform
# "Windows") is a free contradiction for Cloudflare to flag at zero cost — the
# exact trap the reference project warns about in fingerprint.py:1541-1547.
#
# These fields used to be hardcoded "Win32" / "10.0.0" strings inside
# sentinel_fingerprint(), which only held because every profile was a Windows
# desktop browser. The table below is now the single source of truth so the
# protocol path and the browser-profile path never disagree. P0-2 extends it
# (safari/macos, safari/ios) when the version matrix lands — until then the
# current two profiles stay byte-identical to the old hardcoded output.
#
# NOTE: per-account device_memory / hardware_concurrency / device_pixel_ratio /
# screen stay in _device_profile() (derived from the account device id). That
# per-account variation is intentional anti-correlation and is NOT family-fixed;
# only the platform-class constants that must match the UA live in this table.
# Platform-class constants per OS. The protocol path (and, eventually, the
# browser path) both read these so a UA claiming macOS never reports a Windows
# platform. P1-1 introduced this table; P0-2 extended it with macOS/iOS Safari.
# navigator.vendor is browser-specific (Firefox/Edge empty, Chrome "Google
# Inc.", Safari "Apple Computer, Inc.") so it lives in _VENDOR_BY_FAMILY.
_HARDWARE_PROFILES = {
    "windows": {
        "navigator_platform": "Win32",
        "max_touch_points": 0,
        # Windows 11 24H2+ reports 19.0.0 (HAR 2026-10-23); Win10 reports
        # 10.0.0. The UA stays "Windows NT 10.0" for both.
        "sec_ch_ua_platform_version": "19.0.0",
    },
    "macos": {
        "navigator_platform": "MacIntel",
        "max_touch_points": 0,
        "sec_ch_ua_platform_version": "14.5.0",
    },
    "ios": {
        "navigator_platform": "iPhone",
        "max_touch_points": 5,
        "sec_ch_ua_platform_version": "17.2.0",
    },
}

_VENDOR_BY_FAMILY = {
    "firefox": "",
    "chrome": "Google Inc.",
    "safari": "Apple Computer, Inc.",
    "edge": "",
}


def _platform_class(impersonate: str) -> str:
    """Map an impersonate token to its platform class (windows/macos/ios)."""
    token = str(impersonate or "").lower()
    if token.endswith("_ios"):
        return "ios"
    if token.endswith("_android"):
        return "android"
    if _browser_family(token) == "safari":
        return "macos"
    return "windows"


def hardware_profile_for_family(impersonate: str) -> dict[str, object]:
    """Return platform-class hardware constants for an impersonate token.

    Combines the OS platform class (platform string, touch points, platform
    version) with the browser family's vendor. Unknown tokens degrade to the
    Windows desktop profile rather than to a half-empty dict, and the returned
    dict always carries every key callers expect.
    """
    cls = _platform_class(impersonate)
    hw = dict(_HARDWARE_PROFILES.get(cls, _HARDWARE_PROFILES["windows"]))
    hw["navigator_vendor"] = _VENDOR_BY_FAMILY.get(_browser_family(impersonate), "")
    return hw


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


# ──────────────────── Per-account device profile (anti-correlation) ──────────
#
# The screen/hardware/heap readings reported to Sentinel used to be one hard-
# coded desktop shared by every account, so a whole batch looked like the exact
# same machine (only DID/session differed) — a trivial cluster signal. These are
# now derived deterministically from the account's device id: the same account
# always reports the same device, but different accounts differ. Values stay
# within realistic Windows-desktop Chrome ranges (deviceMemory is capped at 8 as
# Chrome does; touch points stay 0 for a desktop profile).
#
# P0-2 (2026-11-11): the tables are now **per platform class**. Before this,
# every profile — including `safari18_0_ios` — drew from the Windows desktop
# list, so an iPhone UA reported `screen=2560x1080, dpr=1.25, maxTouchPoints=5`
# and a macOS Safari reported a Windows ultrawide screen. That contradiction
# travels inside the Sentinel proof (`p[0]` is width+height; the profile block
# carries platform + touch points), so it was checkable server-side for free.
# Measured: 9.4 % of pool draws were iOS and 9.7 % macOS before the fix.
_SCREEN_CHOICES = (
    "1920x1080",
    "1536x864",
    "1366x768",
    "1600x900",
    "2560x1440",
    "1440x900",
    "1680x1050",
    "1920x1200",
    "2048x1152",
    "2560x1080",
)
_DPR_CHOICES = (1.0, 1.0, 1.25, 1.5)
_CPU_CHOICES = (4, 6, 8, 8, 12, 16)
_MEM_CHOICES = (4, 8, 8)
_HEAP_CHOICES = (2172649472, 3221225472, 4294705152, 4395630592)

#: macOS Safari/Chrome: Retina panels, DPR 2.0 dominates, no 1080p ultrawide.
_SCREEN_CHOICES_MACOS = (
    "1440x900",
    "1512x982",
    "1680x1050",
    "1728x1117",
    "1920x1200",
    "2560x1440",
    "2560x1600",
)
_DPR_CHOICES_MACOS = (2.0, 2.0, 1.0)
_CPU_CHOICES_MACOS = (8, 10, 12)
_MEM_CHOICES_MACOS = (8, 8, 16)

#: iPhone Safari: CSS-pixel viewports of the models still in the wild.
_SCREEN_CHOICES_IOS = (
    "390x844",
    "393x852",
    "428x926",
    "430x932",
    "375x812",
    "414x896",
)
_DPR_CHOICES_IOS = (3.0, 3.0, 2.0)
_CPU_CHOICES_IOS = (4, 6)
_MEM_CHOICES_IOS = (4, 4, 8)

_SCREEN_CHOICES_BY_PLATFORM = {
    "windows": _SCREEN_CHOICES,
    "macos": _SCREEN_CHOICES_MACOS,
    "ios": _SCREEN_CHOICES_IOS,
    # No Android profile ships today; the desktop list is the least surprising
    # placeholder and `validate_protocol_fingerprint` flags the mismatch.
    "android": _SCREEN_CHOICES_IOS,
}
_DPR_CHOICES_BY_PLATFORM = {
    "windows": _DPR_CHOICES,
    "macos": _DPR_CHOICES_MACOS,
    "ios": _DPR_CHOICES_IOS,
    "android": _DPR_CHOICES_IOS,
}
_CPU_CHOICES_BY_PLATFORM = {
    "windows": _CPU_CHOICES,
    "macos": _CPU_CHOICES_MACOS,
    "ios": _CPU_CHOICES_IOS,
    "android": _CPU_CHOICES_IOS,
}
_MEM_CHOICES_BY_PLATFORM = {
    "windows": _MEM_CHOICES,
    "macos": _MEM_CHOICES_MACOS,
    "ios": _MEM_CHOICES_IOS,
    "android": _MEM_CHOICES_IOS,
}

#: ``performance.memory`` is Chromium-only; the vendored runner gates it by
#: family, so this value is inert for Firefox/Safari profiles.
_HEAP_CHOICES_BY_PLATFORM = {
    "windows": _HEAP_CHOICES,
    "macos": (2172649472, 3221225472, 4294705152),
    "ios": (1073741824, 2147483648),
    "android": (1073741824, 2147483648),
}


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
        from .config import CFG

        _raw_email_cfg: object = CFG.get("email_registration")
        email_cfg: dict = _raw_email_cfg if isinstance(_raw_email_cfg, dict) else {}
        value = email_cfg.get("auth_fingerprint") or CFG.get("auth_fingerprint") or {}
        return value if isinstance(value, dict) else {"profile": value}
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


def _configured_auth_profiles():
    cfg = _auth_fingerprint_config()
    configured = cfg.get("profiles")
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.replace(";", ",").split(",") if item.strip()]
    if not isinstance(configured, (list, tuple)):
        configured = []
    names = [str(item or "").strip().lower() for item in configured]
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {item.value for item in BrowserType}
    except Exception:
        supported = {AUTH_IMPERSONATE}
    names = [name for name in names if name in AUTH_FINGERPRINT_PROFILES and name in supported]
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
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {item.value for item in BrowserType}
    except Exception:
        supported = set(AUTH_FINGERPRINT_PROFILES)
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
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {item.value for item in BrowserType}
    except Exception:
        supported = {requested}
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
    """Return configured profiles that the installed curl_cffi can provide."""
    cfg = _auth_fingerprint_config()
    raw = cfg.get("profiles")
    if isinstance(raw, str):
        configured = [item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()]
    elif isinstance(raw, (list, tuple)):
        configured = [str(item or "").strip().lower() for item in raw if str(item or "").strip()]
    else:
        profile = str(cfg.get("profile") or "").strip().lower()
        configured = [profile] if profile else [AUTH_IMPERSONATE]
    try:
        from curl_cffi.requests.impersonate import BrowserType

        installed = {item.value for item in BrowserType}
    except Exception:
        installed = set()
    known = set(AUTH_FINGERPRINT_PROFILES)
    available = sorted(name for name in configured if name in known and name in installed)
    missing = sorted(name for name in configured if name not in known or name not in installed)
    return {"configured": configured, "available": available, "missing": missing}


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
    version_ok = bool(parts and (parts[0] == 0 and 15 <= parts[1] < 17))
    return {"version": installed_version, "version_ok": version_ok, **profiles}


def _timezone_offset_minutes(tz_name: str) -> int:
    """UTC offset in minutes for the profile's declared timezone.

    Single authority: ``sms_tool.geo.clock``.  On Windows the IANA database is
    only available when the ``tzdata`` package is installed; without it this
    degrades to ``0`` and the clock module logs a one-time warning, which is
    still better than disagreeing with the requirements token silently.
    """
    try:
        from .geo.clock import offset_minutes_for_timezone

        return offset_minutes_for_timezone(tz_name, 0)
    except Exception:
        return 0


def _full_version_list(sec_ch_ua: str) -> str:
    """Expand a ``Sec-CH-UA`` brand list into the ``Sec-CH-UA-Full-Version-List`` form.

    Chrome emits both hints from the same GREASE seed: identical brand names, the
    same order, the same grease version, and only the version *widened* to four
    components (``"Chromium";v="146"`` -> ``"Chromium";v="146.0.0.0"``).
    Synthesising this list from a constant instead (the pre-2026-10-31
    ``"Google Chrome";v=.., "Chromium";v=.., "Not(A:Brand";v="24.0.0.0"``)
    made the JS-side ``navigator.userAgentData`` disagree with the ``sec-ch-ua``
    header the same account sent -- a contradiction the SDK reports and a real
    Chrome never has. Deriving it keeps the two halves in lockstep for every
    profile.

    Returns ``""`` for an empty input (Firefox/Safari emit no client hints).
    """
    text = str(sec_ch_ua or "").strip()
    if not text:
        return ""
    parts = []
    for chunk in text.split(","):
        brand, _, version = chunk.partition(";v=")
        brand = brand.strip()
        major = version.strip().strip('"')
        if not brand or not major:
            continue
        full = major if major.count(".") == 3 else f"{major}.0.0.0"
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
            _full_version_list(str(fingerprint.get("sec_ch_ua") or "")) if is_chrome else ""
        ),
        "sec_ch_ua_arch": "x86" if is_chrome else "",
        "sec_ch_ua_bitness": "64" if is_chrome else "",
        "sec_ch_ua_model": "" if is_chrome else "",
        "sec_ch_ua_platform_version": hw["sec_ch_ua_platform_version"],
    }


#: First major version of each engine that advertises ``zstd`` in
#: ``Accept-Encoding``.  Chrome/Edge 123 (2024-03), Firefox 126 (2024-05);
#: WebKit only from **Safari 26.3** (2026-02), so every Safari profile this repo
#: ships (17.0 / 18.0 / 18.4) must not advertise it.  Sending one shared list to
#: every family made 19.2 % of the fingerprint pool (all six Safari profiles, 3
#: macOS + 3 iOS) claim a coding their engine cannot decode.
_ZSTD_SINCE = {"chrome": 123, "edge": 123, "firefox": 126}
_ACCEPT_ENCODING_ZSTD = "gzip, deflate, br, zstd"
_ACCEPT_ENCODING_NO_ZSTD = "gzip, deflate, br"


def accept_encoding_for(impersonate: str) -> str:
    """``Accept-Encoding`` matching the impersonated engine, not the newest one.

    An unknown version token keeps ``zstd`` for the browsers that have had it
    for two years (every profile in the table is above its floor) and drops it
    for WebKit, whose support landed in Safari 26.3.
    """
    family = _browser_family(impersonate)
    floor = _ZSTD_SINCE.get(family)
    if floor is None:
        return _ACCEPT_ENCODING_NO_ZSTD
    major = _browser_version(impersonate)
    try:
        supported = int(major) >= floor
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


#: Plausible screen bounds per platform class.  Deliberately wide: this is a
#: contradiction detector, not a device catalog.
_SCREEN_BOUNDS_BY_PLATFORM = {
    "windows": ((1024, 5120), (600, 2880)),
    "macos": ((1280, 3840), (720, 2400)),
    "ios": ((320, 500), (480, 1000)),
    "android": ((320, 520), (480, 1000)),
}


def _validate_platform_block(fingerprint: Mapping[str, object], platform_class: str) -> list[str]:
    """Platform string and touch points must agree with the profile's OS."""
    issues: list[str] = []
    platform = str(fingerprint.get("navigator_platform") or "")
    expected = {"windows": "Win32", "macos": "MacIntel", "ios": "iPhone"}.get(platform_class, "")
    if expected and platform != expected:
        issues.append(f"navigator_platform {platform!r} does not match {platform_class!r} profile")
    try:
        touch = int(str(fingerprint.get("max_touch_points") or 0))
    except (TypeError, ValueError):
        return issues + ["max_touch_points must be an integer"]
    if platform_class == "ios" and touch <= 0:
        issues.append("iOS profile reports max_touch_points=0")
    elif platform_class in {"windows", "macos"} and touch != 0:
        issues.append(f"desktop profile reports max_touch_points={touch}")
    return issues


def _validate_screen_block(fingerprint: Mapping[str, object], platform_class: str) -> list[str]:
    """Screen geometry and DPR must be plausible *for this platform*."""
    issues: list[str] = []
    screen = str(fingerprint.get("screen") or "")
    width_text, _, height_text = screen.partition("x")
    try:
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError):
        issues.append(f"screen {screen!r} is not WxH")
    else:
        bounds = _SCREEN_BOUNDS_BY_PLATFORM.get(platform_class)
        if bounds is not None:
            (width_low, width_high), (height_low, height_high) = bounds
            if not width_low <= width <= width_high or not height_low <= height <= height_high:
                issues.append(f"screen {screen} is implausible for a {platform_class} profile")
    try:
        dpr = float(str(fingerprint.get("device_pixel_ratio") or 0))
    except (TypeError, ValueError):
        dpr = 0.0
    if dpr <= 0:
        issues.append("device_pixel_ratio must be positive")
    elif platform_class == "ios" and dpr < 2.0:
        issues.append(f"iOS profile reports device_pixel_ratio={dpr}")
    return issues


def _validate_locale_block(fingerprint: Mapping[str, object]) -> list[str]:
    """Locale, Accept-Language and clock must describe one place."""
    issues: list[str] = []
    lang = str(fingerprint.get("lang") or "").strip()
    lang_full = str(fingerprint.get("lang_full") or "").strip()
    if not lang:
        issues.append("lang must not be blank")
    elif lang_full and not lang_full.lower().startswith(lang.lower()):
        issues.append(f"lang_full {lang_full!r} does not start with lang {lang!r}")
    timezone = str(fingerprint.get("timezone") or "").strip()
    if not timezone:
        issues.append("timezone must not be blank")
    elif "/" not in timezone and timezone.upper() != "UTC":
        issues.append(f"timezone {timezone!r} is not an IANA name")
    try:
        offset = int(str(fingerprint.get("timezone_offset_minutes") or 0))
    except (TypeError, ValueError):
        offset = 0
    if not -720 <= offset <= 840:
        issues.append(f"timezone_offset_minutes={offset} is out of range")
    return issues


def validate_protocol_fingerprint(fingerprint: Mapping[str, object]) -> list[str]:
    """Return the internal contradictions of a protocol fingerprint (empty = ok).

    The protocol twin of ``browser_fingerprint_pool.validate_browser_profile``:
    it checks the invariants that cross the layers — platform vs touch points,
    platform vs screen/DPR, locale vs Accept-Language, zone vs offset.  The
    reference implementation (``sentinel-rs``) enforces the same set in
    ``DeviceFingerprint`` / ``validate_fingerprint``, because each one is a value
    the server receives in the Sentinel proof and can check for free.

    Diagnostic only: callers log the result and never abort.  A hard failure here
    would turn a cosmetic mismatch into a lost mailbox.
    """
    if not isinstance(fingerprint, Mapping):
        return [f"fingerprint must be a mapping, got {type(fingerprint).__name__}"]
    platform_class = _platform_class(str(fingerprint.get("impersonate") or ""))
    return [
        *_validate_platform_block(fingerprint, platform_class),
        *_validate_screen_block(fingerprint, platform_class),
        *_validate_locale_block(fingerprint),
    ]


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
            from .codex_sentinel import attach_sentinel

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
    if str(family or "").strip().lower() == "nextauth":
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
    elif str(family or "").strip().lower() == "chatgpt":
        headers.update(
            {
                # The 2026-10-23 HAR carries only `oai-client-version` on
                # chatgpt.com backend requests; the old build-number header is
                # no longer sent by the web client.
                "oai-client-version": CHATGPT_CLIENT_VERSION,
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
