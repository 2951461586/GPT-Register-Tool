"""Guard: a WPF *decision* must not compare localized display text.

The account grid renders every row through ``Loc.T``: in the English build a
status cell reads ``"Registered"`` where the Chinese build reads ``"已注册"``.
A predicate that decides something about the row -- is it registered, does it
need attention, may it be exported -- therefore changed behaviour the moment the
operator switched language, because it compared the Chinese source literal.

Measured 2026-10-25: switching to English dropped the registered counter to
zero and made ``RegisterViewModel.LocalUnregisteredVerdict`` report
already-registered mailboxes as pending (the backend's
``registration.skip_registered_mailboxes`` masked the worst consequence, but the
"可注册 N 个" count and the wasted slots did not recover).

**Scope is the decision layer, deliberately.** ``Views/AccountGridPresentation.cs``
also compares these labels, but only to pick a badge *colour*, and it ships both
languages by hand; colour is not a business decision, so it is out of scope here.
Every decision goes through :meth:`Loc.DisplayContains`, which matches the marker
in every shipped language regardless of the active one.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = ROOT / "SmsWorkbench"

#: Files that decide something about an account row. Missing file == guard went
#: vacuous, so the test fails instead of silently checking nothing.
DECISION_FILES = (
    "ViewModels/RegisterViewModel.cs",
    "ViewModels/PoolViewModel.cs",
    "ViewModels/ExportViewModel.cs",
    "Views/RegistrationStatusPresentation.cs",
)

#: Display labels that appear in (localized) row status text.
MARKERS = ("已注册", "支付完成", "待支付", "已导入", "PM已创建", "待", "缺", "失败")

_CONTAINS_RE = re.compile(r"\.Contains\(\s*(?P<prefix>[$@]*)(?P<literal>\"[^\"]*\"|'[^']*')")


def _strip_line_comment(line: str) -> str:
    """Drop a trailing ``//`` comment that is not inside a string literal.

    The files under guard explain the rule in comments that quote the old
    ``status.Contains("已注册")`` form, so comments must not count as offenders.
    """
    in_string = False
    in_char = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif in_char:
            if char == "\\":
                index += 2
                continue
            if char == "'":
                in_char = False
        elif char == '"':
            in_string = True
        elif char == "'":
            in_char = True
        elif char == "/" and line[index : index + 2] == "//":
            return line[:index]
        index += 1
    return line


def offenders(source: str) -> list[tuple[int, str]]:
    """``(line number, matched literal)`` for every localized-literal decision."""
    found: list[tuple[int, str]] = []
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = _strip_line_comment(raw)
        if "Loc.DisplayContains" in line:
            continue
        for match in _CONTAINS_RE.finditer(line):
            literal = match.group("literal")
            if any(marker in literal for marker in MARKERS):
                found.append((lineno, literal))
    return found


def test_no_decision_compares_localized_status_text():
    problems: list[str] = []
    for relative in DECISION_FILES:
        path = WORKBENCH / relative
        assert path.is_file(), f"guarded decision file is missing: {relative}"
        for lineno, literal in offenders(path.read_text(encoding="utf-8")):
            problems.append(f"{relative}:{lineno} compares {literal} instead of Loc.DisplayContains")
    assert not problems, (
        "a decision compared localized display text; use Loc.DisplayContains(status, marker) "
        "so the result does not depend on the UI language:\n  " + "\n  ".join(problems)
    )


def test_guard_detects_a_planted_decision():
    planted = "            return status.Contains(\"已注册\") || status.Contains('待');"
    assert offenders(planted), "the guard no longer detects the defect class it exists for"


def test_guard_accepts_the_language_agnostic_form():
    safe = '            return Loc.DisplayContains(status, "已注册") || Loc.DisplayContains(status, "待");'
    assert offenders(safe) == []


def test_guard_ignores_comments_and_unrelated_containers():
    source = "\n".join(
        [
            '            // status.Contains("已注册") was the old, language-dependent form',
            '            string marker = "失败率";',
            "            return rows.Any(r => r.Identifier.Contains(term));",
        ]
    )
    assert offenders(source) == []
