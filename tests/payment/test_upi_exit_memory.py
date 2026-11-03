"""P1-1 / P1-3: the two written-but-never-read caches.

* **P1-1** -- ``_upi_record_zero_result`` stored "this exit produced amount=N"
  on every run, and the UPI lane never read it back: the next account handed the
  same exit still created a Checkout to discover the same thing.  (The *pool*
  selector ``paypal.proxy.select_proxy_from_pool`` did consume it, so only the
  single-credential path and the CLI were blind.)
* **P1-3** -- a ``blocked`` verdict is a property of the exit.  ``_stage_approve``
  concluded "provider risk control" and then forgot it; the next run started
  from zero.

Both fixes are deliberately conservative: the cached facts are always *read and
reported*, but they only *change control flow* behind an explicit switch
(``UPI_ZERO_CACHE_SCHEDULING`` / ``UPI_EXIT_HEALTH_SKIP``), because the UPI lane
receives one credential and cannot choose a different exit itself.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from sms_tool.payment import upi_exit_health as exit_health
from sms_tool.payment import upi_identity_store as identity_store
from sms_tool.upi_link import pipeline, stage_config
from sms_tool.upi_link.operations import UpiOperations
from sms_tool.upi_link.stages import _stage_checkout

# ``tests/upi_replay.py`` is imported by filename (see ``test_upi_replay.py``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upi_replay as UR  # noqa: E402

build_state = UR.build_state


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "identity"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(identity_store.STORE_DIR_ENV, str(directory))
    for name in ("UPI_ZERO_CACHE_SCHEDULING", "UPI_EXIT_HEALTH_SKIP", "UPI_EXIT_HEALTH"):
        monkeypatch.delenv(name, raising=False)
    return directory


class _ZeroState:
    """Minimal ``PayPalProxyState`` surface the lane reads."""

    def __init__(self, status: str = "", amount: int | None = None) -> None:
        self._status = status
        self._amount = amount
        self.seen: list[tuple[str, str]] = []

    def zero_status(self, proxy: str, country: str):
        self.seen.append((proxy, country))
        return self._status, self._amount


# ── P1-1: the 0-元 cache is now read ────────────────────────────────────────
class _ProceededToStage1(RuntimeError):
    """Raised by the stub transport: proves the zero-cache gate let the run through."""


class _GateOps:
    """Just enough ``UpiOperations`` to reach the 0-元 gate and no further."""

    @staticmethod
    def redact_proxy_text(text, _proxy=None):  # noqa: ARG004 - ops contract
        return "PROXY"

    @staticmethod
    def _upi_new_chatgpt_session(*_args, **_kwargs):
        raise _ProceededToStage1("stage 1 reached")


def _gate_ops() -> UpiOperations:
    """Duck-typed stub, cast so the intent ("stop at the gate") stays typed."""
    return cast(UpiOperations, _GateOps())


def test_zero_cache_is_read_for_todays_exit():
    proxy_state = _ZeroState("bad", 199900)
    state, events = build_state(proxy_state=proxy_state)
    with pytest.raises(_ProceededToStage1):
        _stage_checkout(_gate_ops(), state)
    assert proxy_state.seen == [(state.checkout_proxy, "IN")]
    assert any("0 元缓存" in message for _stage, message in events)


def test_zero_cache_bad_exit_is_refused_when_scheduling_is_on():
    state, _events = build_state(proxy_state=_ZeroState("bad", 199900), upi_cfg={"zero_cache_scheduling": True})
    result = _stage_checkout(_gate_ops(), state)
    assert result is not None
    assert result["error_code"] == "zero_cache_bad_exit"
    assert result["amount"] == 199900


def test_zero_cache_ok_exit_is_never_refused():
    state, _events = build_state(proxy_state=_ZeroState("ok", 0), upi_cfg={"zero_cache_scheduling": True})
    with pytest.raises(_ProceededToStage1):
        _stage_checkout(_gate_ops(), state)


def test_zero_cache_is_ignored_for_the_original_price_chain():
    """``require_zero=False`` means a non-zero amount is the expected answer."""
    state, _events = build_state(
        proxy_state=_ZeroState("bad", 199900),
        upi_cfg={"zero_cache_scheduling": True},
        require_zero=False,
    )
    with pytest.raises(_ProceededToStage1):
        _stage_checkout(_gate_ops(), state)


def test_zero_cache_read_tolerates_a_broken_state_object():
    class Broken:
        def zero_status(self, proxy, country):
            raise RuntimeError("boom")

    state, events = build_state(proxy_state=Broken())
    with pytest.raises(_ProceededToStage1):
        _stage_checkout(_gate_ops(), state)
    assert not any("0 元缓存" in message for _stage, message in events)


def test_zero_cache_bad_exit_reopens_on_a_fresh_exit():
    assert stage_config._upi_round_retryable({"ok": False, "error_code": "zero_cache_bad_exit"}) is True


def test_zero_cache_switches_default_off_and_read_config_first(monkeypatch):
    assert stage_config._upi_zero_cache_scheduling({}) is False
    assert stage_config._upi_zero_cache_scheduling({"zero_cache_scheduling": True}) is True
    monkeypatch.setenv("UPI_ZERO_CACHE_SCHEDULING", "1")
    assert stage_config._upi_zero_cache_scheduling({}) is True
    assert stage_config._upi_zero_cache_skip_bad({}) is True
    assert stage_config._upi_zero_cache_skip_bad({"zero_cache_skip_bad": False}) is False


# ── P1-3: exit risk memory ────────────────────────────────────────────────
def test_block_is_remembered_and_cleared(store):
    exit = "http://user:pass@1.2.3.4:8080"
    assert exit_health.block_reason(exit) == ""
    exit_health.record_block(exit, "approve blocked")
    assert "approve blocked" in exit_health.block_reason(exit)
    exit_health.clear(exit)
    assert exit_health.block_reason(exit) == ""


def test_block_window_expires(store, monkeypatch):
    monkeypatch.setenv("UPI_EXIT_HEALTH_WINDOW", "0")
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "blocked")
    assert exit_health.block_reason(exit) == ""


def test_small_pools_are_never_fully_poisoned(store):
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "blocked")
    assert exit_health.should_skip(exit, pool_size=1) is False
    assert exit_health.should_skip(exit, pool_size=10) is True


def test_the_raw_credential_is_never_written_to_disk(store):
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "blocked")
    raw = (store / exit_health.EXIT_HEALTH_FILENAME).read_text(encoding="utf-8")
    assert "user:pass" not in raw
    assert "1.2.3.4" not in raw


def test_health_can_be_switched_off(store, monkeypatch):
    monkeypatch.setenv("UPI_EXIT_HEALTH", "0")
    exit = "http://user:pass@1.2.3.4:8080"
    assert exit_health.record_block(exit, "blocked") is None
    assert exit_health.block_reason(exit) == ""


def test_wrapper_records_a_block_and_clears_it_on_success(store, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: {"ok": False, "error_code": "upi_provider_declined", "error": "approve blocked"},
    )
    exit = "http://user:pass@1.2.3.4:8080"
    pipeline.generate_upi_qr_link("at", checkout_proxy=exit, runtime_config={"upi": {"rounds": 1}})
    assert exit_health.block_reason(exit) != ""

    monkeypatch.setattr(pipeline, "_generate_upi_qr_link_once", lambda **kwargs: {"ok": True, "url": "upi://pay"})
    pipeline.generate_upi_qr_link("at", checkout_proxy=exit, runtime_config={"upi": {"rounds": 1}})
    assert exit_health.block_reason(exit) == ""


def test_cooling_exit_is_reported_but_does_not_stop_the_run(store, monkeypatch):
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "approve blocked")
    calls: list[int] = []
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: calls.append(1) or {"ok": True, "url": "upi://pay"},
    )
    result = pipeline.generate_upi_qr_link("at", checkout_proxy=exit, runtime_config={"upi": {"rounds": 1}})
    assert result["ok"] is True
    assert calls == [1]


def test_skip_switch_turns_a_cooling_exit_into_a_retryable_refusal(store, monkeypatch):
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "approve blocked")
    calls: list[int] = []
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: calls.append(1) or {"ok": True, "url": "upi://pay"},
    )
    result = pipeline.generate_upi_qr_link(
        "at",
        checkout_proxy=exit,
        runtime_config={"upi": {"rounds": 1, "exit_health_skip": True}},
    )
    assert result["error_code"] == "exit_health_blocked"
    assert calls == []  # never spent a Checkout on the cooling exit
    assert stage_config._upi_round_retryable(result) is True


def test_skip_switch_lets_the_second_round_proceed(store, monkeypatch):
    exit = "http://user:pass@1.2.3.4:8080"
    exit_health.record_block(exit, "approve blocked")
    calls: list[int] = []

    def fake_once(**kwargs):
        calls.append(1)
        # A fresh exit (rotated session) is clear again.
        exit_health.clear(kwargs["checkout_proxy"])
        return {"ok": True, "url": "upi://pay"}

    monkeypatch.setattr(pipeline, "_generate_upi_qr_link_once", fake_once)
    monkeypatch.setattr(pipeline, "rotate_session", lambda proxy, country: proxy + "#2")
    result = pipeline.generate_upi_qr_link(
        "at",
        checkout_proxy=exit,
        runtime_config={"upi": {"rounds": 2, "exit_health_skip": True}},
    )
    assert result["ok"] is True
    assert calls == [1]


def _ops():
    return UR._pipeline()._build_upi_operations()
