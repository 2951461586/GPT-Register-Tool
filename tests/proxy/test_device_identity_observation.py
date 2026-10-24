"""S0 of plan-2026-10-10-fingerprint-device-layer: observe where ``oai-did`` comes from.

Zero behaviour change.  The tests pin today's baseline (an address that never
registered gets a *fresh* ``oai-did`` every run) so that S1's
``device_identity_seeded_by_email`` has something to be compared against.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool.registration.funnel import combine_registration_funnels, summarize_registration_funnel
from sms_tool.registration.handlers import RegistrationEmailWorkflow
from sms_tool.registration.result import build_registration_result, safe_device_identity
from sms_tool.registration.state import RegistrationStateMachine

FULL_DID = "0123456789abcdef-secret-tail"


def _workflow(email: str, stored: dict, fresh_did: str, registration: dict | None = None):
    mailbox = SimpleNamespace(email=email)
    persistence = Mock()
    persistence.get_device_context.return_value = stored
    ops = Mock()
    ops._tl.return_value = []
    ops._sentinel_device_id.side_effect = lambda data: data.get("oai_did", "")
    ops._normalize_registration_mode.return_value = "password"
    ops._stored_registration_password.return_value = ""
    ops._generate_password.return_value = "GeneratedPassword!1"
    ops._random_name.return_value = ("Ada", "Lovelace")
    ops._random_birthdate.return_value = "1990-01-01"
    ops.openai_auth_headers.side_effect = lambda *a, **k: {"oai-device-id": workflow.runtime.device_id}
    ops._sanitize_text.side_effect = lambda value: str(value or "")
    ops.assert_sentinel_device_id = lambda *_args: None
    ops.runtime_config_scope.return_value = nullcontext()
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        proxy="http://proxy.invalid:8080",
        mailbox=mailbox,
        config={"email_registration": {}, "chatgpt": {}, "registration": dict(registration or {})},
        operations=ops,
        persistence=persistence,
    )
    workflow.runtime.mailbox = mailbox
    workflow.runtime.proxy = "http://proxy.invalid:8080"
    workflow.runtime.auth_base = "https://auth.example"
    workflow.runtime.chat_base = "https://chat.example"
    workflow.runtime.sentinel_data = {"oai_did": fresh_did}
    return workflow


def _prepare(workflow) -> None:
    with (
        patch("sms_tool.registration.identity_stages._new_registration_session", return_value=Mock()),
        patch("sms_tool.registration.identity_stages._apply_protocol_fingerprint"),
    ):
        workflow.prepare_identity()


def test_never_registered_address_gets_a_fresh_device_identity_every_run(capsys):
    first = _workflow("same@example.com", {}, "aaaaaaaa-1111")
    second = _workflow("same@example.com", {}, "bbbbbbbb-2222")
    _prepare(first)
    _prepare(second)

    assert first.device_identity["reused"] is False
    assert second.device_identity["reused"] is False
    assert first.device_identity["seeded"] == second.device_identity["seeded"] == "none"
    assert first.runtime.device_id != second.runtime.device_id
    out = capsys.readouterr().out
    assert "Device identity: oai-did=aaaaaaaa reused=false seeded=none" in out
    assert "Device identity: oai-did=bbbbbbbb reused=false seeded=none" in out


def test_persisted_device_context_reads_as_reused(capsys):
    workflow = _workflow(
        "known@example.com",
        {"device_id": "cccccccc-3333", "auth_session_logging_id": "logging"},
        "dddddddd-4444",
    )
    _prepare(workflow)

    assert workflow.device_identity == {"oai_did": "cccccccc-3333", "reused": True, "seeded": "none"}
    assert "Device identity: oai-did=cccccccc reused=true seeded=none" in capsys.readouterr().out


def test_mechanism_line_never_carries_more_than_eight_characters_of_the_id(capsys):
    workflow = _workflow("long@example.com", {}, FULL_DID)
    _prepare(workflow)
    out = capsys.readouterr().out
    assert "oai-did=01234567 " in out
    assert FULL_DID not in out
    assert "secret-tail" not in out


def test_safe_device_identity_truncates_and_distinguishes_unmeasured_from_fresh():
    assert safe_device_identity({"oai_did": FULL_DID, "reused": False, "seeded": "email"}) == {
        "oai_did": "01234567",
        "reused": False,
        "seeded": "email",
    }
    assert safe_device_identity(None) == {"oai_did": "", "reused": None, "seeded": "unknown"}
    hostile = safe_device_identity({"device_id": FULL_DID, "reused": "yes", "seeded": "x" * 500})
    assert hostile == {"oai_did": "01234567", "reused": None, "seeded": "unknown"}


def test_result_contract_carries_a_safe_device_identity():
    result = build_registration_result(
        success=False,
        registration_mode="password",
        registration_state="failed",
        device_id=FULL_DID,
        device_identity={"oai_did": FULL_DID, "reused": False, "seeded": "none"},
    )
    assert result["device_identity"] == {"oai_did": "01234567", "reused": False, "seeded": "none"}
    assert "secret-tail" not in repr(result["device_identity"])
    bare = build_registration_result(success=False, registration_mode="password", registration_state="failed")
    assert bare["device_identity"]["reused"] is None


def test_funnel_counts_every_attempt_not_only_successes():
    rows = [
        {"success": False, "device_identity": {"reused": False, "seeded": "none"}},
        {"success": False, "device_identity": {"reused": False, "seeded": "none"}},
        {"success": True, "device_identity": {"reused": True, "seeded": "none"}},
        {"success": False, "device_identity": {"reused": False, "seeded": "email"}},
        {"success": False},
        {"success": False, "device_identity": "not-a-mapping"},
    ]
    report = summarize_registration_funnel(rows, attempted=6)
    assert report["device_identity"] == {"reused": 1, "fresh": 3, "unknown": 2, "seeded_by_email": 1}

    combined = combine_registration_funnels([report, report])
    assert combined["device_identity"] == {"reused": 2, "fresh": 6, "unknown": 4, "seeded_by_email": 2}


def test_funnel_device_identity_never_breaks_on_a_hostile_aggregate():
    funnel = {"attempted": 1, "device_identity": {"reused": "oops", "fresh": -3}}
    combined = combine_registration_funnels([funnel, {"attempted": 1}])
    assert combined["device_identity"] == {"reused": 0, "fresh": 0, "unknown": 0, "seeded_by_email": 0}


# ---------------------------------------------------------------------------
# S1: registration.device_identity_seeded_by_email
# ---------------------------------------------------------------------------

SEEDED = {"device_identity_seeded_by_email": True}


def test_seed_is_a_pure_function_of_the_normalised_address():
    import re
    import uuid

    from sms_tool.registration.state import seeded_device_identity

    a = seeded_device_identity("Same@Example.com ")
    assert a == seeded_device_identity("same@example.com")
    assert str(uuid.UUID(a["device_id"])) == a["device_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", a["auth_session_logging_id"])
    assert a["device_id"] != a["auth_session_logging_id"]
    other = seeded_device_identity("other@example.com")
    assert other["device_id"] != a["device_id"]
    assert other["auth_session_logging_id"] != a["auth_session_logging_id"]
    assert seeded_device_identity("") == {}
    assert seeded_device_identity(None) == {}


def test_seed_uses_a_private_salt_not_turbs_public_literal():
    import uuid

    from sms_tool.registration.state import seeded_device_identity

    turb = str(uuid.uuid5(uuid.NAMESPACE_URL, "device_id:registration:same@example.com"))
    assert seeded_device_identity("same@example.com")["device_id"] != turb


def test_plus_alias_is_a_different_address_with_a_different_identity():
    from sms_tool.registration.state import seeded_device_identity

    base = seeded_device_identity("name@icloud.com")
    alias = seeded_device_identity("name+tag@icloud.com")
    assert base["device_id"] != alias["device_id"]


def test_toggle_on_gives_a_never_registered_address_the_same_identity_every_run(capsys):
    first = _workflow("same@example.com", {}, "aaaaaaaa-1111", SEEDED)
    second = _workflow("same@example.com", {}, "bbbbbbbb-2222", SEEDED)
    _prepare(first)
    _prepare(second)

    assert first.runtime.device_id == second.runtime.device_id
    assert first.runtime.session_logging_id == second.runtime.session_logging_id
    assert first.runtime.sentinel_data["oai_did"] == first.runtime.device_id
    for workflow in (first, second):
        assert workflow.device_identity["reused"] is False
        assert workflow.device_identity["seeded"] == "email"
    out = capsys.readouterr().out
    assert out.count("Device identity seeded by email") == 2
    assert f"oai-did={first.runtime.device_id[:8]} reused=false seeded=email" in out
    assert first.runtime.device_id not in out
    assert "Reusing persisted device context" not in out


def test_toggle_on_gives_different_addresses_different_identities():
    one = _workflow("one@example.com", {}, "aaaaaaaa-1111", SEEDED)
    two = _workflow("two@example.com", {}, "aaaaaaaa-1111", SEEDED)
    _prepare(one)
    _prepare(two)
    assert one.runtime.device_id != two.runtime.device_id


def test_toggle_on_never_overrides_a_stored_identity(capsys):
    workflow = _workflow(
        "known@example.com",
        {"device_id": "cccccccc-3333", "auth_session_logging_id": "logging"},
        "dddddddd-4444",
        SEEDED,
    )
    _prepare(workflow)

    assert workflow.runtime.device_id == "cccccccc-3333"
    assert workflow.device_identity == {"oai_did": "cccccccc-3333", "reused": True, "seeded": "none"}
    out = capsys.readouterr().out
    # The arm was armed even though this address's stored id won.
    assert "Device identity seeded by email" in out
    assert "reused=true seeded=none" in out


def test_toggle_off_is_byte_for_byte_todays_behaviour(capsys):
    off = _workflow("same@example.com", {}, "aaaaaaaa-1111", {"device_identity_seeded_by_email": False})
    absent = _workflow("same@example.com", {}, "bbbbbbbb-2222")
    _prepare(off)
    _prepare(absent)
    assert off.runtime.device_id == "aaaaaaaa-1111"
    assert absent.runtime.device_id == "bbbbbbbb-2222"
    assert "Device identity seeded by email" not in capsys.readouterr().out


def test_seeded_identity_survives_the_legacy_sentinel_regeneration_branch():
    workflow = _workflow("same@example.com", {}, "aaaaaaaa-1111", SEEDED)
    workflow.input_sentinel = {"oai_did": "aaaaaaaa-1111", "sentinel_token": "t"}
    workflow.runtime.sentinel_data = dict(workflow.input_sentinel)
    seen = {}

    def regenerate(**kwargs):
        seen["device_id"] = kwargs.get("device_id")
        return {"oai_did": kwargs.get("device_id"), "sentinel_token": "t2"}

    workflow.r._extract_sentinel.side_effect = regenerate
    _prepare(workflow)
    assert seen["device_id"] == workflow.runtime.device_id
    assert workflow.runtime.sentinel_data["oai_did"] == workflow.runtime.device_id


def test_config_rejects_a_non_boolean_toggle():
    import pytest

    from sms_tool.config import ConfigError, validate_config

    with pytest.raises(ConfigError, match=r"registration\.device_identity_seeded_by_email must be a boolean"):
        validate_config({"registration": {"device_identity_seeded_by_email": "yes"}})


def test_ab_harness_preregisters_p2_1_on_the_toggle_and_its_mechanism_line():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "registration_ab.py"
    spec = importlib.util.spec_from_file_location("registration_ab_p2_1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    design = module.EXPERIMENTS["p2-1-device-identity-seeded"]
    assert design["toggle"] == "registration.device_identity_seeded_by_email"
    assert design["arms"] == {"default": False, "seeded": True}
    assert design["mechanism"] == "Device identity seeded by email"
    assert not design.get("conditional")
    assert any("DIFFERENT addresses" in item for item in design["hold_constant"])
