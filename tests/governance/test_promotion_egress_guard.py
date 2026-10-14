"""Behaviour tests for the promotion batch's whole-batch egress guards.

Three guards, added after the 2026-09-29 incident where a wholly dead proxy
pool turned a 15-account run into ~95 silent minutes:

1. **Preflight** -- one probe per sampled candidate before any account is
   touched. A pool where every candidate is a transport failure aborts the batch
   in seconds instead of walking every account through every candidate.
2. **Circuit breaker** -- N consecutive *transport* failures trip the batch and
   the remaining accounts are parked. A per-account verdict (401 / an HTTP
   reply) proves the environment is alive and must not trip it.
3. **Per-candidate progress** -- a ``proxy_attempt`` event per candidate, so the
   UI shows rotation instead of freezing until the account ends.

``tests/conftest.py::_no_real_promotion_preflight`` neutralises the preflight's
network by default; these tests re-patch ``promotion_batch.probe_openai_edge``
in the body (their monkeypatch runs after the fixture, so it wins).
"""

from __future__ import annotations

import json

import pytest

from sms_tool.accounts import promotion_batch
from sms_tool.proxy_edge_probe import BLOCKED, CLEAN, DEAD, EdgeVerdict


def _verdict(status: str) -> EdgeVerdict:
    return EdgeVerdict(proxy="***:***", status=status)


def _transport_failure(_account, **_kwargs):
    return {
        "ok": False,
        "promotion_status": "检测失败",
        "error": "Failed to perform, curl: (28) Connection timed out",
    }


@pytest.fixture
def one_account(monkeypatch):
    monkeypatch.setattr(promotion_batch, "CFG", {})
    monkeypatch.setattr(
        "sms_tool.storage.get_account_record",
        lambda email: {
            "email": email,
            "access_token": "at",
            "raw_json": json.dumps({"access_token": "at"}),
        },
    )
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)


@pytest.fixture
def five_accounts(monkeypatch):
    emails = [f"a{index}@example.com" for index in range(5)]
    monkeypatch.setattr(promotion_batch, "CFG", {})
    monkeypatch.setattr(
        "sms_tool.storage.get_account_record",
        lambda email: {
            "email": email,
            "access_token": "at",
            "raw_json": json.dumps({"access_token": "at"}),
        },
    )
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    return emails


def _events(monkeypatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        promotion_batch, "_emit_account_batch_event", lambda *args, **kwargs: captured.append((args, kwargs))
    )
    return captured


# --------------------------------------------------------------------------
# Guard 1: preflight
# --------------------------------------------------------------------------


def test_all_dead_pool_aborts_before_any_account(monkeypatch, one_account):
    probed: list[str] = []
    monkeypatch.setattr(promotion_batch, "probe_openai_edge", lambda proxy, **kwargs: _verdict(DEAD))
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: probed.append(account["email"]) or {"ok": True},
    )
    events = _events(monkeypatch)

    result = promotion_batch.refresh_promotion_statuses(
        ["a@example.com"], workers=1, proxy="http://dead.example:8080", payment_eligibility=False
    )

    assert result["ok"] is False
    assert result["error_code"] == "egress_pool_unavailable"
    assert result["results"] == []
    assert result["requested"] == 1
    assert probed == [], "no account may be probed once the pool is known dead"
    assert any(args[1] == "batch_aborted" for args, _ in events)


def test_blocked_edge_is_still_usable_and_the_batch_proceeds(monkeypatch, one_account):
    """A Cloudflare 403 at the login edge proves the proxy carried the request."""
    monkeypatch.setattr(promotion_batch, "probe_openai_edge", lambda proxy, **kwargs: _verdict(BLOCKED))
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"},
    )

    result = promotion_batch.refresh_promotion_statuses(
        ["a@example.com"], workers=1, proxy="http://edge.example:8080", payment_eligibility=False
    )

    assert result["success"] == 1
    assert "error_code" not in result


def test_direct_egress_never_probes(monkeypatch, one_account):
    def _boom(*args, **kwargs):
        raise AssertionError("preflight must not probe when there is no proxy pool")

    monkeypatch.setattr(promotion_batch, "probe_openai_edge", _boom)
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"},
    )

    result = promotion_batch.refresh_promotion_statuses(["a@example.com"], workers=1, payment_eligibility=False)

    assert result["success"] == 1


def test_preflight_samples_up_to_the_cap_and_stops_at_the_first_alive(monkeypatch, one_account):
    seen: list[str] = []

    def probe(proxy, **kwargs):
        seen.append(proxy)
        return _verdict(DEAD if "dead" in proxy else CLEAN)

    monkeypatch.setattr(promotion_batch, "probe_openai_edge", probe)
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"},
    )

    result = promotion_batch.refresh_promotion_statuses(
        ["a@example.com"],
        workers=1,
        payment_eligibility=False,
        proxy_pool="http://dead1.example:1\nhttp://dead2.example:1\nhttp://live.example:1\nhttp://dead3.example:1",
    )

    assert result["success"] == 1
    assert seen == ["http://dead1.example:1", "http://dead2.example:1", "http://live.example:1"]


# --------------------------------------------------------------------------
# Guard 2: circuit breaker
# --------------------------------------------------------------------------


def test_consecutive_transport_failures_trip_and_park_the_rest(monkeypatch, five_accounts):
    monkeypatch.setattr(promotion_batch, "check_account_promotion", _transport_failure)

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is True
    parked = [item for item in result["results"] if item.get("skipped")]
    assert len(parked) == 2, "threshold 3 leaves 2 of 5 accounts parked"
    assert all(item["promotion_status"] == "批次已熔断" for item in parked)
    assert result["transport_failed"] == 3


def test_unauthorized_failures_never_trip_the_breaker(monkeypatch, five_accounts):
    """A dead access token is a per-account verdict, not a dead environment."""
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {
            "ok": False,
            "promotion_status": "AT失效",
            "error": "token_invalid",
            "status_code": 401,
        },
    )

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is False
    assert result["unauthorized"] == 5
    assert not any(item.get("skipped") for item in result["results"])


def test_http_reply_failures_never_trip_the_breaker(monkeypatch, five_accounts):
    """An HTTP answer means the tunnel works, so the consecutive count resets."""
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {
            "ok": False,
            "promotion_status": "HTTP 403",
            "error": "http_403",
            "status_code": 403,
        },
    )

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is False
    assert not any(item.get("skipped") for item in result["results"])


def test_a_success_resets_the_consecutive_count(monkeypatch, five_accounts):
    """Two transport failures then a success must not leave the breaker primed."""
    outcomes = iter(
        [
            _transport_failure(None),
            _transport_failure(None),
            {"ok": True, "promotion_status": "Free·无优惠"},
            _transport_failure(None),
            _transport_failure(None),
        ]
    )
    monkeypatch.setattr(promotion_batch, "check_account_promotion", lambda account, **kwargs: next(outcomes))

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is False
    assert result["success"] == 1


def test_breaker_threshold_is_configurable(monkeypatch, five_accounts):
    monkeypatch.setattr(promotion_batch, "CFG", {"registration": {"batch_circuit_breaker_threshold": 2}})
    monkeypatch.setattr(promotion_batch, "check_account_promotion", _transport_failure)

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is True
    assert sum(1 for item in result["results"] if item.get("skipped")) == 3


def test_breaker_can_be_disabled(monkeypatch, five_accounts):
    monkeypatch.setattr(promotion_batch, "CFG", {"registration": {"batch_circuit_breaker_enabled": False}})
    monkeypatch.setattr(promotion_batch, "check_account_promotion", _transport_failure)

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["circuit_breaker_tripped"] is False
    assert not any(item.get("skipped") for item in result["results"])


def test_parked_accounts_are_not_counted_as_persist_failures(monkeypatch, five_accounts):
    monkeypatch.setattr(promotion_batch, "check_account_promotion", _transport_failure)

    result = promotion_batch.refresh_promotion_statuses(
        five_accounts, workers=1, proxy="http://only.example:8080", payment_eligibility=False
    )

    assert result["persist_failed"] == 0, "a parked account never attempted a save"


# --------------------------------------------------------------------------
# Guard 3: per-candidate progress
# --------------------------------------------------------------------------


def test_each_candidate_emits_a_progress_event(monkeypatch, one_account):
    monkeypatch.setattr(promotion_batch, "check_account_promotion", _transport_failure)
    events = _events(monkeypatch)

    promotion_batch.refresh_promotion_statuses(
        ["a@example.com"],
        workers=1,
        payment_eligibility=False,
        proxy_pool="http://a.example:1\nhttp://b.example:1\nhttp://c.example:1",
    )

    details = [kwargs["detail"] for args, kwargs in events if args[1] == "proxy_attempt"]
    assert details == ["代理 1/3", "代理 2/3", "代理 3/3"]
    assert all(kwargs["total"] == 1 for args, kwargs in events if args[1] == "proxy_attempt")


# --------------------------------------------------------------------------
# Failure-class mapping
# --------------------------------------------------------------------------


def test_transport_is_the_only_environment_failure_class():
    assert (
        promotion_batch._promotion_env_failure_class(
            {"ok": False, "probe": {"ok": False, "error": "curl: (28) timed out"}}
        )
        == "network"
    )
    assert (
        promotion_batch._promotion_env_failure_class({"ok": False, "probe": {"ok": False, "status_code": 401}})
        == "account"
    )
    assert (
        promotion_batch._promotion_env_failure_class({"ok": False, "probe": {"ok": False, "status_code": 403}})
        == "account"
    )
