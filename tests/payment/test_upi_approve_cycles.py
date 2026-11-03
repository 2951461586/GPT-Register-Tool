"""P1-2: the approve stage retries across **cycles**, not one reused session.

Before this change ``_stage_approve`` built one ChatGPT session and fired
``max_approve_attempts`` requests through it.  In the default ``reference``
shape approve reuses the *Checkout-stage* Sentinel pair, so
``_upi_apply_approve_risk`` re-pastes the same risk context instead of minting a
new one -- the stage's own comment says exactly that ("只换 UA 而沿用同一份风控
上下文……必然继续 blocked"), yet the loop kept replaying it.

The reference implementation plans ``cycles × per_cycle``
(``upi_extract.py:6791 approve_with_retry``) and rebuilds the session each cycle.
Local contract pinned here:

* ``approve_cycles`` defaults to 1, so the attempt budget is unchanged;
* every cycle gets a **new session** (fresh Sentinel);
* in ``reference`` shape a ``blocked`` verdict ends that cycle instead of
  replaying the same context;
* ``current`` shape keeps the legacy retry-within-cycle behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sms_tool.upi_link import pipeline as p

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upi_replay as UR  # noqa: E402


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Records approve posts; each instance is one 'cycle'."""

    def __init__(self, results: list[dict[str, Any]], calls: list[tuple[str, str]]) -> None:
        self._results = results
        self._calls = calls
        self.headers: dict[str, str] = {}

    def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs) -> _Resp:
        self._calls.append(("cycle", url))
        result = self._results.pop(0) if self._results else {"result": "blocked"}
        return _Resp(result)


def _run(monkeypatch, *, cycles: int, attempts: int, shape: str, results: list[dict[str, Any]]):
    calls: list[tuple[str, str]] = []
    sessions: list[_FakeSession] = []
    queue: list[dict[str, Any]] = list(results)

    def fake_session(*_args, **_kwargs):
        # Every cycle shares the same scripted answers: the queue belongs to the
        # test, not to the session object.
        session = _FakeSession(queue, calls)
        sessions.append(session)
        return session

    monkeypatch.setattr(p, "_upi_new_chatgpt_session", fake_session)
    monkeypatch.setattr(p, "_upi_apply_approve_risk", lambda *a, **k: None)
    monkeypatch.setattr(p, "_upi_dump_http", lambda *a, **k: None)
    monkeypatch.setattr(p, "_approve_backoff", lambda attempt, cap: 0.0)

    state, events = UR.build_state()
    state.approve_shape = shape
    state.max_approve_attempts = attempts
    state.approve_cycles = cycles
    state.approve_backoff_cap = 0.0
    state.confirm_data = {
        "submission_attempt": {"state": "requires_approval"},
        "checkout_session_id": "cs_1",
    }
    ops = p._build_upi_operations()
    result = UR.run_stage("approve", ops, state)
    return result, state, sessions, calls, events


def test_default_single_cycle_keeps_the_old_budget(monkeypatch):
    """cycles=1 + current shape ⇒ the legacy retry-within-cycle loop."""
    _result, state, sessions, calls, events = _run(
        monkeypatch,
        cycles=1,
        attempts=3,
        shape="current",
        results=[
            {"result": "requires_approval"},  # the /checkout/confirm probe (current shape only)
            {"result": "blocked"},
            {"result": "blocked"},
            {"result": "approved"},
        ],
    )
    assert len(sessions) == 1
    approve_calls = [call for call in calls if call[1].endswith("/approve")]
    assert len(approve_calls) == 3  # the legacy loop keeps retrying inside the cycle
    assert state.approval_ok is True
    assert not any("no fresh context left in this cycle" in message for _stage, message in events)


def test_reference_shape_stops_a_blocked_cycle_early(monkeypatch):
    """Replaying a reused Sentinel is a no-op, so do not burn 5 requests."""
    _result, state, _sessions, calls, events = _run(
        monkeypatch,
        cycles=1,
        attempts=5,
        shape="reference",
        results=[{"result": "blocked"}] * 5,
    )
    approve_calls = [call for call in calls if call[1].endswith("/approve")]
    assert len(approve_calls) == 1
    assert state.approval_blocked is True
    assert any("no fresh context left in this cycle" in message for _stage, message in events)


def test_each_cycle_rebuilds_the_session(monkeypatch):
    _result, state, sessions, calls, _events = _run(
        monkeypatch,
        cycles=3,
        attempts=3,
        shape="reference",
        results=[{"result": "blocked"}, {"result": "blocked"}, {"result": "approved"}],
    )
    assert len(sessions) == 3  # one fresh session (fresh Sentinel) per cycle
    approve_calls = [call for call in calls if call[1].endswith("/approve")]
    assert len(approve_calls) == 3
    assert state.approval_ok is True


def test_success_in_the_first_cycle_uses_one_session(monkeypatch):
    _result, state, sessions, calls, _events = _run(
        monkeypatch,
        cycles=4,
        attempts=4,
        shape="reference",
        results=[{"result": "approved"}],
    )
    assert len(sessions) == 1
    assert len([call for call in calls if call[1].endswith("/approve")]) == 1
    assert state.approval_ok is True


def test_attempt_budget_is_split_not_multiplied(monkeypatch):
    """``cycles`` must not raise the request count -- only how it is sliced."""
    _result, _state, sessions, calls, _events = _run(
        monkeypatch,
        cycles=2,
        attempts=4,
        shape="current",
        results=[{"result": "blocked"}] * 8,
    )
    assert len(sessions) == 2
    approve_calls = [call for call in calls if call[1].endswith("/approve")]
    # 2 per cycle (confirm endpoint is only called in ``current`` shape, once per cycle)
    assert len(approve_calls) == 4


def test_approve_cycles_defaults_to_one(monkeypatch):
    monkeypatch.delenv("UPI_APPROVE_CYCLES", raising=False)
    runtime = p._resolve_upi_runtime(
        access_token="at",
        proxy="http://p",
        checkout_proxy="",
        provider_proxy="",
        approve_proxy="",
        target_country=None,
        checkout_country=None,
        payment_country=None,
        require_zero=None,
        runtime_config={"upi": {"billing_regions": ["IN"]}},
        device_id="did",
        session_token="",
    )
    assert runtime.approve_cycles == 1
    monkeypatch.setenv("UPI_APPROVE_CYCLES", "3")
    runtime = p._resolve_upi_runtime(
        access_token="at",
        proxy="http://p",
        checkout_proxy="",
        provider_proxy="",
        approve_proxy="",
        target_country=None,
        checkout_country=None,
        payment_country=None,
        require_zero=None,
        runtime_config={"upi": {"billing_regions": ["IN"]}},
        device_id="did",
        session_token="",
    )
    assert runtime.approve_cycles == 3
