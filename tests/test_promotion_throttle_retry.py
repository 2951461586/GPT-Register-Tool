"""Behaviour tests for the 优惠 (promotion) probe's throttling and status coding.

Covers round-3 audit P1-6:

* HTTP 429 must be retried (it is per-exit *and* short-lived), while HTTP 401
  must **not** be -- the access token is dead, so a second attempt only burns a
  proxy slot and adds latency.
* ``Retry-After`` is honoured but clamped, so a hostile/misconfigured header
  cannot park a batch run.
* The auth-failure decision must key off the machine-readable
  ``promotion.status_code``, not the Chinese display label.

Every test stubs ``time.sleep`` -- these are control-flow tests, and a real
sleep would make the suite slow and order-dependent.
"""

from __future__ import annotations

import json

import pytest

from sms_tool.accounts import account_promotion
from sms_tool.accounts.account_recovery import _promotion_auth_failure


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace ``time.sleep`` with a recorder; yields the list of requested delays."""
    delays: list[float] = []
    monkeypatch.setattr(account_promotion.time, "sleep", delays.append)
    return delays


@pytest.fixture
def one_account(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {
        "email": email,
        "access_token": "at",
        "raw_json": json.dumps({"access_token": "at"}),
    })
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)


def _refresh(monkeypatch, probe, **kwargs):
    calls: list[str | None] = []

    def fake(account, **kw):
        calls.append(kw["proxy"])
        return probe(account, calls)

    monkeypatch.setattr(account_promotion, "check_account_promotion", fake)
    result = account_promotion.refresh_promotion_statuses(["a@example.com"], workers=1, timeout=5, **kwargs)
    return result, calls


def _throttled_then_ok(probe_ok):
    def probe(_account, calls):
        if len(calls) == 1:
            return {"ok": False, "promotion_status": "HTTP 429", "error": "http_429", "status_code": 429}
        return probe_ok
    return probe


# --------------------------------------------------------------------------
# 429 retry behaviour
# --------------------------------------------------------------------------

def test_429_rotates_to_the_next_proxy_after_a_backoff(monkeypatch, no_sleep, one_account):
    """A throttled probe sleeps first, then tries a fresh exit."""
    result, calls = _refresh(
        monkeypatch,
        _throttled_then_ok({"ok": True, "promotion_status": "Free·无优惠"}),
        proxy="http://throttled.example:8080",
        proxy_pool="http://throttled.example:8080\nhttp://fresh.example:8080",
    )

    assert result["success"] == 1
    assert calls == ["http://throttled.example:8080", "http://fresh.example:8080"]
    assert no_sleep == [account_promotion.PROMOTION_THROTTLE_DEFAULT_BACKOFF]


def test_429_retries_the_same_exit_when_the_pool_is_exhausted(monkeypatch, no_sleep, one_account):
    """One candidate left: still worth one delayed retry, but only one."""
    result, calls = _refresh(
        monkeypatch,
        _throttled_then_ok({"ok": True, "promotion_status": "Free·无优惠"}),
        proxy="http://only.example:8080",
    )

    assert result["success"] == 1
    assert calls == ["http://only.example:8080", "http://only.example:8080"]
    assert len(no_sleep) == 1


def test_429_is_retried_at_most_once_per_account(monkeypatch, no_sleep, one_account):
    """A permanently throttled account must not multiply the batch duration."""
    result, calls = _refresh(
        monkeypatch,
        lambda _a, _c: {"ok": False, "promotion_status": "HTTP 429", "error": "http_429", "status_code": 429},
        proxy="http://only.example:8080",
    )

    assert result["success"] == 0
    assert len(calls) == 2
    assert len(no_sleep) == 1


def test_401_is_never_retried(monkeypatch, no_sleep, one_account):
    """A dead access token cannot be fixed by a second attempt."""
    result, calls = _refresh(
        monkeypatch,
        lambda _a, _c: {"ok": False, "promotion_status": "AT失效", "error": "token_invalid", "status_code": 401},
        proxy="http://a.example:8080",
        proxy_pool="http://a.example:8080\nhttp://b.example:8080",
    )

    assert result["unauthorized"] == 1
    assert len(calls) == 1
    assert no_sleep == []


def test_other_http_errors_are_not_retried(monkeypatch, no_sleep, one_account):
    """Only 429 is treated as throttling; e.g. 403 is a terminal answer."""
    result, calls = _refresh(
        monkeypatch,
        lambda _a, _c: {"ok": False, "promotion_status": "HTTP 403", "error": "http_403", "status_code": 403},
        proxy="http://a.example:8080",
        proxy_pool="http://a.example:8080\nhttp://b.example:8080",
    )

    assert len(calls) == 1
    assert no_sleep == []


def test_transport_errors_still_rotate_without_sleeping(monkeypatch, no_sleep, one_account):
    """Pre-existing behaviour preserved: proxy rotation only, no artificial delay."""
    def probe(_account, calls):
        if len(calls) == 1:
            return {"ok": False, "promotion_status": "检测失败", "error": "Failed to perform, curl: (28) Connection timed out"}
        return {"ok": True, "promotion_status": "Free·无优惠"}

    result, calls = _refresh(
        monkeypatch,
        probe,
        proxy="http://dead.example:8080",
        proxy_pool="http://dead.example:8080\nhttp://healthy.example:8080",
    )

    assert result["success"] == 1
    assert len(calls) == 2
    assert no_sleep == []


# --------------------------------------------------------------------------
# Retry-After handling
# --------------------------------------------------------------------------

def test_retry_after_header_is_honoured(monkeypatch, no_sleep, one_account):
    result, _calls = _refresh(
        monkeypatch,
        _throttled_then_ok({"ok": True, "promotion_status": "Free·无优惠"}),
        proxy="http://only.example:8080",
    )
    assert result["success"] == 1

    no_sleep.clear()
    _refresh(
        monkeypatch,
        lambda _a, calls: (
            {"ok": False, "promotion_status": "HTTP 429", "error": "http_429", "status_code": 429, "retry_after": "3"}
            if len(calls) == 1
            else {"ok": True, "promotion_status": "Free·无优惠"}
        ),
        proxy="http://only.example:8080",
    )
    assert no_sleep == [3.0]


def test_oversized_retry_after_is_clamped(monkeypatch, no_sleep, one_account):
    _refresh(
        monkeypatch,
        lambda _a, calls: (
            {"ok": False, "promotion_status": "HTTP 429", "error": "http_429", "status_code": 429, "retry_after": "3600"}
            if len(calls) == 1
            else {"ok": True, "promotion_status": "Free·无优惠"}
        ),
        proxy="http://only.example:8080",
    )
    assert no_sleep == [account_promotion.PROMOTION_THROTTLE_MAX_BACKOFF]


def test_garbage_retry_after_falls_back_to_the_default(monkeypatch, no_sleep, one_account):
    _refresh(
        monkeypatch,
        lambda _a, calls: (
            {"ok": False, "promotion_status": "HTTP 429", "error": "http_429", "status_code": 429, "retry_after": "soon"}
            if len(calls) == 1
            else {"ok": True, "promotion_status": "Free·无优惠"}
        ),
        proxy="http://only.example:8080",
    )
    assert no_sleep == [account_promotion.PROMOTION_THROTTLE_DEFAULT_BACKOFF]


# --------------------------------------------------------------------------
# Auth-failure detection must not key off the Chinese display label
# --------------------------------------------------------------------------

def test_auth_failure_uses_the_persisted_code_not_the_label():
    """A record with status_code 401 is an auth failure even with an unknown label."""
    assert _promotion_auth_failure({
        "promotion_status": "任何别的措辞",
        "promotion": {"status_code": "401", "status": "任何别的措辞"},
    })


def test_non_auth_code_is_not_an_auth_failure_even_with_the_legacy_label():
    """Once a real code is present it wins -- a stale label must not override it."""
    assert not _promotion_auth_failure({
        "promotion_status": "AT失效",
        "promotion": {"status_code": "200", "status": "AT失效"},
    })


def test_legacy_label_still_detected_when_no_code_was_persisted():
    """Back-compat: records written before status_code existed only have the label."""
    assert _promotion_auth_failure({"promotion_status": "AT失效", "promotion": {"status": "AT失效"}})
    assert not _promotion_auth_failure({"promotion_status": "Free·无优惠", "promotion": {"status": "Free·无优惠"}})


def test_auth_failure_handles_non_dicts():
    assert not _promotion_auth_failure(None)
    assert not _promotion_auth_failure("AT失效")
    assert not _promotion_auth_failure({})
