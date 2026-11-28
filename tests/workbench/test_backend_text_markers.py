"""Cross-language guard for free-text stdout markers (round 6).

The WPF host matches some Python output by plain substring, e.g.
``MainWindow.Tasks.cs`` looks for ``Saved session:`` to trigger a hot-persistence
pool refresh. That is a contract with no version, no schema and no error path:
edit the Python ``print()`` alone and the C# behaviour silently stops.

The marker text now lives in two named constants -- ``BackendTextMarkers`` in
``SmsWorkbench.Contracts`` and ``SAVED_SESSION_MARKER`` in
``sms_tool.commands.registration``. This module parses **both** sides and
asserts they agree, so a one-sided edit fails the suite loudly instead of
failing quietly in production.

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252, and a
non-Latin-1 ``print`` there aborts the step (see MEMORY.md).
"""

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CS_MARKERS = PROJECT_ROOT / "SmsWorkbench.Contracts" / "BackendTextMarkers.cs"
PY_MARKERS = PROJECT_ROOT / "sms_tool" / "commands" / "registration.py"
PY_STATE = PROJECT_ROOT / "sms_tool" / "store" / "normalize.py"

# Python constant name -> C# constant name
PAIRED_MARKERS = {"SAVED_SESSION_MARKER": "SavedSession"}

# Account-state marker lists. The C# `AtInvalid` list intentionally omits the
# deactivation markers; the Python tuple is the union of both.
PAIRED_LISTS = {"ACCOUNT_DEACTIVATED_MARKERS": "AccountDeactivated"}
UNION_LISTS = {"AT_INVALID_MARKERS": ("AtInvalid", "AccountDeactivated")}


def _csharp_constants(path):
    """Pull ``public const string Name = "value";`` out of a C# file."""
    source = path.read_text(encoding="utf-8-sig")
    return {name: value for name, value in re.findall(r'public\s+const\s+string\s+(\w+)\s*=\s*"([^"]*)"\s*;', source)}


def _python_constants(path):
    """Pull module-level ``NAME = "value"`` string constants out of a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    values[target.id] = node.value.value
    return values


def _csharp_string_arrays(path):
    """Pull ``public static readonly string[] Name = { "a", "b" };`` blocks."""
    source = path.read_text(encoding="utf-8-sig")
    arrays = {}
    for name, body in re.findall(
        r"public\s+static\s+readonly\s+string\[\]\s+(\w+)\s*=\s*\{(.*?)\}\s*;",
        source,
        re.DOTALL,
    ):
        # strip // line comments so prose cannot be mistaken for a marker
        body = re.sub(r"//[^\n]*", "", body)
        arrays[name] = re.findall(r'"([^"]*)"', body)
    return arrays


def _python_string_tuples(path):
    """Pull module-level ``NAME = ("a", "b", ...)`` tuples, including + others."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    simple = {}
    values = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Tuple):
            try:
                simple[target.id] = [ast.literal_eval(e) for e in node.value.elts]
            except ValueError:
                pass
        elif isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add):
            # NAME = (...) + OTHER
            left, right = node.value.left, node.value.right
            if isinstance(left, ast.Tuple) and isinstance(right, ast.Name):
                try:
                    simple[target.id] = [ast.literal_eval(e) for e in left.elts] + simple.get(right.id, [])
                except ValueError:
                    pass
        elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            values[target.id] = node.value.value
    return simple, values


def test_sources_are_readable() -> None:
    assert CS_MARKERS.is_file(), f"missing {CS_MARKERS}"
    assert PY_MARKERS.is_file(), f"missing {PY_MARKERS}"


def test_paired_markers_have_identical_values() -> None:
    cs = _csharp_constants(CS_MARKERS)
    py = _python_constants(PY_MARKERS)
    for py_name, cs_name in PAIRED_MARKERS.items():
        assert cs_name in cs, f"C# constant {cs_name} not found"
        assert py_name in py, f"Python constant {py_name} not found"
        assert py[py_name] == cs[cs_name], (
            "marker drift: the host matches Python stdout by substring, so these two must stay byte-identical"
        )


def test_markers_are_non_empty_and_defined() -> None:
    cs = _csharp_constants(CS_MARKERS)
    py = _python_constants(PY_MARKERS)
    for py_name, cs_name in PAIRED_MARKERS.items():
        assert py[py_name].strip()
        assert cs[cs_name].strip()


def test_deactivation_markers_match() -> None:
    cs = _csharp_string_arrays(CS_MARKERS)
    py_tuples, _ = _python_string_tuples(PY_STATE)
    for py_name, cs_name in PAIRED_LISTS.items():
        assert cs_name in cs
        assert py_name in py_tuples
        assert py_tuples[py_name] == cs[cs_name]


def test_at_invalid_is_the_union_of_both_csharp_lists() -> None:
    cs = _csharp_string_arrays(CS_MARKERS)
    py_tuples, _ = _python_string_tuples(PY_STATE)
    for py_name, (first, second) in UNION_LISTS.items():
        assert py_tuples[py_name] == cs[first] + cs[second], (
            "AT_INVALID_MARKERS must equal AtInvalid + AccountDeactivated"
        )


def test_misspelled_marker_is_still_present_on_both_sides() -> None:
    """Older releases wrote "account_deatived"; on-disk sessions still have it."""
    cs = _csharp_string_arrays(CS_MARKERS)
    py_tuples, _ = _python_string_tuples(PY_STATE)
    assert "account_deatived" in cs["AccountDeactivated"]
    assert "account_deatived" in py_tuples["ACCOUNT_DEACTIVATED_MARKERS"]


def test_csharp_uses_the_shared_lists_not_inline_literals() -> None:
    source = (PROJECT_ROOT / "SmsWorkbench" / "Services" / "AccountStatusInterpreter.cs").read_text(
        encoding="utf-8-sig"
    )
    assert "BackendTextMarkers.AtInvalid" in source
    assert "BackendTextMarkers.AccountDeactivated" in source
    # the old inline chain must be gone
    for literal in ('text.Contains("account_deatived")', 'text.Contains("token_invalidated")'):
        assert literal not in source


def test_python_prints_through_the_constant() -> None:
    source = PY_MARKERS.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(PY_MARKERS))
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(getattr(node, "ctx", None), ast.Load)
    }
    for py_name in PAIRED_MARKERS:
        assert py_name in used, f"{py_name} is defined but never referenced"
    # and no literal left behind in a print()
    assert 'print(f"[*] Saved session:' not in source


def test_csharp_matches_through_the_constant() -> None:
    source = (PROJECT_ROOT / "SmsWorkbench" / "Views" / "MainWindow.Tasks.cs").read_text(encoding="utf-8-sig") + (
        PROJECT_ROOT / "SmsWorkbench" / "Services" / "BackendBatchCoordinator.cs"
    ).read_text(encoding="utf-8-sig")
    for cs_name in PAIRED_MARKERS.values():
        assert f"BackendTextMarkers.{cs_name}" in source
    # the old inline literal must be gone
    assert 'Contains("Saved session:"' not in source
