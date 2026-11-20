"""The ``cache:`` values the pinned setup actions actually accept.

``actions/setup-python`` documents ``pip`` / ``pipenv`` / ``poetry`` / ``uv`` — or
no ``cache`` input at all. ``cache: false`` *reads* like "no cache" but fails at
runtime with ``Caching for 'false' is not supported`` and kills the job ~40 s in.
That is exactly how the first tag-driven release died on 2026-11-20 (run
44717803977): ``.github/workflows/release.yml`` carried ``cache: false`` on
setup-python, and no local gate reads workflow inputs — the failure only exists
on the runner.

``actions/setup-dotnet`` does take a boolean there, which is why ``ci.yml``'s
`cache: false` for NuGet is legitimate and stays.

Parsing is indentation-only on purpose: these are two small workflow files, and a
YAML dependency is not worth it for a gate that must run before dependencies are
installed.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml"))

#: Accepted ``cache:`` values per pinned action. ``None`` means "omit the
#: input"; anything else must be one of these strings.
ACCEPTED_CACHE_VALUES: dict[str, set[str]] = {
    "actions/setup-python": {"pip", "pipenv", "poetry", "uv"},
    "actions/setup-dotnet": {"true", "false"},
    "actions/setup-node": {"npm", "yarn", "pnpm"},
}


def cache_inputs(path: Path) -> list[tuple[str, str, int]]:
    """Return ``(action, value, line_number)`` for every setup step's ``cache:``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[str, str, int]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- uses:"):
            continue
        action = stripped[len("- uses:") :].strip().split("@", 1)[0]
        if action not in ACCEPTED_CACHE_VALUES:
            continue
        indent = len(line) - len(line.lstrip())
        for offset, follow in enumerate(lines[index + 1 :], start=index + 2):
            if not follow.strip():
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            key, _, value = follow.strip().partition(":")
            if key == "cache":
                found.append((action, value.strip().strip("\"'"), offset))
    return found


def test_workflows_are_discovered():
    """防假绿：路径写错会让下面的比较退化成空集通过。"""
    assert WORKFLOWS, "no .github/workflows/*.yml found"
    assert any(cache_inputs(path) for path in WORKFLOWS), (
        "no setup-action cache input found in any workflow — the parser or the paths broke"
    )


def test_setup_action_cache_inputs_use_accepted_values():
    offenders = [
        f"{path.name}:{line} {action} cache: {value!r}"
        for path in WORKFLOWS
        for action, value, line in cache_inputs(path)
        if value.lower() not in ACCEPTED_CACHE_VALUES[action]
    ]
    assert not offenders, (
        "an action input fails on the runner, not here:\n  "
        + "\n  ".join(offenders)
        + "\nOmit the input to disable caching (setup-python rejects 'false')."
    )
