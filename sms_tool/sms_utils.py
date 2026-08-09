"""Shared SMS polling and extraction utilities.

Extracted from ``paypal_auto.py`` to resolve the circular import between
``paypal_auto`` and ``paypal_reverse``:  paypal_reverse imports SMS helpers
from paypal_auto, while paypal_reverse was transitively pulled in by
paypal_auto.  Both modules now import from this neutral shared module.
"""

from __future__ import annotations

import re
import time

import requests as _requests

# ─── SMS code extraction ──────────────────────────────────────────────────────


def _extract_sms_code(text: str) -> str | None:
    """Extract verification code from SMS text, avoiding false positives."""
    if not text:
        return None

    keyword_patterns = [
        re.compile(r"(?:code|otp|verification|verify)[:\s]+(\d{4,6})", re.IGNORECASE),
        re.compile(r"(?:is|:)\s*(\d{4,6})\s*(?:for|to|\.|$)", re.IGNORECASE),
    ]
    for pattern in keyword_patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)

    standalone_pattern = re.compile(r"(?<![0-9-])(?<!20[0-9]{2})(\d{4,6})(?![0-9-])")
    match = standalone_pattern.search(text)
    if match:
        code = match.group(1)
        if 2000 <= int(code) <= 2099 and len(code) == 4:
            return None
        return code

    return None


# ─── SMS API polling ──────────────────────────────────────────────────────────


def _sms_baseline(api_url: str) -> dict:
    """Record the current SMS state as baseline before starting."""
    result = {"raw": "", "timestamp": 0}
    try:
        r = _requests.get(api_url, timeout=10)
        if r.status_code == 200:
            result["raw"] = r.text.strip()
            result["timestamp"] = time.time()
    except Exception:
        pass
    return result


def _poll_sms_code(
    api_url: str,
    baseline: dict,
    timeout: int = 120,
    poll_interval: int = 5,
) -> str | None:
    """Poll SMS API for a new verification code."""
    deadline = time.time() + timeout
    baseline_raw = baseline.get("raw", "")
    attempt = 0

    print(f"[*] Polling SMS (timeout={timeout}s, interval={poll_interval}s)...")

    while time.time() < deadline:
        attempt += 1
        try:
            r = _requests.get(api_url, timeout=10)
            if r.status_code == 200:
                text = r.text.strip()

                if text and text != baseline_raw:
                    code = _extract_sms_code(text)
                    if code:
                        print(f"\n[*] SMS code received (content change): {code}")
                        return code

                if text:
                    code = _extract_sms_code(text)
                    if code and attempt > 2:
                        if not hasattr(_poll_sms_code, '_last_seen') or _poll_sms_code._last_seen != text:
                            _poll_sms_code._last_seen = text
                            print(f"\n[*] SMS code received (new message): {code}")
                            return code

        except Exception as e:
            print(f"[sms poll error: {e}]")

        remaining = int(deadline - time.time())
        print(f". [{attempt}/{timeout//poll_interval}]", end="", flush=True)
        time.sleep(poll_interval)

    print(f"\n[!] SMS poll timeout after {timeout}s")
    return None
