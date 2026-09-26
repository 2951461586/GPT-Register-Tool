"""Offline contracts for deliberate iDEAL, TWINT and BLIK extractor differences.

Only pure configuration/normalization helpers run here. No payment flow,
provider session, proxy request or checkout is started.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


PROTOCOL_ROOT = Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
PROVIDERS = {
    "ideal": ("ideal", "ideal_qr_extract"),
    "twint": ("twint", "twint_extract"),
    "blik": ("blik", "blik_qr_extract"),
}
LOCALE_VARS = ("BROWSER_LOCALE", "ELEMENTS_LOCALE", "BROWSER_TIMEZONE")


@pytest.fixture(scope="module")
def extractors():
    # Use the same child-process import layout as test_extractors_contract.py.
    for directory, _ in PROVIDERS.values():
        source = str(PROTOCOL_ROOT / directory)
        if source not in sys.path:
            sys.path.insert(0, source)
    if str(PROTOCOL_ROOT) not in sys.path:
        sys.path.insert(0, str(PROTOCOL_ROOT))
    return {
        name: importlib.import_module(module)
        for name, (_, module) in PROVIDERS.items()
    }


@pytest.mark.parametrize(
    "name,mode,prefix,defaults",
    [
        ("ideal", "ideal", "IDEAL", ("nl-NL", "nl", "Europe/Amsterdam")),
        ("twint", "twint", "TWINT", ("de-CH", "de", "Europe/Zurich")),
        ("blik", "blik", "IDEAL", ("pl-PL", "pl-PL", "Europe/Warsaw")),
        ("blik", "ideal", "IDEAL", ("en-US", "en-US", "Europe/Amsterdam")),
    ],
)
def test_locale_defaults_blank_values_and_env_overrides(
    extractors, monkeypatch, name, mode, prefix, defaults,
):
    module = extractors[name]
    monkeypatch.setenv("IDEAL_PAYMENT_METHOD", mode)
    for suffix in LOCALE_VARS:
        monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    helpers = (
        module.payment_browser_locale,
        module.payment_elements_locale,
        module.payment_browser_timezone,
    )
    assert tuple(helper() for helper in helpers) == defaults

    for suffix in LOCALE_VARS:
        monkeypatch.setenv(f"{prefix}_{suffix}", "   ")
    assert tuple(helper() for helper in helpers) == defaults

    override = ("fr-FR", "fr", "Europe/Paris")
    for suffix, value in zip(LOCALE_VARS, override):
        monkeypatch.setenv(f"{prefix}_{suffix}", f"  {value}  ")
    assert tuple(helper() for helper in helpers) == override

    # BLIK's Elements fallback follows the *chosen browser locale*, unlike
    # iDEAL/TWINT's fixed defaults. It also uses IDEAL_ env names by design.
    monkeypatch.setenv(f"{prefix}_ELEMENTS_LOCALE", "")
    expected_elements = "fr-FR" if name == "blik" else defaults[1]
    assert module.payment_elements_locale() == expected_elements


@pytest.mark.parametrize(
    "name,mode,expected,default_country",
    [
        ("ideal", "ideal", {
            "NL": "EUR", "BE": "EUR", "DE": "EUR", "FR": "EUR",
            "US": "USD", "IN": "INR", "JP": "JPY", "VN": "VND",
        }, "NL"),
        ("twint", "twint", {
            "CH": "CHF", "NL": "EUR", "BE": "EUR", "DE": "EUR",
            "FR": "EUR", "US": "USD", "IN": "INR", "JP": "JPY", "VN": "VND",
        }, "CH"),
        ("blik", "blik", {
            "NL": "EUR", "BE": "EUR", "DE": "EUR", "FR": "EUR",
            "PL": "PLN", "US": "USD", "IN": "INR", "JP": "JPY",
        }, "PL"),
        ("blik", "ideal", {
            "NL": "EUR", "BE": "EUR", "DE": "EUR", "FR": "EUR",
            "PL": "PLN", "US": "USD", "IN": "INR", "JP": "JPY",
        }, "NL"),
    ],
)
def test_country_currency_tables_and_unknown_country_fallback(
    extractors, monkeypatch, name, mode, expected, default_country,
):
    module = extractors[name]
    monkeypatch.setenv("IDEAL_PAYMENT_METHOD", mode)
    assert module.COUNTRY_CURRENCY == expected
    for country, currency in expected.items():
        assert module.normalize_country(f" {country.lower()} ") == country
        assert module.currency_for_country(country) == currency
    for country in ("", "ZZ", "VN" if name == "blik" else "PL"):
        assert module.normalize_country(country) == default_country
        assert module.currency_for_country(country) == expected[default_country]


@pytest.mark.parametrize("name,four_part", [
    ("ideal", "none"), ("twint", "none"), ("blik", "user_first"),
])
def test_proxy_normalization_keeps_its_existing_shared_adapter(
    extractors, name, four_part,
):
    module = extractors[name]
    with patch.object(module, "shared_normalize_proxy_url", return_value="fixture") as shared:
        assert module.normalize_proxy_url("proxy.invalid:8080") == "fixture"
    assert shared.call_args.args == ("proxy.invalid:8080",)
    assert shared.call_args.kwargs["four_part"] == four_part


def test_proxy_four_field_behavior_remains_provider_specific(extractors, monkeypatch):
    monkeypatch.setenv("IDEAL_PROXY_DEFAULT_SCHEME", "http")
    monkeypatch.setenv("TWINT_PROXY_DEFAULT_SCHEME", "http")
    assert extractors["ideal"].normalize_proxy_url("proxy.invalid:8080") == "http://proxy.invalid:8080"
    assert extractors["twint"].normalize_proxy_url("proxy.invalid:8080") == "http://proxy.invalid:8080"
    assert (
        extractors["blik"].normalize_proxy_url("fixture_user:fixture_pw:proxy.invalid:8080")
        == "http://fixture_user:fixture_pw@proxy.invalid:8080"
    )
