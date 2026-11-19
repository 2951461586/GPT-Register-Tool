"""The CI environment gate must ask the owner, not restate the answer.

``scripts/environment_preflight.ps1`` runs before anything else in CI. On
2026-11-19 it rejected a healthy ``curl_cffi 0.17.0`` install -- the dependency
bump had moved ``requirements.txt``, ``auth_headers.curl_cffi_capabilities`` and
``doctor`` together, and this PowerShell copy still said
``^0\\.(15|16)(\\.|$)`` plus a hardcoded ``chrome146``. That is the third copy of
one fact and the second time it drifted; the fix was to make the script read
``CURL_CFFI_MIN_MINOR`` / ``CURL_CFFI_MAX_MINOR`` and the newest
``AUTH_FINGERPRINT_PROFILES`` entry instead.

These tests pin the *shape* of that fix, because a script has no type checker:
a re-added literal is invisible until CI runs the whole Windows suite.

It also pins the pipeline-shape bug next door: piping a native command into
``Select-Object -First 1`` raises ``StopUpstreamCommandsException`` under
Windows PowerShell 5.1, terminates the child and leaves ``$LASTEXITCODE`` at
``-1``. That turned "is python runnable" into a coin flip locally while CI
happened to pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from sms_tool.auth_headers import (
    AUTH_FINGERPRINT_PROFILES,
    CURL_CFFI_MAX_MINOR,
    CURL_CFFI_MIN_MINOR,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "environment_preflight.ps1"
README = ROOT / "README.md"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _newest_chrome_profile() -> str:
    chrome = [
        name for name in AUTH_FINGERPRINT_PROFILES if name.startswith("chrome") and name[len("chrome") :].isdigit()
    ]
    assert chrome, "the profile table lost its Chrome family"
    return max(chrome, key=lambda name: int(name[len("chrome") :]))


def test_the_script_reads_the_range_from_the_module_that_enforces_it() -> None:
    text = _script()
    assert "CURL_CFFI_MIN_MINOR" in text
    assert "CURL_CFFI_MAX_MINOR" in text


def test_the_script_reads_the_required_profile_from_the_table() -> None:
    text = _script()
    assert "AUTH_FINGERPRINT_PROFILES" in text
    assert _newest_chrome_profile() not in text, (
        "the newest Chrome profile is spelled out in the script again; it must come from AUTH_FINGERPRINT_PROFILES"
    )


def test_no_hardcoded_curl_cffi_version_bracket_survives() -> None:
    text = _script()
    for pattern in (r"\^0\\\.\(1\d", r"0\.15\.x or", r"0\.16\.x", r"0\.17\.x"):
        assert re.search(pattern, text) is None, f"hardcoded curl_cffi range in environment_preflight.ps1: {pattern}"


def test_no_native_command_is_piped_into_select_first() -> None:
    """The PS 5.1 quirk that made the python check flaky -- see the module docstring."""
    text = _script()
    assert re.search(r"\(& (python|dotnet)[^)]*\)? \| Select-Object -First", text) is None


def test_the_readme_states_the_same_range_as_the_code() -> None:
    """README is the operator-facing copy; it drifts as easily as the script."""
    match = re.search(r"curl_cffi==([0-9.]+)`.{0,40}?（0\.(\d+)–0\.(\d+)）", README.read_text(encoding="utf-8"))
    assert match, "README no longer states the accepted curl_cffi range next to the pin"
    assert int(match.group(2)) == CURL_CFFI_MIN_MINOR
    assert int(match.group(3)) == CURL_CFFI_MAX_MINOR


def test_the_readme_pin_matches_the_declared_dependency() -> None:
    readme_pin = re.search(r"curl_cffi==([0-9.]+)", README.read_text(encoding="utf-8"))
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    constraints = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert readme_pin, "README stopped naming the curl_cffi pin"
    pinned = readme_pin.group(1)
    assert f"curl_cffi=={pinned}" in requirements
    assert f"curl_cffi=={pinned}" in constraints
