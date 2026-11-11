"""Family-consistent Sentinel environment (offline, needs node).

The vendored runner fabricates a browser environment from the profile.  Two
classes of defect lived there and are asserted here:

* Chromium-only globals leaking onto Gecko/WebKit profiles
  (``performance.memory``, ``navigator.deviceMemory``, ``window.chrome``).
* Platform-blind device measurements (an iPhone UA reporting a 1920x1080
  desktop screen) — asserted on the Python side in
  ``tests/registration/test_fingerprint_platform_consistency.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sms_tool.sentinel.bundle import RUNNER_PATH

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required by the Sentinel runner")

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


def _probe(families: list[str]) -> dict[str, dict[str, str]]:
    script = """
const runner = require(process.argv[1]);
const payload = JSON.parse(process.argv[2]);
const out = {};
for (const [family, options] of Object.entries(payload)) {
  const context = runner.createBrowserContext(options).context;
  const window = context.window || context;
  out[family] = {
    memory: typeof (window.performance || {}).memory,
    deviceMemory: typeof (window.navigator || {}).deviceMemory,
    chrome: typeof window.chrome,
  };
}
process.stdout.write(JSON.stringify(out));
"""
    payload = {
        family: {
            "userAgent": _UA[family],
            "language": "en-US",
            "languages": ["en-US"],
            "timeZone": "America/New_York",
            "timezoneName": "Eastern Standard Time",
            "screen": {
                "width": 1920,
                "height": 1080,
                "availWidth": 1920,
                "availHeight": 1040,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "jsHeapSizeLimit": 4294705152,
            "devicePixelRatio": 1,
            "maxTouchPoints": 0,
            "scriptSrc": "https://sentinel.openai.com/sentinel/probe/sdk.js",
            "navigatorPlatform": _PLATFORM[family],
            "navigatorVendor": _VENDOR[family],
            "browserFamily": family,
            "pageUrl": "https://auth.openai.com/email-verification",
            "deviceId": "11111111-1111-4111-8111-111111111111",
        }
        for family in families
    }
    completed = subprocess.run(
        ["node", "-e", script, str(RUNNER_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    return json.loads(completed.stdout)


def test_chromium_keeps_its_chromium_only_globals():
    result = _probe(["chrome"])["chrome"]
    assert result["memory"] == "object"
    assert result["deviceMemory"] == "number"
    assert result["chrome"] == "object"


def test_gecko_and_webkit_have_no_chromium_only_globals():
    # ``performance.memory`` is a Chromium extension; exposing it (and a 4 GB
    # ``jsHeapSizeLimit``) on a Firefox or Safari profile was a free
    # contradiction for any server-side environment check.
    for family in ("firefox", "safari"):
        result = _probe([family])[family]
        assert result["memory"] == "undefined", family
        assert result["deviceMemory"] == "undefined", family
        assert result["chrome"] == "undefined", family


def test_runner_is_only_compared_with_its_pinned_digest():
    # Guard against a silent re-vendor: the patched bytes must still be the ones
    # ``bundle.py`` pins (see THIRD_PARTY_NOTICES.md).
    from sms_tool.sentinel.bundle import validate_runtime_bundle

    sdk, runner = validate_runtime_bundle()
    assert Path(runner) == RUNNER_PATH
    assert sdk.is_file()
