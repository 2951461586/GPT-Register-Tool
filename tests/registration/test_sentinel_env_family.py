"""Family-consistent Rust Sentinel VM environment (offline, zero Node.js)."""

from __future__ import annotations

from pathlib import Path

from sms_tool.sentinel.bundle import NATIVE_LIB_PATH, validate_runtime_bundle
from sms_tool.sentinel.native_ffi import probe_env

_UA = {
    "chrome": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
}
_PLATFORM = {"chrome": "Win32", "firefox": "Win32", "safari": "MacIntel"}
_VENDOR = {"chrome": "Google Inc.", "firefox": "", "safari": "Apple Computer, Inc."}


def _probe(family: str) -> dict:
    return probe_env(
        {
            "browser_family": family,
            "user_agent": _UA[family],
            "lang": "en-US",
            "languages": ["en-US"],
            "timezone": "America/New_York",
            "timezone_name": "Eastern Standard Time",
            "screen_width": 1920,
            "screen_height": 1080,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "js_heap_size_limit": 4294705152,
            "device_pixel_ratio": 1,
            "max_touch_points": 0,
            "navigator_platform": _PLATFORM[family],
            "navigator_vendor": _VENDOR[family],
            "page_url": "https://auth.openai.com/email-verification",
        }
    )


def test_chromium_keeps_its_chromium_only_globals():
    result = _probe("chrome")
    assert result["hasMemory"] is True
    assert result["hasDeviceMemory"] is True
    assert result["hasChrome"] is True
    assert result["hasUserAgentData"] is True
    assert result["hasInstallTrigger"] is False


def test_gecko_and_webkit_have_no_chromium_only_globals():
    for family in ("firefox", "safari"):
        result = _probe(family)
        assert result["hasMemory"] is False, family
        assert result["hasDeviceMemory"] is False, family
        assert result["hasChrome"] is False, family
        assert result["hasUserAgentData"] is False, family
        assert result["hasGpu"] is False, family
    assert _probe("firefox")["hasInstallTrigger"] is True
    assert _probe("safari")["hasSafari"] is True


def test_native_runtime_matches_its_pinned_digest():
    lib_path = validate_runtime_bundle()
    assert Path(lib_path) == NATIVE_LIB_PATH
    assert lib_path.is_file()
