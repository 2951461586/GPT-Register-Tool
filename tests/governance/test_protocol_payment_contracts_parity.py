"""Cross-language single-source lock tests for protocol payment tables.

Guards against duplicated-fact drift across Python, Rust, and C#:
- Stripe zero-decimal currencies across ``protocol_codec.py``,
  ``rust/protocol-payment-rs/src/port.rs``, and
  ``SmsWorkbench/Models/ProtocolCardPaymentModels.cs``.
- 25-region billing catalog across ``protocol_codec.BILLING_REGIONS`` and
  C# ``ProtocolCardPaymentCatalog.BillingCountries``.
- Overlapping country currency/locale/timezone consistency across
  ``protocol_codec.BILLING_REGIONS``, ``payment/wire.py::CURRENCY_MAP``, and
  ``payment/checkout_contract.py::COUNTRY_BROWSER_PROFILES``.
"""

from __future__ import annotations

import re
from pathlib import Path

from sms_tool.payment import checkout_contract, protocol_codec, wire

ROOT = Path(__file__).resolve().parents[2]
CS_MODELS = ROOT / "SmsWorkbench" / "Models" / "ProtocolCardPaymentModels.cs"
RS_PORT = ROOT / "rust" / "protocol-payment-rs" / "src" / "port.rs"


def _extract_quoted_codes(block: str) -> set[str]:
    return set(re.findall(r'"([A-Z]{3})"', block))


def test_zero_decimal_currencies_match_across_python_rust_and_csharp() -> None:
    py_codes = set(protocol_codec.ZERO_DECIMAL_CURRENCIES)
    assert "IDR" not in py_codes, "Stripe treats IDR as a 2-decimal currency (minor units = sen)"

    rs_text = RS_PORT.read_text(encoding="utf-8")
    rs_match = re.search(r"const ZERO_DECIMAL_CURRENCIES:\s*&\[&str\]\s*=\s*&\[(.*?)\];", rs_text, re.DOTALL)
    assert rs_match, "ZERO_DECIMAL_CURRENCIES missing in rust/protocol-payment-rs/src/port.rs"
    rs_codes = _extract_quoted_codes(rs_match.group(1))
    assert rs_codes == py_codes

    cs_text = CS_MODELS.read_text(encoding="utf-8")
    cs_match = re.search(r"ZeroDecimalCurrencies\s*=\s*new\([^)]*\)\s*\{(.*?)\};", cs_text, re.DOTALL)
    assert cs_match, "ZeroDecimalCurrencies missing in ProtocolCardPaymentModels.cs"
    cs_codes = _extract_quoted_codes(cs_match.group(1))
    assert cs_codes == py_codes


def test_billing_regions_match_csharp_catalog_and_shared_python_tables() -> None:
    cs_text = CS_MODELS.read_text(encoding="utf-8")
    assert "COUNTRY_PROFILES" not in cs_text, "stale symbol reference in ProtocolCardPaymentModels.cs"

    cs_pairs = re.findall(r'new\("([A-Z]{2})",\s*"([A-Z]{2})\s*\(([A-Z]{3})\)"\)', cs_text)
    assert cs_pairs, "BillingCountries entries missing in ProtocolCardPaymentModels.cs"
    cs_by_country = {code: currency for code, label_code, currency in cs_pairs if code == label_code}
    py_by_country = {code: currency for code, _name, currency, _locale, _tz in protocol_codec.BILLING_REGIONS}
    assert cs_by_country == py_by_country

    # Overlapping entries in wire.CURRENCY_MAP must agree with BILLING_REGIONS.
    for country, currency in wire.CURRENCY_MAP.items():
        if country in py_by_country:
            assert py_by_country[country] == currency, f"{country}: wire.CURRENCY_MAP != protocol_codec.BILLING_REGIONS"

    # Every BILLING_REGIONS entry must match checkout_contract.COUNTRY_BROWSER_PROFILES.
    for country, _name, _currency, locale, timezone in protocol_codec.BILLING_REGIONS:
        profile = checkout_contract.COUNTRY_BROWSER_PROFILES.get(country)
        assert profile is not None, f"{country} missing from checkout_contract.COUNTRY_BROWSER_PROFILES"
        assert profile.browser_locale == locale
        assert profile.browser_timezone == timezone
