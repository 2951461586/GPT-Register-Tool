"""PayPal module browser fingerprint constants.

Centralizes User-Agent and version strings shared across paypal_auto,
paypal_reverse, paypal_protocol to avoid fingerprint drift between modules.
"""

from __future__ import annotations

# Canonical Chrome version used for PayPal browser flows.
# Keep in sync with auth_headers.py AUTH_FINGERPRINT_PROFILES (chrome136 entry).
PAYPAL_CHROME_VERSION = "136"
PAYPAL_CHROME_FULL_VERSION = "136.0.7103.93"
PAYPAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{PAYPAL_CHROME_VERSION}.0.0.0 Safari/537.36"
)
PAYPAL_SEC_CH_UA = (
    f'"Chromium";v="{PAYPAL_CHROME_VERSION}", '
    f'"Google Chrome";v="{PAYPAL_CHROME_VERSION}", '
    '"Not-A.Brand";v="99"'
)
PAYPAL_SEC_CH_UA_MOBILE = "?0"
PAYPAL_SEC_CH_UA_PLATFORM = '"Windows"'  # noqa: RUF001
