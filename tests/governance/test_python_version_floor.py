"""One Python floor, one source, every claim site checked.

Why this exists
---------------
On 2026-08-31 an audit recorded the gap: CI ran a single ``3.12`` job, ``ruff``
targeted ``py312`` and ``pyright`` checked ``3.12``, while ``README`` promised
"Python 3.10 or later" and ``scripts/preflight_env.py`` refused anything below
3.10. Nothing verified 3.10, so a future commit could have used a 3.12-only
construct and only a user on 3.10 would have found out.

``scripts/runtime_manifest.json`` is the authority: it names the interpreter the
installer bundles, and ``test_runtime_manifest.py`` already ties it to CI and the
ruff target. This file closes the rest of the surface -- every place that states
a floor, including the two user-facing copies (README and the installer's
"Python missing" dialog) that no other test reads.

The floor is declared as ``>=3.12`` because that is what is actually tested:
3.10/3.11 support was a promise with no job behind it. Making it true again (a CI
matrix plus ``target-version = "py310"``) is a deliberate change, and this test
will demand it everywhere at once rather than in one forgotten file.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "scripts" / "runtime_manifest.json").read_text(encoding="utf-8"))

#: `MAJOR.MINOR` the bundled interpreter publishes, e.g. ``3.12``.
FLOOR = MANIFEST["python_version"]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def _search(pattern: str, relative: str) -> str:
    match = re.search(pattern, _text(relative))
    assert match, f"{relative}: no match for {pattern!r} -- the claim moved or was deleted"
    return ".".join(match.groups())


def _preflight_floor() -> str:
    return _search(r"if \(major, minor\) >= \((\d+), (\d+)\)", "scripts/preflight_env.py")


def _installer_floor() -> str:
    return _search(r"Python (\d+\.\d+)\+", "scripts/installer/Program.cs")


def _readme_claims(relative: str, requirements_word: str) -> tuple[str, str]:
    badge = _search(r"badge/Python-(\d+\.\d+)%2B-", relative)
    requirements = _search(rf"- Python (\d+\.\d+) {requirements_word}", relative)
    return badge, requirements


def collect_claims() -> dict[str, str]:
    """Every place that names the floor, keyed by a human-readable label."""
    pyproject = tomllib.loads(_text("pyproject.toml"))
    pyright = json.loads(_text("pyrightconfig.json"))

    badge_zh, requirements_zh = _readme_claims("README.md", "或更高版本")
    badge_en, requirements_en = _readme_claims("README_EN.md", "or later")

    return {
        "pyproject.toml [project] requires-python": pyproject["project"]["requires-python"],
        "pyproject.toml [tool.ruff] target-version": pyproject["tool"]["ruff"]["target-version"],
        "pyrightconfig.json pythonVersion": pyright["pythonVersion"],
        ".github/workflows/ci.yml python-version": _search(r'python-version: "(\d+\.\d+)"', ".github/workflows/ci.yml"),
        "README.md badge": badge_zh,
        "README.md requirements line": requirements_zh,
        "README_EN.md badge": badge_en,
        "README_EN.md requirements line": requirements_en,
        "scripts/preflight_env.py runtime floor": _preflight_floor(),
        "scripts/installer/Program.cs dialog": _installer_floor(),
    }


def _normalize(label: str, value: str) -> str:
    """Reduce each claim to the one comparable thing it states."""
    if label.endswith("target-version"):
        return value.removeprefix("py")[:1] + "." + value.removeprefix("py")[1:]
    if label.endswith("requires-python"):
        return value.removeprefix(">=")
    return value


def test_every_claim_names_the_same_minor_version():
    mismatches = {label: value for label, value in collect_claims().items() if _normalize(label, value) != FLOOR}
    assert not mismatches, (
        f"the Python floor is {FLOOR} (scripts/runtime_manifest.json) but these claims disagree:\n"
        + "\n".join(f"  - {label}: {value}" for label, value in mismatches.items())
        + "\nChange the manifest first, then make every site agree."
    )


def test_the_floor_is_at_least_the_lowest_version_the_code_can_run_on():
    """A floor below 3.10 would be a typo, not a decision (``X | Y`` annotations)."""
    major, minor = (int(part) for part in FLOOR.split("."))
    assert (major, minor) >= (3, 10)


def test_the_installer_and_the_manifest_ship_the_same_interpreter():
    """The dialog text and the bundled archive must not drift apart."""
    assert MANIFEST["python_full_version"].startswith(FLOOR + ".")
    assert FLOOR in MANIFEST["asset"]
