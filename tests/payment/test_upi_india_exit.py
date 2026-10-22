"""Increment 1: India exit grading + checkout admission (2026-10-01).

Ported from the reference ``upi_core_local`` (``_is_india_exit`` /
``pick_india_proxy`` / ``select_checkout_capable_ip`` / ``probe_checkout_reachable``).
These are pure unit tests of the selector; the real probe is never dialed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sms_tool.upi_link import pipeline as p


def _verdict(code: int, *, blocked: bool = False) -> SimpleNamespace:
    return SimpleNamespace(http_status=code, blocked_by_cloudflare=blocked)


def test_india_exit_probe_is_off_by_default(monkeypatch):
    monkeypatch.delenv("UPI_INDIA_EXIT_PROBE", raising=False)
    assert p._upi_india_exit_probe_enabled({}) is False
    assert p._upi_india_exit_probe_enabled({"india_exit_probe": True}) is True
    monkeypatch.setenv("UPI_INDIA_EXIT_PROBE", "1")
    assert p._upi_india_exit_probe_enabled({}) is True


def test_india_exit_attempts_and_timeout_read_config_then_env(monkeypatch):
    monkeypatch.delenv("UPI_INDIA_EXIT_ATTEMPTS", raising=False)
    monkeypatch.delenv("UPI_INDIA_EXIT_TIMEOUT", raising=False)
    assert p._upi_india_exit_attempts({}) == 2
    assert p._upi_india_exit_attempts({"india_exit_attempts": 4}) == 4
    assert p._upi_india_exit_attempts({"india_exit_attempts": "bad"}) == 2
    monkeypatch.setenv("UPI_INDIA_EXIT_ATTEMPTS", "3")
    assert p._upi_india_exit_attempts({}) == 3
    assert p._upi_india_exit_timeout({"india_exit_timeout": 99}) == 20.0


def test_select_keeps_an_in_exit_that_admits_checkout(monkeypatch):
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="IN"))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(401))
    proxy = "http://u:p@exit:1"
    assert p._upi_select_india_exit(proxy, country="IN", attempts=2, timeout=5) == proxy


def test_select_rotates_on_wrong_country_then_accepts(monkeypatch):
    geo = {"http://u:p@exit:1": "DZ", "http://u:p@exit:2": "IN"}
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country=geo[proxy]))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(200))
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: "http://u:p@exit:2")
    assert p._upi_select_india_exit("http://u:p@exit:1", country="IN", attempts=2, timeout=5) == "http://u:p@exit:2"


def test_select_rotates_when_checkout_is_cloudflare_blocked(monkeypatch):
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="IN"))

    def probe(proxy, path="", timeout=0):
        return _verdict(403, blocked=True) if proxy.endswith(":1") else _verdict(400)

    monkeypatch.setattr(p, "probe_openai_edge", probe)
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: "http://u:p@exit:2")
    assert p._upi_select_india_exit("http://u:p@exit:1", country="IN", attempts=2, timeout=5) == "http://u:p@exit:2"


def test_select_does_not_touch_non_india_countries(monkeypatch):
    def boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("non-IN checkout must not probe exits")

    monkeypatch.setattr(p, "resolve_proxy_geo", boom)
    monkeypatch.setattr(p, "probe_openai_edge", boom)
    proxy = "http://u:p@exit:1"
    assert p._upi_select_india_exit(proxy, country="JP", attempts=2, timeout=5) == proxy


def test_select_returns_last_candidate_on_exhaustion(monkeypatch):
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="DZ"))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(200))
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: proxy + "x")
    assert p._upi_select_india_exit("http://u:p@exit:1", country="IN", attempts=2, timeout=5) == "http://u:p@exit:1x"


def test_exit_country_is_empty_when_geo_fails(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("geo down")

    monkeypatch.setattr(p, "resolve_proxy_geo", boom)
    assert p._upi_exit_country("http://u:p@exit:1", 5) == ""


def test_transport_failure_is_no_evidence_not_a_refusal(monkeypatch):
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(0))
    assert p._upi_exit_admits_checkout("http://u:p@exit:1", 5) is True


def test_cloudflare_block_on_all_paths_refuses(monkeypatch):
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(403, blocked=True))
    assert p._upi_exit_admits_checkout("http://u:p@exit:1", 5) is False


@pytest.mark.parametrize("attempts", [1, 2, 4])
def test_select_never_rotates_past_the_last_attempt(monkeypatch, attempts):
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="DZ"))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(200))
    monkeypatch.setattr(p, "rotate_session", lambda proxy, country: proxy + "#")
    got = p._upi_select_india_exit("E", country="IN", attempts=attempts, timeout=5)
    assert got.count("#") == attempts - 1


# ── Branches the 2026-10-22 coverage pass found unreached ────────────────────
#
# ``india_exit.py`` was at 95%: the timeout *fallback*, the rotate failure path,
# and the "rotation changed nothing" break. All three are failure-tolerance
# branches, i.e. the ones a happy-path suite never reaches and a regression
# turns into a hung rotation loop or a crash inside the selector.


def test_timeout_falls_back_to_the_env_then_to_its_default(monkeypatch):
    """``upi_cfg`` absent → ``UPI_INDIA_EXIT_TIMEOUT`` → 8.0, clamped to [3, 20]."""
    monkeypatch.delenv("UPI_INDIA_EXIT_TIMEOUT", raising=False)
    assert p._upi_india_exit_timeout({}) == 8.0
    assert p._upi_india_exit_timeout(None) == 8.0  # non-mapping config is not a crash
    monkeypatch.setenv("UPI_INDIA_EXIT_TIMEOUT", "1")
    assert p._upi_india_exit_timeout({}) == 3.0  # floor
    monkeypatch.setenv("UPI_INDIA_EXIT_TIMEOUT", "60")
    assert p._upi_india_exit_timeout({}) == 20.0  # ceiling
    monkeypatch.setenv("UPI_INDIA_EXIT_TIMEOUT", "bad")
    assert p._upi_india_exit_timeout({}) == 8.0


def test_a_raising_rotate_keeps_the_candidate_instead_of_crashing(monkeypatch):
    """A dead rotate endpoint must not fail the payment attempt by itself."""
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="DZ"))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(200))

    def boom(*_a, **_k):
        raise RuntimeError("rotate endpoint down")

    monkeypatch.setattr(p, "rotate_session", boom)
    proxy = "http://u:p@exit:1"
    assert p._upi_select_india_exit(proxy, country="IN", attempts=3, timeout=5) == proxy


@pytest.mark.parametrize("unchanged", ["same", ""])
def test_rotation_that_changes_nothing_stops_immediately(monkeypatch, unchanged):
    """A no-op rotation is the loop's stop condition, not something to retry.

    Without the break the selector would burn every attempt on one exit; the
    empty-string case matters because ``rotate_session`` returning ``""`` is how
    a provider reports "no new session available".
    """
    calls = []
    monkeypatch.setattr(p, "resolve_proxy_geo", lambda proxy, **kw: SimpleNamespace(country="DZ"))
    monkeypatch.setattr(p, "probe_openai_edge", lambda proxy, path="", timeout=0: _verdict(200))

    def rotate(proxy, country):
        calls.append(proxy)
        return proxy if unchanged == "same" else ""

    monkeypatch.setattr(p, "rotate_session", rotate)
    proxy = "http://u:p@exit:1"
    assert p._upi_select_india_exit(proxy, country="IN", attempts=4, timeout=5) == proxy
    assert len(calls) == 1, "a rotation that changes nothing must not be retried"


def test_rotate_proxy_set_preserves_identity_across_duplicate_entries(monkeypatch):
    """Three stages sharing one exit must stay one exit after rotation."""
    seen = []

    def rotate(proxy, country):
        seen.append(proxy)
        return proxy + "-rotated"

    monkeypatch.setattr(p, "rotate_session", rotate)
    shared = "http://u:p@exit:1"
    got = p.india_exit._upi_rotate_proxy_set((shared, shared, "http://u:p@exit:2", ""), "IN", p._exit_ops())
    assert got == (shared + "-rotated", shared + "-rotated", "http://u:p@exit:2-rotated", "")
    assert seen == [shared, "http://u:p@exit:2"], "each distinct exit rotates exactly once"
