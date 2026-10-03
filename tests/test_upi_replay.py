"""Slice tests for the UPI replay harness (``tests/upi_replay.py``).

These are the stage-level test net the pipeline split lacked: each test drives a
real ``_stage_*`` body through ``payment_replay``'s transport and asserts both the
final result and the *served request stream*.  The harness itself is pinned in
both directions -- zero diff for an identical rerun, and sensitive to a request
that changes -- so a broken normaliser cannot pass vacuously.

A full-flow recording (``UPI_REPLAY_DIR`` or ``runtime/upi_replays``) can be
dropped in later; ``test_optional_full_flow_recording`` picks it up and skips
otherwise, mirroring ``tests/test_stage2_replay_selfcheck.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import payment_replay as PR  # noqa: E402
import upi_replay as UR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INIT_URL = "https://api.stripe.com/v1/payment_pages/cs_replay_1/init"

_INIT_PAYLOAD = {
    "config_id": "cfg_replay",
    "init_checksum": "chk_replay",
    "currency": "inr",
    "total_summary": {"due": 0, "currency": "inr"},
    "payment_method_types": ["upi"],
}


def _init_exchange(cs_id: str = "cs_replay_1", payload: dict | None = None) -> dict:
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    return UR.synthetic_exchange("POST", url, status=200, body=payload or _INIT_PAYLOAD)


class FakeResponse:
    def __init__(self, status_code=200, body="{}", url=INIT_URL, headers=None):
        self.status_code = status_code
        self._body = body
        self.url = url
        self.headers = dict(headers or {"content-type": "application/json"})

    @property
    def text(self):
        return self._body

    def json(self):
        return json.loads(self._body) if self._body else {}


class FakeSession:
    """The real transport a recording wraps (never dials out)."""

    def __init__(self, responses):
        self.headers = {}
        self.cookies = {}
        self.proxies = {}
        self.trust_env = True
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method.upper(), url, kwargs))
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


# --------------------------------------------------------------------------
# the harness proves itself
# --------------------------------------------------------------------------


def test_replay_mismatch_fails_loudly():
    """A stage asking for an endpoint the recording lacks must fail, not pass."""
    cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(cursor, geo_country="IN") as handle:
        state, _ = handle.build_state()
        state.cs_id = "cs_something_else"  # makes the init URL differ
        with pytest.raises(PR.ReplayMismatch):
            handle.run_stage("stripe_init", state)


def test_replay_exhaustion_fails_loudly():
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor) as handle:
        state, _ = handle.build_state()
        with pytest.raises(PR.ReplayExhausted):
            handle.run_stage("stripe_init", state)


# --------------------------------------------------------------------------
# real stage bodies, offline
# --------------------------------------------------------------------------


def test_stripe_init_slice_replays_one_init_post():
    """Stage 2's non-oaics path is exactly one Stripe init POST."""
    cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(cursor) as handle:
        state, events = handle.build_state()
        assert handle.run_stage("stripe_init", state) is None
        assert state.init["config_id"] == "cfg_replay"
        assert state.init["init_checksum"] == "chk_replay"
        assert state.ctx["init_checksum"] == "chk_replay"
        assert state.ctx["currency"] == "inr"
        assert state.stripe is not None

    assert cursor.remaining == 0
    assert [fp[0] for fp in cursor.served] == ["POST"]
    assert cursor.served[0][1] == INIT_URL
    assert [stage for stage, _ in events] == ["stripe_init", "stripe_init"]


def test_harness_reports_zero_diff_for_a_rerun():
    """Identical code + identical recording must give identical result + stream."""
    first_cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(first_cursor) as handle:
        state, _ = handle.build_state()
        handle.run_stage("stripe_init", state)
        first_state = dict(state.init)

    second_cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(second_cursor) as handle:
        state, _ = handle.build_state()
        handle.run_stage("stripe_init", state)
        second_state = dict(state.init)

    assert first_state == second_state
    assert first_cursor.served == second_cursor.served


def test_harness_is_sensitive_to_a_request_change():
    """A different stripe_pk changes the request body -> fingerprint must differ."""
    base_cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(base_cursor) as handle:
        state, _ = handle.build_state()
        handle.run_stage("stripe_init", state)

    changed_cursor = UR.cursor_from([_init_exchange()])
    with UR.upi_replay(changed_cursor) as handle:
        state, _ = handle.build_state(stripe_pk="pk_test_CHANGED")
        handle.run_stage("stripe_init", state)

    assert base_cursor.served != changed_cursor.served, "fingerprint ignored the Stripe key change"


def test_pre_exit_is_http_free_when_the_egress_gate_passes():
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor, egress_failure=None) as handle:
        state, _ = handle.build_state()
        assert handle.run_stage("pre_exit", state) is None
    assert cursor.served == []


def test_pre_exit_returns_the_egress_failure_verbatim():
    failure = {"ok": False, "error_code": "payment_egress_contract_mismatch", "payment_method": "upi"}
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor, egress_failure=failure) as handle:
        state, _ = handle.build_state()
        assert handle.run_stage("pre_exit", state) == failure


def test_free_trial_slice_is_http_free_and_zero_due_passes():
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor) as handle:
        state, _ = handle.build_state()
        assert handle.run_stage("free_trial", state) is None
        assert state.amount == 0
        assert state.ft_status["has_free_trial"] is True
    assert cursor.served == []


def test_free_trial_slice_flags_a_nonzero_offer():
    """require_zero + no trial is the high-value negative sample (recorded before early exit)."""
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor) as handle:
        state, _ = handle.build_state(
            init={"total_summary": {"due": 199900, "currency": "inr"}, "payment_method_types": ["upi"]},
        )
        out = handle.run_stage("free_trial", state)
    assert out is not None
    assert out["ok"] is False
    assert out["error_code"] == "no_free_trial"
    assert out["amount"] == 199900


def test_verify_slice_is_http_free_and_marks_no_url():
    cursor = UR.cursor_from([])
    with UR.upi_replay(cursor) as handle:
        state, _ = handle.build_state()
        out = handle.run_stage("verify", state)
    assert out is not None
    assert out["ok"] is True
    assert out["verification"] == "no_instructions_url"
    assert out["link_verified"] is False
    assert cursor.served == []


# --------------------------------------------------------------------------
# record -> replay round trip
# --------------------------------------------------------------------------


def test_record_then_replay_round_trip(monkeypatch):
    """A recording captured from a fake transport must drive the same slice."""
    pipeline = UR._pipeline()
    fake = FakeSession([FakeResponse(200, json.dumps(_INIT_PAYLOAD), INIT_URL)])
    monkeypatch.setattr(pipeline, "_new_session", lambda *a, **k: fake)

    sink: list[dict] = []
    with patch.dict(os.environ, UR.UPI_STUB_ENV, clear=False):
        with UR.install_upi_recording(sink):
            ops = pipeline._build_upi_operations()
            state, _ = UR.build_state()
            from sms_tool.upi_link import stages

            assert stages._stage_stripe_init(ops, state) is None

    assert len(sink) == 1
    assert sink[0]["request"]["method"] == "POST"
    assert sink[0]["response"]["status_code"] == 200

    cursor = UR.cursor_from(sink)
    with UR.upi_replay(cursor) as handle:
        replayed, _ = handle.build_state()
        assert handle.run_stage("stripe_init", replayed) is None
        assert replayed.init["config_id"] == "cfg_replay"
    assert cursor.remaining == 0


# --------------------------------------------------------------------------
# operator-supplied full-flow recording
# --------------------------------------------------------------------------


def test_optional_full_flow_recording():
    """Full-flow replay when an operator-supplied recording is present; skip otherwise."""
    directory = Path(os.environ.get("UPI_REPLAY_DIR", ROOT / "runtime" / "upi_replays"))
    recordings = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not recordings:
        pytest.skip(f"no recording in {directory}; capture one to enable the full-flow gate")
    for recording in recordings:
        UR.load_recording(recording)  # must parse
