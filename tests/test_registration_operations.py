import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sms_tool import registration
from sms_tool.registration_operations import OPERATION_GROUPS, RegistrationOperations


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
