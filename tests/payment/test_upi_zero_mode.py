"""P2: zero-mode strategies, eligibility precheck, 0-元 negative cache.

Three reference findings (``upi_extract.py:3732`` / ``:9011`` / ``:9041``):

* the promo campaign is only **one** way to make a Checkout bill zero --
  ``trial`` adds a real ``subscription_data.trial_period_days`` and ``momo`` also
  mirrors the MoL flow's billing profile;
* an account already known to answer "non-zero" under ``require_zero`` does not
  deserve another Checkout;
* that knowledge is worth persisting for a day (negative cache).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sms_tool.payment import upi_identity_store as identity_store
from sms_tool.payment import upi_zero as zero_eligibility
from sms_tool.upi_link import pipeline, stage_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upi_replay as UR  # noqa: E402


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "identity"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(identity_store.STORE_DIR_ENV, str(directory))
    for name in ("UPI_ZERO_MODE", "UPI_TRIAL_DAYS", "UPI_ZERO_ELIGIBILITY_PRECHECK", "UPI_ZERO_NEGATIVE_CACHE"):
        monkeypatch.delenv(name, raising=False)
    return directory


# ── config readers ─────────────────────────────────────────────────────────
def test_zero_mode_defaults_to_the_campaign(monkeypatch):
    assert stage_config._upi_zero_mode({}) == "campaign"
    assert stage_config._upi_zero_mode({"zero_mode": "trial"}) == "trial"
    assert stage_config._upi_zero_mode({"zero_mode": "nonsense"}) == "campaign"
    monkeypatch.setenv("UPI_ZERO_MODE", "momo")
    assert stage_config._upi_zero_mode({}) == "momo"


def test_trial_period_days_is_clamped(monkeypatch):
    monkeypatch.delenv("UPI_TRIAL_DAYS", raising=False)
    assert stage_config._upi_trial_period_days() == 30
    monkeypatch.setenv("UPI_TRIAL_DAYS", "500")
    assert stage_config._upi_trial_period_days() == 90
    monkeypatch.setenv("UPI_TRIAL_DAYS", "0")
    assert stage_config._upi_trial_period_days() == 1


def test_eligibility_switches_default_on_and_read_config_first():
    assert stage_config._upi_zero_eligibility_precheck({}) is True
    assert stage_config._upi_zero_eligibility_precheck({"zero_eligibility_precheck": False}) is False
    assert stage_config._upi_zero_negative_cache({}) is True
    assert stage_config._upi_zero_negative_cache({"zero_negative_cache": False}) is False


# ── checkout body ──────────────────────────────────────────────────────────
class _StopAfterPost(RuntimeError):
    pass


class _FakeSession:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink
        self.headers: dict[str, str] = {}

    def post(self, _url: str, json: dict[str, Any] | None = None, **_kwargs):
        self._sink.append(dict(json or {}))
        # A 400 keeps ``_stage_checkout`` on its early-return path.
        return SimpleNamespace(status_code=400, text="nope", json=lambda: {})


class _BodyOps:
    """Ops stub that lets the stage reach the Checkout POST and no further."""

    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink
        self.sessions = 0

    @staticmethod
    def redact_proxy_text(text, _proxy=None):  # noqa: ARG004
        return "PROXY"

    def _upi_new_chatgpt_session(self, *_a, **_k):
        self.sessions += 1
        return _FakeSession(self._sink)

    @staticmethod
    def _upi_capture_risk_context(*_a, **_k):
        return SimpleNamespace(headers=lambda **__k: {}, sentinel_tokens={}, stripe_ids={})

    @staticmethod
    def _upi_account_id_from_token(_token):
        return ""

    @staticmethod
    def _upi_ensure_checkout_flow():
        return "chatgpt_checkout"

    @staticmethod
    def _upi_promo_page_url():
        return ""

    @staticmethod
    def _upi_sentinel_headers(*_a, **_k):
        return {}

    @staticmethod
    def _upi_sentinel_ping(*_a, **_k):
        return None

    @staticmethod
    def _upi_dump_http(*_a, **_k):
        return None


def _run_checkout(monkeypatch, store, *, upi_cfg: dict[str, Any], **overrides):
    ops = _BodyOps([])
    state, events = UR.build_state(upi_cfg=upi_cfg, **overrides)
    result = UR.run_stage("checkout", ops, state)
    return ops, state, result, events


def test_campaign_mode_sends_only_the_promo(store, monkeypatch):
    ops, _state, result, _events = _run_checkout(monkeypatch, store, upi_cfg={"zero_mode": "campaign"})
    assert result is not None and result["error_code"] == "checkout_failed"
    body = ops._sink[0]
    assert "subscription_data" not in body
    assert body["promo_campaign"]["promo_campaign_id"]


def test_trial_mode_adds_a_real_trial_period(store, monkeypatch):
    monkeypatch.setenv("UPI_TRIAL_DAYS", "7")
    ops, _state, _result, events = _run_checkout(monkeypatch, store, upi_cfg={"zero_mode": "trial"})
    body = ops._sink[0]
    assert body["subscription_data"] == {"trial_period_days": 7}
    assert any("zero_mode=trial" in message for _stage, message in events)


def test_momo_mode_adds_the_billing_identity(store, monkeypatch):
    ops, _state, _result, _events = _run_checkout(monkeypatch, store, upi_cfg={"zero_mode": "momo"})
    body = ops._sink[0]
    assert body["subscription_data"]["trial_period_days"] >= 1
    assert body["billing_details"]["name"]
    assert body["billing_details"]["email"]


# ── eligibility precheck + negative cache ──────────────────────────────────
def test_ineligible_account_never_creates_a_checkout(store, monkeypatch):
    zero_eligibility.mark_zero_ineligible("acc-1", cfg={})
    state, events = UR.build_state(upi_cfg={}, account_key="acc-1")
    ops = _BodyOps([])
    result = UR.run_stage("checkout", ops, state)
    assert result is not None
    assert result["error_code"] == "zero_ineligible_cached"
    assert ops.sessions == 0  # not one Checkout was created
    assert any("0-元 ineligible" in message for _stage, message in events)


def test_ineligible_marker_is_ignored_for_the_original_price_chain(store, monkeypatch):
    zero_eligibility.mark_zero_ineligible("acc-1", cfg={})
    state, _events = UR.build_state(upi_cfg={}, account_key="acc-1", require_zero=False)
    ops = _BodyOps([])
    result = UR.run_stage("checkout", ops, state)
    assert result is not None and result["error_code"] == "checkout_failed"
    assert ops.sessions == 1


def test_ineligible_marker_is_ignored_when_the_precheck_is_off(store, monkeypatch):
    zero_eligibility.mark_zero_ineligible("acc-1", cfg={})
    state, _events = UR.build_state(
        upi_cfg={"zero_eligibility_precheck": False},
        account_key="acc-1",
    )
    ops = _BodyOps([])
    result = UR.run_stage("checkout", ops, state)
    assert result is not None and result["error_code"] == "checkout_failed"
    assert ops.sessions == 1


def test_negative_cache_is_marked_on_no_free_trial(store, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: {"ok": False, "error_code": "no_free_trial", "require_zero": True},
    )
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    cooling, remaining = zero_eligibility.in_cooldown(identity_store.account_key(account_id="acc-1"))
    assert cooling is True
    assert remaining > 0


def test_negative_cache_is_not_marked_for_the_original_price_chain(store, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: {"ok": False, "error_code": "no_free_trial", "require_zero": False},
    )
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    assert zero_eligibility.in_cooldown(identity_store.account_key(account_id="acc-1"))[0] is False


def test_success_clears_the_negative_cache(store, monkeypatch):
    key = identity_store.account_key(account_id="acc-1")
    zero_eligibility.mark_zero_ineligible(key, cfg={})
    assert zero_eligibility.in_cooldown(key)[0] is True
    monkeypatch.setattr(pipeline, "_generate_upi_qr_link_once", lambda **kwargs: {"ok": True, "url": "upi://pay"})
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    assert zero_eligibility.in_cooldown(key)[0] is False


def test_the_negative_cache_stores_no_credentials(store):
    key = identity_store.account_key(account_id="acc-1")
    zero_eligibility.mark_zero_ineligible(key, cfg={})
    raw = (store / identity_store.STORE_FILENAME).read_text(encoding="utf-8")
    assert "acc-1" not in raw
