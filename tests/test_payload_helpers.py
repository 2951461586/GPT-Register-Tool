"""Behaviour tests for ``services/protocol-payment/common/payload_helpers.py``.

The six helpers were byte-identical copies in the iDEAL, TWINT and BLIK
extractors; these tests pin the behaviour the extractors rely on so the single
shared definition cannot drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROTOCOL_ROOT = Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
if str(_PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROTOCOL_ROOT))

from common import payload_helpers as ph  # noqa: E402


def test_stripe_browser_id_is_uuid_plus_eight_hex_and_unique():
    first, second = ph.stripe_browser_id(), ph.stripe_browser_id()
    assert len(first) == 36 + 8
    assert first != second


def test_find_session_cookie_decodes_the_cookie_value_from_any_nested_string():
    payload = {"a": ["x", {"cookie": "foo=1; __Secure-next-auth.session-token=ab%2Bc; bar=2"}]}
    assert ph.find_session_cookie(payload) == "ab+c"
    assert ph.find_session_cookie({"a": "nothing here"}) == ""


def test_normalize_access_token_passes_plain_tokens_through():
    assert ph.normalize_access_token("  tok  ") == ("tok", "")
    assert ph.normalize_access_token("") == ("", "")


def test_normalize_access_token_reads_json_session_blobs():
    raw = '{"accessToken": "at", "sessionToken": "st"}'
    assert ph.normalize_access_token(raw) == ("at", "st")


def test_normalize_access_token_falls_back_to_the_session_cookie_string():
    raw = '{"access_token": "at", "cookie": "__Secure-next-auth.session-token=cookie-value"}'
    assert ph.normalize_access_token(raw) == ("at", "cookie-value")


def test_normalize_access_token_keeps_invalid_json_as_the_token():
    assert ph.normalize_access_token("{not json") == ("{not json", "")


def test_infer_processor_entity_reads_the_query_parameter():
    payload = {"url": "https://pay.example/x?foo=1&processor_entity=openai_ie"}
    assert ph.infer_processor_entity(payload) == "openai_ie"
    assert ph.infer_processor_entity({"url": "https://pay.example/x"}) == ""


def test_payment_page_summary_drops_empty_values_and_ignores_non_dicts():
    assert ph.payment_page_summary(["not", "a", "dict"]) == {}
    summary = ph.payment_page_summary(
        {"object": "payment_page", "id": "cs_1", "status": "open", "currency": "chf", "amount": None}
    )
    assert summary["id"] == "cs_1"
    assert summary["currency"] == "chf"
    assert "amount" not in summary


def test_format_payment_summary_joins_key_value_pairs():
    assert ph.format_payment_summary({"a": 1, "b": "x"}) == "a=1, b=x"
    assert ph.format_payment_summary({}) == ""
