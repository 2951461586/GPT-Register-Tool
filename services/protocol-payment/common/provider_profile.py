"""Provider data profile for the protocol-payment extractors (stage 2, S2).

The 2026-09-28 audit (``docs/audits/plan-2026-09-28-stage2-replay.md``) showed
that most of the 23 functions where ``ideal`` and ``twint`` "differ" differ only
in **provider data** or **log wording**, not in control flow.  This module owns
the data half: the country/currency table, locale defaults, unavailable-error
markers and the random billing pools.

Design
------
Only *data* and *pure lookups* live here.  Anything that performs I/O, reads a
session or touches state stays in the extractor (and is passed in where the
shared code needs it).  ``env_bool`` is injected into :func:`billing_profile`
because each extractor keeps its own env parser.

The profiles are plain frozen dataclasses, so a provider's whole data surface is
one object a reviewer can diff.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

__all__ = [
    "IDEAL_PROFILE",
    "TWINT_PROFILE",
    "ProviderProfile",
    "billing_profile",
    "build_email",
    "currency_for_country",
    "is_unavailable_error",
    "normalize_country",
    "payment_browser_locale",
    "payment_browser_timezone",
    "payment_elements_locale",
]

#: Shared across the payment providers (byte-identical in ideal / twint / blik).
#: NOTE: ideal's table deliberately has no ``CH`` entry (``normalize_country``
#: falls back to ``NL``), while twint adds ``CH -> CHF``.  The maps are therefore
#: per-profile, not one shared constant.
COUNTRY_CURRENCY: dict[str, str] = {
    "NL": "EUR",
    "BE": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "US": "USD",
    "IN": "INR",
    "JP": "JPY",
    "VN": "VND",
}

TWINT_COUNTRY_CURRENCY: dict[str, str] = {"CH": "CHF", **COUNTRY_CURRENCY}

EMAIL_DOMAINS: tuple[str, ...] = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")


@dataclass(frozen=True)
class ProviderProfile:
    """One payment provider's data surface."""

    name: str
    unavailable_error: str
    unavailable_checkout_marker: str
    normalize_fallback: str
    currency_fallback: str
    browser_locale: tuple[str, str]  # (env name, default)
    elements_locale: tuple[str, str]
    browser_timezone: tuple[str, str]
    billing_names: tuple[tuple[str, str], ...]
    billing_addresses: tuple[tuple[str, str, str], ...]
    billing_default: Mapping[str, str]
    billing_fixed_env: str
    billing_env_map: Mapping[str, str]
    #: Redirect host the provider is reached through (bare domain; both the
    #: exact host and any subdomain of it count as redirect-like).
    redirect_domain: str
    #: Lowercase token that marks a redirect URL as belonging to this provider.
    redirect_marker: str
    email_domains: tuple[str, ...] = EMAIL_DOMAINS
    country_currency: Mapping[str, str] = field(default_factory=lambda: COUNTRY_CURRENCY)


IDEAL_PROFILE = ProviderProfile(
    name="ideal",
    unavailable_error="当前账号支付方式不支持 iDEAL",
    unavailable_checkout_marker="当前 checkout 不支持 iDEAL",
    normalize_fallback="NL",
    currency_fallback="EUR",
    browser_locale=("IDEAL_BROWSER_LOCALE", "nl-NL"),
    elements_locale=("IDEAL_ELEMENTS_LOCALE", "nl"),
    browser_timezone=("IDEAL_BROWSER_TIMEZONE", "Europe/Amsterdam"),
    billing_names=(
        ("Daan", "de Vries"),
        ("Sem", "Jansen"),
        ("Milan", "Bakker"),
        ("Lars", "Visser"),
        ("Sophie", "Smit"),
        ("Emma", "Meijer"),
        ("Tess", "Mulder"),
        ("Nina", "de Boer"),
    ),
    billing_addresses=(
        ("Damrak 1", "Amsterdam", "1012 LG"),
        ("Kalverstraat 92", "Amsterdam", "1012 PH"),
        ("Coolsingel 40", "Rotterdam", "3011 AD"),
        ("Lijnbaan 50", "Rotterdam", "3012 EP"),
        ("Grote Marktstraat 43", "Den Haag", "2511 BH"),
        ("Oudegracht 120", "Utrecht", "3511 AW"),
        ("Stationsplein 1", "Eindhoven", "5611 AB"),
        ("Vismarkt 10", "Groningen", "9711 KV"),
    ),
    billing_default={
        "email": "redacted@example.invalid",
        "name": "Daan de Vries",
        "country": "NL",
        "line1": "Damrak 1",
        "line2": "",
        "city": "Amsterdam",
        "postal_code": "1012 LG",
        "state": "",
    },
    billing_fixed_env="IDEAL_USE_FIXED_BILLING",
    billing_env_map={
        "email": "IDEAL_EMAIL",
        "name": "IDEAL_NAME",
        "country": "IDEAL_BILLING_COUNTRY",
        "line1": "IDEAL_LINE1",
        "line2": "IDEAL_LINE2",
        "city": "IDEAL_CITY",
        "postal_code": "IDEAL_POSTAL_CODE",
        "state": "IDEAL_STATE",
    },
    redirect_domain="ideal.nl",
    redirect_marker="ideal",
)

TWINT_PROFILE = ProviderProfile(
    name="twint",
    unavailable_error="当前账号支付方式不支持 TWINT",
    unavailable_checkout_marker="当前 checkout 不支持 TWINT",
    normalize_fallback="CH",
    currency_fallback="CHF",
    browser_locale=("TWINT_BROWSER_LOCALE", "de-CH"),
    elements_locale=("TWINT_ELEMENTS_LOCALE", "de"),
    browser_timezone=("TWINT_BROWSER_TIMEZONE", "Europe/Zurich"),
    billing_names=(
        ("Alex", "Meyer"),
        ("Lea", "Keller"),
        ("Noah", "Fischer"),
        ("Mia", "Weber"),
        ("Luca", "Schmid"),
    ),
    billing_addresses=(
        ("Bahnhofstrasse 1", "Zurich", "8001"),
        ("Aeschenplatz 2", "Basel", "4052"),
        ("Rue du Rhone 50", "Geneva", "1204"),
        ("Bahnhofplatz 1", "Bern", "3011"),
    ),
    billing_default={
        "email": "redacted@example.invalid",
        "name": "Alex Meyer",
        "country": "CH",
        "line1": "Bahnhofstrasse 1",
        "line2": "",
        "city": "Zurich",
        "postal_code": "8001",
        "state": "",
    },
    billing_fixed_env="TWINT_USE_FIXED_BILLING",
    billing_env_map={
        "email": "TWINT_EMAIL",
        "name": "TWINT_NAME",
        "country": "TWINT_BILLING_COUNTRY",
        "line1": "TWINT_LINE1",
        "line2": "TWINT_LINE2",
        "city": "TWINT_CITY",
        "postal_code": "TWINT_POSTAL_CODE",
        "state": "TWINT_STATE",
    },
    redirect_domain="twint.ch",
    redirect_marker="twint",
    country_currency=TWINT_COUNTRY_CURRENCY,
)


def _env(pair: tuple[str, str]) -> str:
    name, default = pair
    return os.environ.get(name, default).strip() or default


def normalize_country(profile: ProviderProfile, country: str) -> str:
    value = str(country or "").strip().upper()
    return value if value in profile.country_currency else profile.normalize_fallback


def currency_for_country(profile: ProviderProfile, country: str) -> str:
    return profile.country_currency.get(normalize_country(profile, country), profile.currency_fallback)


def payment_browser_locale(profile: ProviderProfile) -> str:
    return _env(profile.browser_locale)


def payment_elements_locale(profile: ProviderProfile) -> str:
    return _env(profile.elements_locale)


def payment_browser_timezone(profile: ProviderProfile) -> str:
    return _env(profile.browser_timezone)


def is_unavailable_error(profile: ProviderProfile, value) -> bool:
    text = str(value or "")
    return profile.unavailable_error in text or profile.unavailable_checkout_marker in text


def build_email(profile: ProviderProfile, first_name: str, last_name: str) -> str:
    first = re.sub(r"[^a-z]", "", first_name.lower())
    last = re.sub(r"[^a-z]", "", last_name.lower())
    suffix = random.randint(10000, 999999)
    domain = random.choice(profile.email_domains)
    if random.random() < 0.5:
        local = f"{first}.{last}{suffix}"
    else:
        local = f"{first}{last}{suffix}"
    return f"{local}@{domain}"


def billing_profile(profile: ProviderProfile, *, env_bool: Callable[[str, bool], bool]) -> dict[str, str]:
    first_name, last_name = random.choice(profile.billing_names)
    line1, city, postal_code = random.choice(profile.billing_addresses)
    result = {
        "email": build_email(profile, first_name, last_name),
        "name": f"{first_name} {last_name}",
        "country": profile.normalize_fallback,
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal_code,
        "state": "",
    }
    if env_bool(profile.billing_fixed_env, False):
        result = dict(profile.billing_default)
    for key, env_name in profile.billing_env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            result[key] = value
    result["country"] = normalize_country(profile, result.get("country", profile.normalize_fallback))
    return result
