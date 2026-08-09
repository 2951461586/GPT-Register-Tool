"""Common OTP polling loop with settle-stability detection.

Shared template used by _poll_email_otp, _poll_chongzhi_otp and
mailbox_cfworker polling to avoid duplicating the deadline/interval/settle
logic across providers.
"""

from __future__ import annotations

import time
import logging
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Default configuration fallbacks (overridable via config).
_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_SETTLE_SECONDS = 1.5


def _poll_otp_with_settle(
    fetch_candidate: Callable[[], Optional[dict]],
    *,
    timeout: int = 300,
    interval: Optional[float] = None,
    settle_seconds: Optional[float] = None,
    excluded_otps: Iterable | None = None,
    log_prefix: str = "poll",
    is_newer: Callable[[Optional[dict], Optional[dict]], bool] | None = None,
    reraise: tuple[type[Exception], ...] | None = None,
) -> Optional[str]:
    """Poll for an OTP with settle-stability detection.

    Repeatedly calls ``fetch_candidate()`` until it returns a candidate dict
    with an ``otp`` key.  Once a candidate is found, waits ``settle_seconds``
    to confirm no newer OTP arrives (settle-stability pattern).

    Args:
        fetch_candidate: Callable that returns a dict with at least ``otp`` or None.
        timeout: Total deadline in seconds.
        interval: Sleep between polls (defaults to configured _DEFAULT_POLL_INTERVAL).
        settle_seconds: How long to wait for stability before returning.
        excluded_otps: Iterable of OTP strings to skip.
        log_prefix: Prefix for log messages.
        is_newer: Callable(newer, older) -> bool; defaults to comparing
            ``received_ts`` keys.
        reraise: Exception types to re-raise instead of swallowing.

    Returns:
        The OTP string, or None on timeout.
    """
    if interval is None:
        interval = _DEFAULT_POLL_INTERVAL
    if settle_seconds is None:
        settle_seconds = _DEFAULT_SETTLE_SECONDS
    if is_newer is None:
        def is_newer(a, b):
            return (a or {}).get("received_ts", 0) > (b or {}).get("received_ts", 0)

    excluded = {str(value or "").strip() for value in (excluded_otps or ())}
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            candidate = fetch_candidate()
            if candidate and candidate.get("otp") not in excluded:
                stable_until = time.time() + settle_seconds
                while settle_seconds > 0 and time.time() < stable_until and time.time() < deadline:
                    time.sleep(min(interval, max(0.0, stable_until - time.time())))
                    newer = fetch_candidate()
                    if is_newer(newer, candidate):
                        candidate = newer
                        stable_until = time.time() + settle_seconds
                otp_code = str(candidate.get("otp") or "")
                if otp_code:
                    print(f" code:{otp_code}!")
                    return otp_code
        except reraise or ():
            raise
        except Exception as e:
            print(f"[{log_prefix} error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None
