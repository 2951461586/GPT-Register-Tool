"""Reference-only PayPal country catalog.

Source: PayPal country codes reference
(https://developer.paypal.com/reference/country-codes/). Proxy routing no longer
gates Checkout or Approve egress against this catalog.
"""

from __future__ import annotations

# ISO-3166-1 alpha-2 codes PayPal supports (197 entries).
PAYPAL_SUPPORTED_COUNTRIES: frozenset[str] = frozenset({
    "AD", "AE", "AG", "AI", "AL", "AM", "AO", "AR", "AT", "AU", "AW", "AZ",
    "BA", "BB", "BE", "BF", "BG", "BH", "BI", "BJ", "BM", "BN", "BO", "BR",
    "BS", "BT", "BW", "BY", "BZ", "CA", "CD", "CG", "CH", "CK", "CL", "CM",
    "CO", "CR", "CV", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC",
    "EE", "EG", "ER", "ES", "ET", "FI", "FJ", "FK", "FM", "FO", "FR", "GA",
    "GB", "GD", "GE", "GF", "GI", "GL", "GM", "GN", "GP", "GR", "GT", "GW",
    "GY", "HK", "HN", "HR", "HU", "ID", "IE", "IL", "IN", "IS", "IT", "JM",
    "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN", "KR", "KW", "KY", "KZ",
    "LA", "LC", "LI", "LK", "LS", "LT", "LU", "LV", "MA", "MC", "MD", "ME",
    "MG", "MH", "MK", "ML", "MN", "MQ", "MR", "MS", "MT", "MU", "MV", "MW",
    "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP",
    "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PL", "PM", "PN",
    "PT", "PW", "PY", "QA", "RO", "RS", "RU", "RW", "SA", "SB", "SC", "SE",
    "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SV", "SZ",
    "TC", "TD", "TG", "TH", "TJ", "TM", "TN", "TO", "TT", "TV", "TW", "TZ",
    "UA", "UG", "US", "UY", "VA", "VC", "VE", "VG", "VN", "VU", "WF", "WS",
    "YE", "YT", "ZA", "ZM", "ZW",
})

def normalize_country(value: object) -> str:
    return str(value or "").strip().upper()


def is_paypal_supported(country: object) -> bool:
    """Return True for a PayPal-supported ISO alpha-2 country code."""
    return normalize_country(country) in PAYPAL_SUPPORTED_COUNTRIES


def paypal_country_requires_validation(payment_method: object) -> bool:
    """Deprecated compatibility hook; stage countries are no longer gated."""
    return False


def validate_paypal_country(payment_method: object, country: object, *, field: str = "country") -> None:
    """Deprecated no-op retained for third-party import compatibility."""
    return None
