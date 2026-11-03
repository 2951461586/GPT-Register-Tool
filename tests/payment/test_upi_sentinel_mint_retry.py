"""A fallback-flow Sentinel token is the one the risk engine rejects.

Measured 2026-11-03 (same account, same IN exit, three consecutive runs):

    [sentinel] chatgpt_checkout Sentinel ready (bridge len=5161 so=yes)
    [checkout] checkout success: cs_live_...

    [sentinel] chatgpt_checkout mint empty; used fallback flow authorize_continue
    [checkout] checkout failed: 400 {"detail":"Our systems have detected unusual
               activity. Please try again later."}

So when the primary flow mints nothing and the header falls back to
``authorize_continue``, the next Checkout create is refused. The fix under test
is deliberately narrow: **only** on the path that was about to use a fallback
token, retry the primary flow once, then take whatever we have.

The switch (``UPI_SENTINEL_MINT_RETRY``) exists so the change can be A/B'd
against the old behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

from sms_tool.upi_link import sentinel
from sms_tool.upi_link.constants import UPI_SENTINEL_CHECKOUT_FLOW

PRIMARY = UPI_SENTINEL_CHECKOUT_FLOW


def _install(monkeypatch, answers: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Serve scripted mint results per flow and record the call order."""
    calls: list[str] = []
    queue = {flow: list(values) for flow, values in answers.items()}

    def fake_mint(*, flow, **_kwargs):
        calls.append(flow)
        pending = queue.get(flow) or [{}]
        return dict(pending.pop(0) if len(pending) > 1 else pending[0])

    monkeypatch.setattr(sentinel, "_upi_mint_sentinel_cached", fake_mint)
    monkeypatch.setattr(sentinel, "_upi_session_is_live", lambda _session: True)
    monkeypatch.setattr(sentinel, "_upi_session_cookie_header", lambda _s, _d: "oai-did=did")
    return calls


def _headers(**kwargs) -> dict[str, str]:
    return sentinel._upi_sentinel_headers(
        object(),
        "did-1",
        "http://exit:1",
        flow=kwargs.pop("flow", PRIMARY),
        fingerprint={"ua": "UA/1.0"},
        page_url="https://chatgpt.com/",
        **kwargs,
    )


def test_the_primary_flow_is_retried_before_a_fallback_token_is_used(monkeypatch):
    monkeypatch.delenv("UPI_SENTINEL_MINT_RETRY", raising=False)
    calls = _install(
        monkeypatch,
        {
            PRIMARY: [{}, {"main": "PRIMARY-RETRY", "so": "PRIMARY-SO"}],
            "checkout_pay": [{}],
            "authorize_continue": [{"main": "FALLBACK", "so": "FALLBACK-SO"}],
        },
    )

    headers = _headers()

    assert headers["OpenAI-Sentinel-Token"] == "PRIMARY-RETRY"
    # primary, checkout_pay, authorize_continue, then the retried primary
    assert calls == [PRIMARY, "checkout_pay", "authorize_continue", PRIMARY]


def test_the_fallback_is_still_used_when_the_retry_is_empty_too(monkeypatch):
    monkeypatch.delenv("UPI_SENTINEL_MINT_RETRY", raising=False)
    calls = _install(
        monkeypatch,
        {
            PRIMARY: [{}],
            "checkout_pay": [{}],
            "authorize_continue": [{"main": "FALLBACK", "so": "FALLBACK-SO"}],
        },
    )

    headers = _headers()

    assert headers["OpenAI-Sentinel-Token"] == "FALLBACK"
    assert calls.count(PRIMARY) == 2  # first attempt + the retry


def test_the_switch_restores_the_previous_behaviour(monkeypatch):
    monkeypatch.setenv("UPI_SENTINEL_MINT_RETRY", "0")
    calls = _install(
        monkeypatch,
        {
            PRIMARY: [{}],
            "checkout_pay": [{}],
            "authorize_continue": [{"main": "FALLBACK", "so": "FALLBACK-SO"}],
        },
    )

    headers = _headers()

    assert headers["OpenAI-Sentinel-Token"] == "FALLBACK"
    assert calls.count(PRIMARY) == 1  # no retry
    assert calls == [PRIMARY, "checkout_pay", "authorize_continue"]


def test_a_healthy_primary_mint_costs_nothing_extra(monkeypatch):
    monkeypatch.delenv("UPI_SENTINEL_MINT_RETRY", raising=False)
    calls = _install(monkeypatch, {PRIMARY: [{"main": "PRIMARY", "so": "PRIMARY-SO"}]})

    headers = _headers()

    assert headers["OpenAI-Sentinel-Token"] == "PRIMARY"
    assert calls == [PRIMARY]


@pytest.mark.parametrize("value,expected", [("", True), ("0", False), ("1", True), ("off", False), ("yes", True)])
def test_the_switch_reader(monkeypatch, value, expected):
    if value:
        monkeypatch.setenv("UPI_SENTINEL_MINT_RETRY", value)
    else:
        monkeypatch.delenv("UPI_SENTINEL_MINT_RETRY", raising=False)
    assert sentinel._upi_sentinel_mint_retry_enabled() is expected
