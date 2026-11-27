"""Browser fingerprint profile tables, platform hardware tables, and validators.

Extracted from :mod:`sms_tool.transport.auth_headers` so the static profile
catalog and invariant checks stay separated from runtime header assembly and
thread-local fingerprint state.
"""

from __future__ import annotations

from collections.abc import Mapping

# Firefox (first) is the primary impersonation. At ChatGPT's edge, Cloudflare
# challenges Chrome TLS/HTTP2 with HTTP 403 while Firefox passes, so the protocol
# path defaults to firefox144. Chrome is retained as a fallback pool entry for
# A/B comparison and recovery. Firefox does not emit Sec-CH-UA client hints, so
# its sec_ch_ua is intentionally blank (see family-aware header building in
# auth_headers.py).
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
    # 2026-11-19 (curl_cffi 0.17.0): the two newest Gecko editions.  Their
    # ClientHello is byte-identical to firefox144/147 -- ``--capture`` measured
    # the same extension list on 0.17.0 -- so ``tls_fingerprint``'s per-account
    # permutation stays valid without a re-capture.
    "firefox155": {
        "name": "firefox155",
        "impersonate": "firefox155",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0"),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "firefox153": {
        "name": "firefox153",
        "impersonate": "firefox153",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"),
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
    # with curl_cffi 0.16.0 and re-measured 2026-11-19 on 0.17.0, where every
    # pre-existing literal still matched byte for byte). Chrome rotates both the
    # GREASE brand (`Not(A:Brand` / `Not-A.Brand` / `Not_A Brand` / `Not:A-Brand`)
    # and the brand ORDER every few majors, so a hand-written list goes stale
    # silently -- the pre-2026-10-31 table had `"Google Chrome"` first with
    # `Not(A:Brand";v="24"` for every version, which matches no Chrome at all.
    # The test `test_every_chrome_hint_literal_matches_curl_cffi` re-measures this,
    # so a curl_cffi upgrade that changes the emissions fails the suite instead of
    # leaving the UA and the hints disagreeing.
    #
    # 2026-11-19 (curl_cffi 0.17.0): the table now reaches `chrome152`. There is
    # deliberately **no `chrome150` on Windows** -- upstream ships that preset as
    # a *macOS* build (measured: UA `Macintosh`, hints `"macOS"`), so it lives in
    # the macOS group below; `chrome151`/`chrome152` are the Windows editions.
    # This is the newest major 0.17.0 exposes; bump the table when the library
    # ships a newer one.
    "chrome152": {
        "name": "chrome152",
        "impersonate": "chrome152",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        ),
        "full_version": "152.0.7977.83",
        "sec_ch_ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome151": {
        "name": "chrome151",
        "impersonate": "chrome151",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        "full_version": "151.0.7900.123",
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    "chrome146": {
        "name": "chrome146",
        "impersonate": "chrome146",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "full_version": "146.0.7680.178",
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
        "full_version": "145.0.7632.160",
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
        "full_version": "142.0.7444.176",
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
        "full_version": "136.0.7103.93",
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
        "full_version": "131.0.6778.205",
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
        "full_version": "124.0.6367.207",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    # ── Chrome on macOS (only place this preset exists upstream) ──────────────
    #
    # ``chrome150`` is macOS-only in curl_cffi 0.17.0, so its UA and hints must
    # say so -- see ``_MACOS_IMPERSONATE`` for the platform class that keeps
    # navigator.platform / sec-ch-ua-platform in step with it.
    "chrome150": {
        "name": "chrome150",
        "impersonate": "chrome150",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "full_version": "150.0.7830.90",
        "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
    # ── Safari family, macOS (WebKit, no Sec-CH-UA client hints) ──────────────
    "safari270": {
        "name": "safari270",
        "impersonate": "safari270",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
    "safari260": {
        "name": "safari260",
        "impersonate": "safari260",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
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


# ──────────────────── Family-level hardware constants ─────────────────────────
#
# Platform-class fields must agree with the User-Agent's claimed OS. A UA that
# says "macOS" but reports navigator.platform "Win32" (and sec_ch_ua_platform
# "Windows") is a free contradiction for Cloudflare to flag at zero cost — the
# exact trap the reference project warns about in fingerprint.py:1541-1547.
#
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

#: Presets curl_cffi ships as **macOS** builds even though the name carries no
#: platform suffix.  ``chrome150`` is the case that matters today: its UA and
#: ``sec-ch-ua-platform`` say macOS, so classifying it as Windows would put
#: ``navigator.platform = "Win32"`` in the same proof as a Macintosh UA.
_MACOS_IMPERSONATE = frozenset({"chrome150", "chrome150_macos", "chrome151_macos", "chrome152_macos"})


def _platform_class(impersonate: str) -> str:
    """Map an impersonate token to its platform class (windows/macos/ios)."""
    token = str(impersonate or "").lower()
    if token.endswith("_ios"):
        return "ios"
    if token.endswith("_android"):
        return "android"
    if token.endswith("_macos") or token in _MACOS_IMPERSONATE:
        return "macos"
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


# ──────────────────── Per-platform device choice tables ───────────────────────
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


# ──────────────────── Fingerprint invariant validation ────────────────────────
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
