"""Tests for the UPI Sentinel mint reuse + flow fallback (item 2, 2026-09-30).

Two behaviours, both ported from the reference projects:

* **flow fallback** — ``chatgpt-upi-extractor`` tries
  ``authorize_continue → checkout_pay → checkout_approve``; if the primary flow
  mints empty we must try the next spelling before giving up on the Sentinel
  header.
* **mint reuse** — a Sentinel token is valid ~9 min, so re-minting the *same*
  (flow, identity, egress) is pure waste. ``tilian`` solves this with a resident
  AF_UNIX daemon; that is not portable to Windows and our vendored bridge is a
  verbatim upstream copy, so we cache the successful mint in-process instead.
"""

from __future__ import annotations

from sms_tool.upi_link import sentinel as S
from sms_tool.upi_link.constants import UPI_SENTINEL_APPROVAL_FLOW, UPI_SENTINEL_CHECKOUT_FLOW


# --------------------------------------------------------------------------
# flow fallback
# --------------------------------------------------------------------------


def test_checkout_flow_falls_back_to_checkout_pay_then_authorize_continue():
    assert S._upi_sentinel_flow_candidates(UPI_SENTINEL_CHECKOUT_FLOW) == (
        UPI_SENTINEL_CHECKOUT_FLOW,
        "checkout_pay",
        "authorize_continue",
    )


def test_approval_flow_falls_back_to_checkout_approve_then_checkout():
    assert S._upi_sentinel_flow_candidates(UPI_SENTINEL_APPROVAL_FLOW) == (
        UPI_SENTINEL_APPROVAL_FLOW,
        "checkout_approve",
        UPI_SENTINEL_CHECKOUT_FLOW,
    )


def test_unknown_flow_has_no_fallback():
    assert S._upi_sentinel_flow_candidates("custom_flow") == ("custom_flow",)


def test_headers_use_the_fallback_flow_when_the_primary_mints_empty(monkeypatch):
    seen: list[str] = []

    def fake_mint(*, flow, **kwargs):
        seen.append(flow)
        if flow == UPI_SENTINEL_CHECKOUT_FLOW:
            return {}
        return {"main": "MAIN", "so": "SO"}

    monkeypatch.setattr(S, "_upi_mint_sentinel_cached", fake_mint)
    monkeypatch.setattr(S, "_upi_session_is_live", lambda session: True)
    monkeypatch.setattr(S, "_upi_session_cookie_header", lambda session, device_id: "c=1")

    headers = S._upi_sentinel_headers(object(), "did", "proxy", flow=UPI_SENTINEL_CHECKOUT_FLOW)

    assert seen == [UPI_SENTINEL_CHECKOUT_FLOW, "checkout_pay"]
    assert headers["OpenAI-Sentinel-Token"] == "MAIN"
    assert headers["OpenAI-Sentinel-SO-Token"] == "SO"


# --------------------------------------------------------------------------
# mint reuse
# --------------------------------------------------------------------------


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(S, "_MINT_CACHE", {})


def test_successful_mint_is_reused(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = {"n": 0}

    def fake_mint(**kwargs):
        calls["n"] += 1
        return {"main": "M", "so": "S"}

    monkeypatch.setattr(S, "_upi_mint_sentinel_via_bridge", fake_mint)
    first = S._upi_mint_sentinel_cached(
        flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u"
    )
    second = S._upi_mint_sentinel_cached(
        flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u"
    )

    assert calls["n"] == 1, "the same identity must not mint twice inside the TTL"
    assert first == second == {"main": "M", "so": "S"}
    assert first is not second, "cache must hand out copies, not the stored object"


def test_a_distinct_identity_mints_again(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = {"n": 0}

    def fake_mint(**kwargs):
        calls["n"] += 1
        return {"main": "M"}

    monkeypatch.setattr(S, "_upi_mint_sentinel_via_bridge", fake_mint)
    S._upi_mint_sentinel_cached(flow="f", device_id="d1", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    S._upi_mint_sentinel_cached(flow="f", device_id="d2", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    assert calls["n"] == 2


def test_a_failed_mint_is_not_cached(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = {"n": 0}

    def fake_mint(**kwargs):
        calls["n"] += 1
        return {"error": "transient"}

    monkeypatch.setattr(S, "_upi_mint_sentinel_via_bridge", fake_mint)
    for _ in range(2):
        S._upi_mint_sentinel_cached(flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    assert calls["n"] == 2, "a failure must not poison the run; retry the next call"


def test_cache_respects_the_ttl(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = {"n": 0}

    def fake_mint(**kwargs):
        calls["n"] += 1
        return {"main": "M"}

    monkeypatch.setattr(S, "_upi_mint_sentinel_via_bridge", fake_mint)
    clock = {"t": 1000.0}
    monkeypatch.setattr(S.time, "time", lambda: clock["t"])

    S._upi_mint_sentinel_cached(flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    clock["t"] += S.UPI_SENTINEL_CACHE_TTL_SECONDS - 1
    S._upi_mint_sentinel_cached(flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    assert calls["n"] == 1

    clock["t"] += 2  # now past the TTL
    S._upi_mint_sentinel_cached(flow="f", device_id="d", proxy="p", fingerprint={}, cookie_header="c", page_url="u")
    assert calls["n"] == 2


def test_cache_ttl_is_under_server_validity():
    """A Sentinel token lives ~9 min; the cache must expire before that."""
    assert 0 < S.UPI_SENTINEL_CACHE_TTL_SECONDS < 540
