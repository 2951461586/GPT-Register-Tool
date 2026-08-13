"""PayPal-supported country catalog for early payment-country validation.

Source: PayPal country codes reference
(https://developer.paypal.com/reference/country-codes/). Used to fail fast when
a PayPal agreement checkout/approve egress country is not one PayPal actually
supports, instead of discovering it mid-protocol. Only the PayPal-family methods
(``native_paypal`` / ``native_upi``) are constrained; wallet/script methods own
their own country rules.
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

# Payment methods whose egress country must be a PayPal-supported country.
_PAYPAL_FAMILY_METHODS: frozenset[str] = frozenset({"paypal", "paypal_ba", "upi"})


def normalize_country(value: object) -> str:
    return str(value or "").strip().upper()


def is_paypal_supported(country: object) -> bool:
    """Return True for a PayPal-supported ISO alpha-2 country code."""
    return normalize_country(country) in PAYPAL_SUPPORTED_COUNTRIES


def paypal_country_requires_validation(payment_method: object) -> bool:
    """Whether the method routes through PayPal and must use a supported country."""
    return normalize_country(payment_method).lower() in _PAYPAL_FAMILY_METHODS


def validate_paypal_country(payment_method: object, country: object, *, field: str = "country") -> None:
    """Raise ``ValueError`` when a PayPal-family method targets an unsupported country.

    Blank countries pass through (defaults apply downstream) and non-PayPal
    methods are never constrained here.
    """
    code = normalize_country(country)
    if not code:
        return
    if not paypal_country_requires_validation(payment_method):
        return
    if code not in PAYPAL_SUPPORTED_COUNTRIES:
        raise ValueError(f"{field}_not_paypal_supported:{code}")
