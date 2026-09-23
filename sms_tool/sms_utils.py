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

from .desktop_ipc import progress_dots_enabled

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


# ─── Removed number source ────────────────────────────────────────────────────
#
# Until 2026-09-22 every PayPal SMS gate was fed a *static* number: a fixed
# ``phone`` plus a fixed ``sms_api_url`` pointing at an activation that already
# existed. That source was removed with the rest of the static phone-pool mode
# (it bypassed the rental lifecycle -- nothing ever completed or cancelled an
# activation), and PayPal checkout has no rental replacement yet.
#
# The danger is not that SMS stops working; it is *how* it stops. An empty
# ``api_url`` used to fall through to the poll loop: ``requests.get("")``
# raises, the baseline stays empty, and the loop spins for the full ``timeout``
# -- 120s by default -- before reporting ``sms_code_timeout``. That names the
# wrong cause: the code was never going to arrive, because nobody configured
# where to get a number. So each gate now checks *before* polling.

NO_NUMBER_SOURCE = "no_number_source"

NO_NUMBER_SOURCE_MESSAGE = (
    "PayPal checkout hit an SMS verification gate but no phone number source "
    "is configured. The static phone pool was removed on 2026-09-22 "
    "(paypal_auto.phone_numbers / paypal_auto.phone_number / sms_api_url are "
    "no longer read) and this lane has no rental replacement yet. Wire a "
    "source into sms_tool/paypal/orchestrator.py before enabling PayPal SMS."
)


def _number_source_or_none(api_url: str) -> str:
    """Return the configured ``api_url``, or ``""`` when there is none.

    Callers that only need to *warn* can branch on the empty string; callers
    that must fail use :data:`NO_NUMBER_SOURCE` as the reason code.
    """
    return str(api_url or "").strip()


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
    # Unnewlined dots glue themselves to the head of the next line another
    # thread flushes, which breaks the host's line-anchored envelope parsing.
    dots = progress_dots_enabled()

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
        if dots:
            print(f". [{attempt}/{timeout//poll_interval}]", end="", flush=True)
        time.sleep(poll_interval)

    print(f"\n[!] SMS poll timeout after {timeout}s")
    return None
