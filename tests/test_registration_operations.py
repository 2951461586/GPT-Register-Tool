import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sms_tool import registration
from sms_tool.registration_operations import OPERATION_GROUPS, RegistrationOperations
from sms_tool.registration_handlers import StorageRegistrationPersistence


def test_operation_binding_respects_callers_patch_scope_and_is_immutable():
    fake = Mock()
    with patch.object(registration, "_fetch_auth_session", fake):
        operations = RegistrationOperations.bind(vars(registration))
    assert operations._fetch_auth_session is fake
    assert not hasattr(operations, "sys")
    with pytest.raises(FrozenInstanceError):
        operations._fetch_auth_session = Mock()


def test_interface_covers_only_dependencies_the_workflow_uses():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "sms_tool/registration_handlers.py").read_text(encoding="utf-8"))
    names = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and (
            isinstance(node.value, ast.Name) and node.value.id == "r"
            or isinstance(node.value, ast.Attribute) and node.value.attr == "r"
        )
    }
    assert names == {item.name for item in fields(RegistrationOperations)}


def test_operation_groups_partition_fields():
    declared = [item.name for item in fields(RegistrationOperations)]
    grouped = [name for names in OPERATION_GROUPS.values() for name in names]
    assert len(grouped) == len(set(grouped)), "a field appears in more than one group"
    assert set(grouped) == set(declared), (
        f"only in OPERATION_GROUPS: {sorted(set(grouped) - set(declared))}; "
        f"missing from OPERATION_GROUPS: {sorted(set(declared) - set(grouped))}"
    )
    # Each group must occupy one contiguous block, otherwise "grouped" is a lie.
    for group, names in OPERATION_GROUPS.items():
        positions = [declared.index(name) for name in names]
        assert positions == list(range(min(positions), max(positions) + 1)), (
            f"group {group!r} is not contiguous in the dataclass: {names}"
        )


def test_bind_reports_every_missing_dependency_at_once():
    declared = [item.name for item in fields(RegistrationOperations)]
    with pytest.raises(KeyError) as excinfo:
        RegistrationOperations.bind({})
    message = str(excinfo.value)
    assert f"missing {len(declared)}/{len(declared)}" in message
    for name in ("_tick", "think_stage", "request_with_retry"):
        assert name in message

    partial = {name: object() for name in declared if name != "_tick"}
    with pytest.raises(KeyError) as excinfo:
        RegistrationOperations.bind(partial)
    assert "missing 1/" in str(excinfo.value)
    assert "_tick" in str(excinfo.value)


def test_email_workflow_wiring_reads_globals_at_call_time():
    """The mapping must be built per call, not frozen at import.

    Hoisting it to a module-level constant would still bind correctly, but
    every ``patch.object(registration, ...)`` in the suite would then be
    ignored -- the workflow would run against the real dependencies and the
    tests would go red (or worse, quietly green).
    """
    fake = Mock()
    with patch.object(registration, "_fetch_auth_session", fake):
        operations = registration._email_registration_operations()
    assert operations._fetch_auth_session is fake


def test_email_workflow_wiring_is_explicit_not_a_globals_sweep():
    """``bind(globals())`` hid 59 edges from static analysis.

    Deleting a top-level import surfaced only as a runtime KeyError raised from
    a different module, and linters reported every one of those imports as
    unused. The wiring has to name each dependency literally instead.
    """
    source = (Path(__file__).resolve().parents[1] / "sms_tool/registration.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # AST, not a substring search: the helper docstring legitimately mentions
    # ``bind(globals())`` while explaining why it is gone.
    sweep = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "bind"
        and any(
            isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "globals"
            for arg in node.args
        )
    ]
    assert not sweep, "protocol workflow went back to a globals sweep"

    helper = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "_email_registration_operations"),
        None,
    )
    assert helper is not None, "explicit wiring helper disappeared"

    keys: set[str] = set()
    for node in ast.walk(helper):
        if isinstance(node, ast.Dict):
            keys.update(key.value for key in node.keys if isinstance(key, ast.Constant))

    declared = {item.name for item in fields(RegistrationOperations)}
    assert declared == keys, (
        f"wiring drifted from the dataclass: missing={sorted(declared - keys)} "
        f"extra={sorted(keys - declared)}"
    )


def test_storage_registration_persistence_is_a_replaceable_adapter():
    adapter = StorageRegistrationPersistence()
    with patch("sms_tool.storage.save_registration_checkpoint", return_value=True) as save, \
         patch("sms_tool.storage.upsert_account", return_value=True) as upsert, \
         patch("sms_tool.storage.get_registration_checkpoint", return_value={"state": "x"}) as get, \
         patch("sms_tool.storage.get_device_context", return_value={"device_id": "d"}) as device, \
         patch("sms_tool.storage.clear_registration_checkpoint", return_value=True) as clear:
        assert adapter.save_checkpoint("a@example.com", "x", {"email": "a@example.com"}, runtime_config={})
        assert adapter.upsert_account({"email": "a@example.com"}, runtime_config={})
        assert adapter.get_checkpoint("a@example.com", runtime_config={}) == {"state": "x"}
        assert adapter.get_device_context("a@example.com") == {"device_id": "d"}
        assert adapter.clear_checkpoint("a@example.com", runtime_config={})
    save.assert_called_once()
    upsert.assert_called_once()
    get.assert_called_once()
    device.assert_called_once()
    clear.assert_called_once()
