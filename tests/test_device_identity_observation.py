"""S0 of plan-2026-10-10-fingerprint-device-layer: observe where ``oai-did`` comes from.

Zero behaviour change.  The tests pin today's baseline (an address that never
registered gets a *fresh* ``oai-did`` every run) so that S1's
``device_identity_seeded_by_email`` has something to be compared against.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool.registration_funnel import combine_registration_funnels, summarize_registration_funnel
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_result import build_registration_result, safe_device_identity
from sms_tool.registration_state import RegistrationStateMachine

FULL_DID = "0123456789abcdef-secret-tail"


def _workflow(email: str, stored: dict, fresh_did: str):
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
        config={"email_registration": {}, "chatgpt": {}},
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
        patch("sms_tool.registration_handlers._new_registration_session", return_value=Mock()),
        patch("sms_tool.registration_handlers._apply_protocol_fingerprint"),
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
