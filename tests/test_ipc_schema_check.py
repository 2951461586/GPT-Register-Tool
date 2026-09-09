"""Tests for scripts/ipc_schema_check.py -- the cross-language IPC contract.

Round-3 audit P1-7 said ``error_codes`` was a dead field and recommended
deleting it to "remove the false sense of security". That is backwards: the five
codes are real on **both** sides (``desktop_serve.py`` ``CODE_*`` constants and
``DesktopReadProtocol.cs`` ``DesktopReadErrorCodes``). What was missing is the
*check*. Deleting the field would have removed the only place the contract is
written down and left the drift invisible.

So these tests lock in the checks, not the deletion. The contract is
one-directional on purpose: C# carries client-local codes
(``timeout``/``cancelled``/``protocol_mismatch``/``channel_unavailable``) that
never arrive from the backend, so requiring all three sets to be equal would be
wrong. The rule is: **anything the backend emits, the client must parse.**
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ipc_schema_check as ipc  # noqa: E402


# --------------------------------------------------------------------------
# extraction helpers -- each of these can silently go empty and vacuously pass
# --------------------------------------------------------------------------

def test_py_error_codes_extraction():
    src = (
        'CODE_BAD_REQUEST = "bad_request"\n'
        'CODE_UNKNOWN_OP = "unknown_operation"\n'
        'OTHER_CONSTANT = "not_a_code"\n'
    )
    assert ipc._py_error_codes(src) == ["bad_request", "unknown_operation"]


def test_cs_parseable_codes_extraction():
    src = (
        '            "bad_request" => DesktopReadErrorCode.BadRequest,\n'
        '            "unknown_op" => DesktopReadErrorCode.UnknownOperation,\n'
        '            "internal" => DesktopReadErrorCode.Internal,\n'
    )
    assert ipc._cs_parseable_codes(src) == ["bad_request", "internal", "unknown_op"]


def test_py_envelope_keys_extraction():
    src = (
        "def _envelope(message_type, payload):\n"
        "    return {\n"
        '        "schema": "smsworkbench.ipc.v2",\n'
        '        "version": 2,\n'
        '        "payload": payload,\n'
        "    }\n"
    )
    assert ipc._py_envelope_keys(src) == ["payload", "schema", "version"]


def test_cs_envelope_keys_read_covers_both_call_shapes():
    """Regression: only matching ``root.GetProperty("x")`` missed ``type`` and
    ``sequence``, which are read through the ``Text(root, "x")`` / ``Number(root, "x")``
    helpers -- i.e. the check was blind to exactly the drift it exists to catch."""
    src = (
        'root.GetProperty("version").GetInt32();\n'
        'Text(root, "type");\n'
        'Number(root, "sequence");\n'
        'root.TryGetProperty("payload", out JsonElement payload);\n'
    )
    assert ipc._cs_envelope_keys_read(src) == ["payload", "sequence", "type", "version"]


# --------------------------------------------------------------------------
# contract checks against synthetic sources
# --------------------------------------------------------------------------

_PY = (
    'PROTOCOL_VERSION = 1\n'
    'SUPPORTED_OPS = ["hello", "ping"]\n'
    'CODE_BAD_REQUEST = "bad_request"\n'
    'CODE_INTERNAL = "internal"\n'
)
_PY_IPC = (
    "def _envelope(message_type, payload):\n"
    "    return {\n"
    '        "schema": "smsworkbench.ipc.v2",\n'
    '        "version": 2,\n'
    '        "payload": payload,\n'
    "    }\n"
)
_CS = (
    "public const int Version = 1;\n"
    '            "bad_request" => DesktopReadErrorCode.BadRequest,\n'
    '            "internal" => DesktopReadErrorCode.Internal,\n'
    '            "timeout" => DesktopReadErrorCode.Timeout,\n'
)
# Same header, no ``DesktopReadErrorCode`` mappings at all: the client can parse
# nothing, so every backend code must be reported.
_CS_WITHOUT_CODES = "public const int Version = 1;\n"


def _manifest(**overrides):
    base = {
        "version": 1,
        "ops": ["hello", "ping"],
        "error_codes": ["bad_request", "internal"],
        "event_envelope_keys": ["payload", "schema", "version"],
        "protocol_version": 1,
        "event_envelope_version": 2,
        "event_envelope_schema": "smsworkbench.ipc.v2",
    }
    base.update(overrides)
    return base


def test_happy_path_has_no_failures():
    assert ipc.check_manifest(_manifest(), _PY, _PY_IPC, _CS, "") == []


def test_csharp_may_carry_extra_client_local_codes():
    """The contract is one-directional -- extra C# codes are legitimate."""
    assert '"timeout"' in _CS
    assert ipc.check_manifest(_manifest(), _PY, _PY_IPC, _CS, "") == []


def test_protocol_and_event_envelope_versions_are_distinct():
    failures = ipc.check_manifest(_manifest(event_envelope_version=3), _PY, _PY_IPC, _CS, "")
    assert any("event envelope version mismatch" in item for item in failures)


def test_manifest_missing_an_error_code_is_reported():
    failures = ipc.check_manifest(_manifest(error_codes=["bad_request"]), _PY, _PY_IPC, _CS, "")
    assert any("error codes differ" in f for f in failures)


def test_backend_code_the_client_cannot_parse_is_reported():
    failures = ipc.check_manifest(_manifest(), _PY, _PY_IPC, _CS_WITHOUT_CODES, "")
    assert any("cannot parse" in f for f in failures)


def test_undeclared_envelope_key_built_by_python_is_reported():
    failures = ipc.check_manifest(
        _manifest(event_envelope_keys=["payload", "schema"]), _PY, _PY_IPC, _CS, ""
    )
    assert any("event envelope keys differ" in f for f in failures)


def test_csharp_reading_an_envelope_key_python_never_emits_is_reported():
    cs_events = 'root.GetProperty("version");\nroot.GetProperty("run_id");\n'
    failures = ipc.check_manifest(_manifest(), _PY, _PY_IPC, _CS, cs_events)
    assert any("does not emit" in f and "run_id" in f for f in failures)


# --------------------------------------------------------------------------
# the real tree
# --------------------------------------------------------------------------

def test_real_tree_passes():
    assert ipc.main() == 0


def test_extractors_are_not_empty_on_the_real_tree():
    """Guard against a vacuous pass: every extractor must find something."""
    root = Path(__file__).resolve().parents[1]
    py = (root / "sms_tool" / "desktop_serve.py").read_text(encoding="utf-8")
    py_ipc = (root / "sms_tool" / "desktop_ipc.py").read_text(encoding="utf-8")
    cs = (root / "SmsWorkbench.Contracts" / "DesktopReadProtocol.cs").read_text(encoding="utf-8")
    cs_events = (root / "SmsWorkbench.Contracts" / "BackendProgressEvents.cs").read_text(encoding="utf-8")

    assert len(ipc._py_error_codes(py)) >= 5
    assert len(ipc._cs_parseable_codes(cs)) >= 5
    assert "payload" in ipc._py_envelope_keys(py_ipc)
    assert "version" in ipc._cs_envelope_keys_read(cs_events)
