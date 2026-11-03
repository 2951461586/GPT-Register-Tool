"""P0-2 / P0-3: sticky, de-duplicated billing and fingerprint identities.

Before this change ``pipeline._resolve_upi_runtime`` called
``secrets.randbelow`` twice per invocation -- once for the billing name/address
(5 names × 4 addresses) and once for the fingerprint template (3 Chrome UAs).
One account retried twice therefore presented two different humans *and* two
different devices, and a 4-lane batch could hand the same address to two
accounts.

These tests pin the replacement contract:

* an account key is derived deterministically (and is ``""`` for a token that
  carries nothing, so throwaway runs never touch the disk);
* billing is sticky per account and globally de-duplicated;
* the fingerprint is sticky per account, rotates only on retry/cooldown, and
  records a cooldown after a ``blocked`` verdict;
* the round wrapper asks for rotation on attempt 2+ and records the outcome;
* the historical fully-random behaviour survives when no account is known.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from sms_tool.payment import upi_billing as billing
from sms_tool.payment import upi_fingerprint as fingerprint_identity
from sms_tool.payment import upi_identity_store as identity_store
from sms_tool.upi_link import pipeline, session


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Point the identity ledger at a throwaway directory."""
    directory = tmp_path / "identity"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(identity_store.STORE_DIR_ENV, str(directory))
    return directory


def _jwt(claims: dict) -> str:
    def part(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


# ── account key ─────────────────────────────────────────────────────────────
def test_account_key_is_stable_and_account_specific():
    a = identity_store.account_key(account_id="acc-1")
    assert a == identity_store.account_key(account_id="ACC-1")  # case-insensitive
    assert a != identity_store.account_key(account_id="acc-2")
    assert len(a) == 24


def test_account_key_reads_the_token_claim():
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-token"}})
    from_token = identity_store.account_key(access_token=token)
    assert from_token
    assert from_token == identity_store.account_key(account_id="acc-token")


def test_account_key_is_empty_for_an_opaque_token():
    """No identity ⇒ no ledger writes (this is what keeps the suite offline)."""
    assert identity_store.account_key(access_token="at") == ""
    assert identity_store.account_key() == ""


# ── billing: stickiness + de-duplication ────────────────────────────────────
def test_billing_is_sticky_per_account(store):
    key = identity_store.account_key(account_id="acc-1")
    first = billing.billing_profile(account_key=key)
    second = billing.billing_profile(account_key=key)
    assert first == second
    assert (store / identity_store.STORE_FILENAME).is_file()


def test_two_accounts_never_share_a_name_and_address(store):
    seen = set()
    for index in range(12):
        key = identity_store.account_key(account_id=f"acc-{index}")
        profile = billing.billing_profile(account_key=key)
        identity = "|".join((profile["name"], profile["line1"], profile["postal_code"]))
        assert identity not in seen, identity
        seen.add(identity)
    assert len(seen) == 12


def test_billing_without_an_account_key_stays_random_and_writes_nothing(store):
    first = billing.billing_profile()
    second = billing.billing_profile()
    assert first != second
    assert not (store / identity_store.STORE_FILENAME).exists()


def test_billing_pool_is_large_and_well_formed():
    """The old pool was 5 × 4 = 20; a 4-lane batch collided by construction."""
    assert len(billing.BILLING_NAMES) >= 40
    assert len(billing.BILLING_ADDRESSES) >= 40
    for first, last in billing.BILLING_NAMES:
        assert first.isascii() and last.isascii()
    for street, city, postal, state in billing.BILLING_ADDRESSES:
        assert postal.isdigit() and len(postal) == 6, postal
        assert len(state) == 2 and state.isupper(), state
        assert city.isascii()


def test_generated_line1_has_exactly_one_house_number():
    profile, _identity = billing.billing_identity(4242, 0)
    street_part = profile["line1"].split(" ", 1)[1]
    assert not street_part[0].isdigit(), profile["line1"]


def test_billing_keeps_the_documented_override_chain(store, monkeypatch):
    profile = billing.billing_profile({"fixed_billing": {"country": "IN", "city": "Mumbai"}})
    assert profile["city"] == "Mumbai"
    assert profile["country"] == "IN"
    monkeypatch.setenv("UPI_CITY", "Kochi")
    assert billing.billing_profile()["city"] == "Kochi"


def test_corrupt_ledger_falls_back_instead_of_raising(store):
    (store / identity_store.STORE_FILENAME).write_text("{not json", encoding="utf-8")
    key = identity_store.account_key(account_id="acc-1")
    profile = billing.billing_profile(account_key=key)
    assert profile["line1"] and profile["postal_code"]


def test_billing_script_defaults_to_latin():
    assert billing.billing_script() == "latin"


# ── fingerprint: stickiness, rotation, cooldown ─────────────────────────────
def test_fingerprint_index_is_sticky_then_rotates(store):
    key = identity_store.account_key(account_id="acc-1")
    first = fingerprint_identity.fingerprint_index(key, 3)
    assert first is not None
    assert fingerprint_identity.fingerprint_index(key, 3) == first
    rotated = fingerprint_identity.fingerprint_index(key, 3, rotate=True)
    assert rotated == (first + 1) % 3
    assert fingerprint_identity.fingerprint_index(key, 3, rotate=True) == (first + 2) % 3


def test_fingerprint_index_is_none_without_an_account(store):
    assert fingerprint_identity.fingerprint_index("", 3) is None


def test_blocked_starts_a_cooldown_and_success_clears_it(store):
    key = identity_store.account_key(account_id="acc-1")
    assert fingerprint_identity.cooldown_status(key)[0] is False
    record = fingerprint_identity.record_outcome(key, success=False, blocked=True, reason="blocked")
    assert record is not None and record["last_status"] == "blocked"
    cooling, remaining = fingerprint_identity.cooldown_status(key)
    assert cooling is True
    assert 0 < remaining <= fingerprint_identity.FINGERPRINT_COOLDOWN_SECONDS
    fingerprint_identity.record_outcome(key, success=True, reason="")
    assert fingerprint_identity.cooldown_status(key)[0] is False


def test_cooldown_status_tolerates_a_corrupt_entry(store):
    key = identity_store.account_key(account_id="acc-1")
    (store / identity_store.STORE_FILENAME).write_text(
        json.dumps({"version": 1, "accounts": {key: {"fingerprint": {"cooldown_until": "soon"}}}, "identities": {}}),
        encoding="utf-8",
    )
    assert fingerprint_identity.cooldown_status(key) == (False, 0.0)


def test_session_fingerprint_uses_the_sticky_index(store):
    key = identity_store.account_key(account_id="acc-1")
    first = session._upi_fingerprint(None, "IN", account_key=key)
    second = session._upi_fingerprint(None, "IN", account_key=key)
    assert first["name"] == second["name"]
    rotated = session._upi_fingerprint(None, "IN", account_key=key, rotate=True)
    assert rotated["name"] != first["name"]


def test_session_fingerprint_without_account_key_stays_random(store):
    assert session._upi_fingerprint(0) == session._upi_fingerprint(0)
    assert not (store / identity_store.STORE_FILENAME).exists()


# ── runtime wiring ──────────────────────────────────────────────────────────
def _runtime(account: str, monkeypatch, **kwargs):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", kwargs.pop("store_dir"))
    return pipeline._resolve_upi_runtime(
        access_token="at",
        proxy="",
        checkout_proxy="http://p",
        provider_proxy="",
        approve_proxy="",
        target_country=None,
        checkout_country=None,
        payment_country=None,
        require_zero=None,
        runtime_config={"upi": {"billing_regions": ["IN"]}},
        device_id="",
        session_token="",
        auth_context={"account_id": account},
        **kwargs,
    )


def test_runtime_resolves_one_identity_for_the_whole_run(store, monkeypatch):
    first = _runtime("acc-1", monkeypatch, store_dir=str(store))
    second = _runtime("acc-1", monkeypatch, store_dir=str(store))
    assert first.account_key == second.account_key != ""
    assert first.billing == second.billing
    assert first.fingerprint["name"] == second.fingerprint["name"]


def test_runtime_rotates_the_fingerprint_on_retry(store, monkeypatch):
    base = _runtime("acc-1", monkeypatch, store_dir=str(store))
    rotated = _runtime("acc-1", monkeypatch, store_dir=str(store), rotate_fingerprint=True)
    assert rotated.fingerprint["name"] != base.fingerprint["name"]
    assert rotated.billing == base.billing  # billing must not rotate with the UA


def test_runtime_without_an_account_touches_no_store(store, monkeypatch):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", str(store))
    runtime = pipeline._resolve_upi_runtime(
        access_token="at",
        proxy="",
        checkout_proxy="http://p",
        provider_proxy="",
        approve_proxy="",
        target_country=None,
        checkout_country=None,
        payment_country=None,
        require_zero=None,
        runtime_config={"upi": {"billing_regions": ["IN"]}},
        device_id="",
        session_token="",
    )
    assert runtime.account_key == ""
    assert not (store / identity_store.STORE_FILENAME).exists()


# ── wrapper: rotate on retry + record the verdict ───────────────────────────
def test_wrapper_rotates_the_fingerprint_on_the_second_round(store, monkeypatch):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", str(store))
    seen: list[bool] = []

    def fake_once(**kwargs):
        seen.append(kwargs["rotate_fingerprint"])
        return {"ok": False, "error_code": "no_free_trial"} if len(seen) == 1 else {"ok": True, "url": "upi://pay"}

    monkeypatch.setattr(pipeline, "_generate_upi_qr_link_once", fake_once)
    monkeypatch.setattr(pipeline, "rotate_session", lambda proxy, country: proxy)
    result = pipeline.generate_upi_qr_link(
        "at",
        checkout_proxy="http://p",
        auth_context={"account_id": "acc-1"},
        runtime_config={"upi": {"rounds": 2}},
    )
    assert result["ok"] is True
    assert seen == [False, True]


def test_wrapper_records_the_outcome_on_the_account(store, monkeypatch):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", str(store))
    monkeypatch.setattr(pipeline, "_generate_upi_qr_link_once", lambda **kwargs: {"ok": True, "url": "upi://pay"})
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    key = identity_store.account_key(account_id="acc-1")
    ledger = identity_store.read_ledger()
    assert ledger["accounts"][key]["fingerprint"]["success"] == 1
    assert ledger["accounts"][key]["fingerprint"]["last_status"] == "success"


def test_wrapper_cools_the_fingerprint_after_a_blocked_verdict(store, monkeypatch):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", str(store))
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: {"ok": False, "error_code": "upi_provider_declined", "error": "approve blocked"},
    )
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    key = identity_store.account_key(account_id="acc-1")
    assert fingerprint_identity.cooldown_status(key)[0] is True


def test_wrapper_rotates_immediately_when_the_fingerprint_is_cooling(store, monkeypatch):
    monkeypatch.setenv("UPI_IDENTITY_STORE_DIR", str(store))
    key = identity_store.account_key(account_id="acc-1")
    fingerprint_identity.record_outcome(key, success=False, blocked=True, reason="blocked")
    seen: list[bool] = []
    monkeypatch.setattr(
        pipeline,
        "_generate_upi_qr_link_once",
        lambda **kwargs: seen.append(kwargs["rotate_fingerprint"]) or {"ok": True, "url": "upi://pay"},
    )
    pipeline.generate_upi_qr_link("at", auth_context={"account_id": "acc-1"}, runtime_config={"upi": {"rounds": 1}})
    assert seen == [True]
