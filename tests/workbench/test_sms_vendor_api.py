"""The vendor catalog/balance readers the desktop now asks for over the channel (P7-B).

Both families are covered because the family decides the wire shape, and the
provider table must match `SmsProviderCatalog` on the C# side -- a vendor that
entered one side only is the drift this file prevents.
"""

from __future__ import annotations

from sms_tool.providers import sms_vendor_api as api


class _Fake:
    """Records the URLs it was asked for and answers from a script."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, session, url):
        self.urls.append(url)
        return self.responses.pop(0)

    def close(self):
        return None


def _patch_get(monkeypatch, fake):
    monkeypatch.setattr(api, "_get", fake)
    monkeypatch.setattr(api, "_session", lambda: _Fake([]))


def test_family_is_decided_by_the_provider_table_not_the_endpoint():
    assert api.family_of("smsbower") == "sms-activate"
    assert api.family_of("HEROSMS") == "sms-activate"
    assert api.family_of("nexsms") == "nexsms"
    assert api.family_of("unknown-vendor") == ""


def test_sms_activate_balance_reads_the_access_balance_line(monkeypatch):
    _patch_get(monkeypatch, _Fake(["ACCESS_BALANCE:12.34\n"]))

    result = api.read_balance("smsbower", "key", "https://smsbower.page/stubs/handler_api.php")

    assert result == {"ok": True, "balance": "12.34"}


def test_nexsms_balance_reads_the_envelope(monkeypatch):
    _patch_get(monkeypatch, _Fake(['{"code": 0, "data": {"balance": "7.50"}}']))

    result = api.read_balance("nexsms", "key", "https://api.nexsms.net")

    assert result == {"ok": True, "balance": "7.50"}


def test_a_missing_key_is_reported_not_raised():
    assert api.read_balance("smsbower", "", "https://smsbower.page")["ok"] is False
    assert api.read_catalog("nexsms", "key", "")["ok"] is False


def test_sms_activate_catalog_joins_the_price_list_and_drops_unrentable_countries(monkeypatch):
    countries = '{"38": {"eng": "Indonesia", "chn": "印度尼西亚"}, "999": {"eng": "Nowhere"}}'
    prices = '{"38": {"cost": "0.25", "count": 42}}'
    _patch_get(monkeypatch, _Fake([countries, prices]))

    result = api.read_catalog("smsbower", "key", "https://smsbower.page", "dr")

    assert result["ok"] is True
    assert [country["id"] for country in result["countries"]] == ["38"]
    assert result["countries"][0]["tiers"] == [{"price": "0.25", "count": 42}]


def test_sms_activate_catalog_falls_back_to_get_prices(monkeypatch):
    countries = '{"38": {"eng": "Indonesia"}}'
    empty_v3 = "{}"
    prices = '{"38": {"cost": "0.30", "count": 5}}'
    _patch_get(monkeypatch, _Fake([countries, empty_v3, prices]))

    result = api.read_catalog("smsbower", "key", "https://smsbower.page", "dr")

    assert result["countries"][0]["tiers"] == [{"price": "0.30", "count": 5}]


def test_nexsms_catalog_merges_names_and_price_map(monkeypatch):
    countries = '{"data": [{"id": "38", "name": "Indonesia"}]}'
    service = '{"data": [{"countryId": "38", "priceMap": {"0.40": 9}}]}'
    _patch_get(monkeypatch, _Fake([countries, service]))

    result = api.read_catalog("nexsms", "key", "https://api.nexsms.net", "dr")

    assert result["countries"] == [
        {"id": "38", "name": "Indonesia", "name_zh": "", "tiers": [{"price": "0.40", "count": 9}]}
    ]


def test_a_transport_failure_becomes_a_reported_error(monkeypatch):
    def boom(session, url):
        raise OSError("connection refused")

    monkeypatch.setattr(api, "_get", boom)
    monkeypatch.setattr(api, "_session", lambda: _Fake([]))

    result = api.read_catalog("smsbower", "key", "https://smsbower.page")

    assert result["ok"] is False
    assert "transport:" in result["error"]
