"""Increment 3: reopen the Checkout on a fresh exit (2026-10-01).

The reference measured that a different exit can turn a ``nonzero_due`` round
into a linked one, and that retrying is only safe while the account's
zero-eligibility has not been consumed.  These tests pin the retry gate and the
identity-preserving rotation, not the network.
"""

from __future__ import annotations

from sms_tool.upi_link import pipeline as p


def test_rounds_defaults_to_one_and_reads_config_then_env(monkeypatch):
    monkeypatch.delenv("UPI_ROUNDS", raising=False)
    assert p._upi_rounds({}) == 1
    assert p._upi_rounds({"upi": {"rounds": 3}}) == 3
    assert p._upi_rounds({"upi": {"rounds": "bad"}}) == 1
    monkeypatch.setenv("UPI_ROUNDS", "2")
    assert p._upi_rounds({}) == 2


def test_only_preconfirm_failures_are_retryable():
    for code in ("no_free_trial", "upi_not_available", "checkout_failed", "checkout_bad_response"):
        assert p._upi_round_retryable({"ok": False, "error_code": code}) is True, code
    for code in ("mandate_not_signed", "payment_chain_link", "link_unverified", "upi_provider_declined"):
        assert p._upi_round_retryable({"ok": False, "error_code": code}) is False, code
    assert p._upi_round_retryable({"ok": True}) is False
    assert p._upi_round_retryable(None) is False


def test_rotate_proxy_set_keeps_shared_proxies_on_one_exit(monkeypatch):
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: proxy + "#2")
    got = p._upi_rotate_proxy_set(("http://u@a:1", "http://u@a:1", "", None), "IN")
    assert got == ("http://u@a:1#2", "http://u@a:1#2", "", None)


def test_wrapper_reopens_the_checkout_on_a_preconfirm_failure(monkeypatch):
    seen: list[str] = []

    def fake_once(**kwargs):
        seen.append(kwargs["checkout_proxy"])
        if len(seen) == 1:
            return {"ok": False, "error_code": "no_free_trial"}
        return {"ok": True, "url": "upi://pay"}

    monkeypatch.setattr(p, "_generate_upi_qr_link_once", fake_once)
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: proxy + "#2")

    result = p.generate_upi_qr_link("at", checkout_proxy="http://u@a:1", runtime_config={"upi": {"rounds": 2}})

    assert result["ok"] is True
    assert seen == ["http://u@a:1", "http://u@a:1#2"]


def test_wrapper_never_reopens_after_a_mandate_verdict(monkeypatch):
    calls: list[int] = []

    def fake_once(**_kwargs):
        calls.append(1)
        return {"ok": False, "error_code": "mandate_not_signed"}

    monkeypatch.setattr(p, "_generate_upi_qr_link_once", fake_once)

    result = p.generate_upi_qr_link("at", checkout_proxy="http://u@a:1", runtime_config={"upi": {"rounds": 4}})

    assert result["error_code"] == "mandate_not_signed"
    assert len(calls) == 1


def test_wrapper_is_a_single_attempt_by_default(monkeypatch):
    monkeypatch.delenv("UPI_ROUNDS", raising=False)
    calls: list[int] = []

    def fake_once(**_kwargs):
        calls.append(1)
        return {"ok": False, "error_code": "no_free_trial"}

    monkeypatch.setattr(p, "_generate_upi_qr_link_once", fake_once)

    result = p.generate_upi_qr_link("at", checkout_proxy="http://u@a:1", runtime_config={})

    assert result["error_code"] == "no_free_trial"
    assert len(calls) == 1


def test_wrapper_stops_after_the_configured_rounds(monkeypatch):
    calls: list[int] = []

    def fake_once(**_kwargs):
        calls.append(1)
        return {"ok": False, "error_code": "no_free_trial"}

    monkeypatch.setattr(p, "_generate_upi_qr_link_once", fake_once)
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: proxy + "#")

    result = p.generate_upi_qr_link("at", checkout_proxy="http://u@a:1", runtime_config={"upi": {"rounds": 3}})

    assert result["error_code"] == "no_free_trial"
    assert len(calls) == 3
