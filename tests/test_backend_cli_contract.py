"""G5 - Python/C# command-line contract drift check.

Why this exists
---------------
The WPF front end is a thin shell: it builds an argument list and launches the
Python backend. Both sides hard-code the same ~50 CLI flag literals
independently, so a flag renamed on the Python side keeps compiling on the C#
side and only fails at runtime - and because the backend is launched as a child
process, the failure surfaces as "the operation produced nothing" rather than as
an error. Nothing in either test suite can catch it: the C# tests do not run the
backend, and the Python tests do not know the front end exists.

Direction of the check
----------------------
Only ``C# -> Python`` is asserted. "A flag Python defines that no C# caller
uses" is not a defect - the CLI is used standalone, from scripts and from the
documented commands - so asserting that direction would just be noise.

Two levels:

1. every flag the C# side passes must exist in the Python ``argparse`` parser;
2. for flags with ``choices=[...]``, a *literal* value passed from C# must be one
   of them (values built at runtime are skipped - they cannot be checked
   statically).

Design notes (why this will not become noise)
---------------------------------------------
  * ``MainWindow.Helpers.cs`` is excluded and must stay excluded: its ``"--"``
    literals are **Chrome process arguments** (``--incognito``, ``--new-window``)
    for ``Process.Start``, not backend flags. A blind scan of every ``.cs`` file
    reports those two as missing backend flags, which is a false positive that
    would get the whole guard disabled;
  * ``obj/`` and ``bin/`` are excluded explicitly. Generated files there
    (``MainWindow.g.cs``) duplicate source, and the grep tooling used elsewhere
    in this repo does not honour ``.gitignore`` - so the exclusion has to be
    written down rather than assumed;
  * the flag regex requires an alphanumeric character right after ``--``, so
    ``"--"`` (a placeholder in ``MainWindow.SmsBower.cs``) and ``"----"`` (the
    mailbox-file separator) are not mistaken for flags.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CS_ROOTS = ("SmsWorkbench", "SmsWorkbench.Contracts")
CS_PRUNE = {"obj", "bin", ".vs"}
PY_ROOTS = ("sms_tool", "services")
PY_PRUNE = {"__pycache__", ".venv", "dist", "runtime", "tests", ".git"}

FLAG_RE = re.compile(r'"(--[a-z0-9][a-z0-9-]*)"')
STRING_RE = re.compile(r'"([^"\\]*)"')

# Files whose `"--x"` literals are not backend CLI flags. Each needs a reason.
CS_EXCLUDED_FILES: dict[str, str] = {
    "MainWindow.Helpers.cs": (
        "launches Chrome via Process.Start with browser switches "
        "(--incognito, --new-window). Those are Chrome arguments, not backend "
        "flags, and no Python parser will ever define them."
    ),
}


# ------------------------------------------------------------------- python


def _python_flags() -> dict[str, tuple[str, list[str] | None]]:
    """{flag: (defining file, choices or None)} from every add_argument call."""
    out: dict[str, tuple[str, list[str] | None]] = {}
    for base in PY_ROOTS:
        for path in (ROOT / base).rglob("*.py"):
            if PY_PRUNE & set(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
                continue
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                attr = getattr(func, "attr", None) or getattr(func, "id", None)
                if attr != "add_argument":
                    continue
                flags = [
                    a.value for a in node.args
                    if isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                    and a.value.startswith("--")
                ]
                choices: list[str] | None = None
                for kw in node.keywords:
                    if kw.arg == "choices" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        choices = [
                            e.value for e in kw.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        ]
                for flag in flags:
                    out.setdefault(flag, (rel, choices))
    return out


# ------------------------------------------------------------------- csharp


def _iter_csharp_files() -> list[Path]:
    out: list[Path] = []
    for base in CS_ROOTS:
        root = ROOT / base
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in CS_PRUNE]
            out.extend(Path(dirpath) / f for f in filenames if f.endswith(".cs"))
    return sorted(out)


def _csharp_flag_values() -> dict[str, dict[str, set[str]]]:
    """{flag: {"where": {file}, "values": {literal values passed to it}}}.

    Values are only recorded when the next string literal after the flag is not
    itself a flag - i.e. when it really looks like the flag's argument. Values
    built at runtime (``Count(workers)``, ``request.PaymentMethod``) leave no
    literal and are simply not recorded.
    """
    out: dict[str, dict[str, set[str]]] = {}
    for path in _iter_csharp_files():
        if path.name in CS_EXCLUDED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for line in text.splitlines():
            literals = STRING_RE.findall(line.split("//", 1)[0])
            for index, literal in enumerate(literals):
                if not FLAG_RE.fullmatch(f'"{literal}"'):
                    continue
                entry = out.setdefault(literal, {"where": set(), "values": set()})
                entry["where"].add(rel)
                for follower in literals[index + 1:]:
                    if FLAG_RE.fullmatch(f'"{follower}"'):
                        break  # another flag - this one takes no literal value
                    if follower:
                        entry["values"].add(follower)
                    break
    return out


PYTHON_FLAGS = _python_flags()
CSHARP_FLAGS = _csharp_flag_values()


# ------------------------------------------------------------------- meta


def test_guard_saw_a_real_contract_on_both_sides() -> None:
    """Meta-check: a broken scanner would make every assertion below vacuous."""
    assert len(PYTHON_FLAGS) > 50, f"too few Python flags parsed: {len(PYTHON_FLAGS)}"
    assert len(CSHARP_FLAGS) > 20, f"too few C# flags parsed: {len(CSHARP_FLAGS)}"


def test_excluded_files_still_exist() -> None:
    """A stale exclusion hides real drift in a file that no longer exists."""
    for name, reason in CS_EXCLUDED_FILES.items():
        assert reason.strip(), f"{name} is excluded without a reason"
        matches = [p for p in _iter_csharp_files() if p.name == name]
        assert matches, (
            f"{name} is listed in CS_EXCLUDED_FILES but no such file exists. "
            f"Delete the entry - a stale exclusion hides real drift."
        )


# ------------------------------------------------------------- drift checks


@pytest.mark.parametrize(
    "flag",
    sorted(set(CSHARP_FLAGS) - set(PYTHON_FLAGS)),
    ids=lambda f: f,
)
def test_csharp_flag_is_defined_by_the_python_parser(flag: str) -> None:
    where = ", ".join(sorted(CSHARP_FLAGS[flag]["where"]))
    assert False, (
        f"{flag!r} is passed to the backend from {where} but no Python "
        f"argparse parser defines it. The two sides hard-code these literals "
        f"independently, so this compiles fine on the C# side and fails only at "
        f"runtime - as an empty result, because the backend runs as a child "
        f"process. Either add the flag to sms_tool/cli.py, or - if it is not a "
        f"backend flag at all - add the file to CS_EXCLUDED_FILES with a reason."
    )


@pytest.mark.parametrize(
    "flag,value",
    sorted(
        (flag, value)
        for flag, data in CSHARP_FLAGS.items()
        for value in data["values"]
        if flag in PYTHON_FLAGS
        and PYTHON_FLAGS[flag][1] is not None
        and value not in PYTHON_FLAGS[flag][1]
    ),
    ids=lambda v: str(v),
)
def test_csharp_passes_a_value_the_python_parser_accepts(flag: str, value: str) -> None:
    _where, choices = PYTHON_FLAGS[flag]
    assert False, (
        f"{flag!r} is passed the literal {value!r} from C#, but the Python "
        f"parser only accepts {sorted(choices)}. argparse will reject this at "
        f"runtime with a usage error, and because the backend is a child "
        f"process the front end just sees no result."
    )
