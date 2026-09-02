"""G3 - keep ``requirements.txt`` / ``constraints.txt`` honest in both directions.

Why this exists
---------------
Two real failures in this repo were dependency-declaration bugs, and both were
invisible until something ran on a machine that had been installed from scratch:

1. **``nodriver`` was imported at two sites but never declared.** The whole
   undetected-Chrome payment fallback silently degraded to "not installed" on
   every fresh install, while working fine on the developer's box where the
   package happened to be present.
2. **``selenium`` was declared but referenced nowhere** (removed 2026-09-02).
   A stale declaration is worse than no declaration: it makes every audit of
   "what does this project need" wrong, and it hides the real set.

Both are one-line mistakes that no unit test can catch, because a unit test runs
in whatever environment happens to be installed. This is a static pass, and CI
already runs pytest, so it runs on every push.

Design notes (why this will not become noise)
---------------------------------------------
The check is deliberately approximate in the *miss* direction; a noisy guard
gets ``--no-verify``'d into irrelevance, which is worse than a quiet one.

  * the local/third-party split is done by *resolving* the import, not by
    filename. That matters: ``sms_tool/registration_drivers/playwright.py``
    exists, so a filename-based check would classify the real ``playwright``
    package as first-party and drop every finding about it. Conversely the
    ``services/protocol-payment/*`` scripts import siblings by bare name
    (``import common``, ``import pix_core``) that only resolve because the
    script puts its own directory on ``sys.path`` - those resolve to nothing, so
    they fall back to "does a file with this stem exist in the repo?";
  * packages that are legitimately declared but never imported live in
    ``DECLARED_NOT_IMPORTED`` with a written reason - an unexplained entry is a
    bug in this guard;
  * stdlib is excluded via ``sys.stdlib_module_names``;
  * ``dist/`` and ``runtime/`` are excluded. They are build/runtime copies of the
    same source tree; scanning them triples the noise and, worse, makes
    "declared but unused" findings vanish because the copies import everything.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
CONSTRAINTS = ROOT / "constraints.txt"

SCAN_ROOTS = ("sms_tool", "services")
SKIP_PARTS = {"__pycache__", ".venv", "dist", "runtime", "scripts", ".git", "tests"}

STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
LOCAL_PACKAGE_PREFIXES = ("sms_tool", "services")

# import name -> distribution name, for the cases where they differ.
MODULE_TO_DIST = {
    "nacl": "pynacl",
    "playwright_stealth": "playwright-stealth",
}

# Declared but never imported by first-party source. Each entry needs a reason:
# an unexplained entry is a bug in this guard, not a dependency.
DECLARED_NOT_IMPORTED: dict[str, str] = {
    "httpx": (
        "transitive: arrives via cloakbrowser. Pinned deliberately to bound that "
        "closure - see the note in constraints.txt."
    ),
    "pytest": "test runner, invoked as `pytest`, never imported by source.",
    "pytest-cov": "pytest plugin, selected with --cov on the command line.",
}


# ------------------------------------------------------------------- parsing


def _normalise(name: str) -> str:
    """PEP 503 normalisation: lowercase, runs of -_. collapse to one hyphen."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _parse_requirement_lines(path: Path) -> dict[str, str]:
    """{normalised dist name: raw specifier} from a pip requirements file."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", line)
        if not match:
            continue
        name, _extras, spec = match.groups()
        out[_normalise(name)] = spec.strip()
    return out


_PRUNE = {"__pycache__", "dist", "runtime", ".venv", ".git", "node_modules", "logs"}


def _walk_repo() -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk over the repo with the heavy trees pruned.

    ``runtime/`` alone holds >20k files; walking into it (and into ``.venv``)
    costs seconds and yields nothing but false positives.
    """
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE]
        yield Path(dirpath), dirnames, filenames


def _repo_module_stems() -> set[str]:
    """Bare module names backed by a file *or a package* inside this repository.

    Only consulted for imports that resolve to *nothing* from the repository
    root. The ``services/protocol-payment/*`` scripts insert ``PROTOCOL_ROOT``
    onto ``sys.path`` and then import siblings by bare name - ``common`` is a
    package there (``services/protocol-payment/common/__init__.py``), ``pix_core``
    is a module. Both are first-party but unresolvable without that path hack,
    so a resolution-only check would flag them.
    """
    names: set[str] = set()
    for dirpath, dirnames, filenames in _walk_repo():
        for name in filenames:
            if name.endswith(".py"):
                names.add(name[:-3])
        if "__init__.py" in filenames:
            names.add(dirpath.name)
    return names


def _resolves_outside_the_repo(name: str) -> bool | None:
    """True = resolves to a package outside the repo, False = inside, None = no.

    ``None`` means "cannot be resolved at all", which is the interesting third
    case: it covers both a first-party script on a self-added ``sys.path`` entry
    and a third-party package that simply is not installed. The caller
    distinguishes them with :func:`_repo_module_stems`.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError, TypeError):
        return None
    if spec is None:
        return None
    candidates = [spec.origin]
    candidates.extend(spec.submodule_search_locations or [])
    for raw in candidates:
        if not raw:
            continue
        # site-packages must be tested *before* the repo check: this project's
        # venv lives at <repo>/.venv, so every installed package would otherwise
        # look like a first-party module sitting inside the repository.
        if "site-packages" in str(raw).replace("\\", "/").lower():
            return True
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            return True  # outside the repo -> a real distribution
        return False  # inside the repo -> first-party
    return None


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if not SKIP_PARTS & set(p.parts))
    return sorted(files)


def _iter_imported_top_level_names() -> list[tuple[str, Path]]:
    """(top-level import name, file) for every absolute import in shipped source."""
    out: list[tuple[str, Path]] = []
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            continue  # unparseable source must not break the guard
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import - always first-party
                    continue
                tops = [(node.module or "").split(".")[0]]
            else:
                continue
            for top in tops:
                if not top or top in STDLIB or top in LOCAL_PACKAGE_PREFIXES:
                    continue
                out.append((top, path))
    return out


def _imported_third_party() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(resolved distributions, unresolvable names) keyed by normalised name.

    Unresolvable names that are also not backed by a file in this repo are the
    dangerous ones: they are third-party imports of a package that is not even
    installed. Reporting them is what catches the ``nodriver`` class of bug on a
    machine where the package is absent - precisely where a runtime test cannot.
    """
    stems = _repo_module_stems()
    resolved: dict[str, list[str]] = {}
    unresolvable: dict[str, list[str]] = {}
    for top, path in _iter_imported_top_level_names():
        rel = path.relative_to(ROOT).as_posix()
        verdict = _resolves_outside_the_repo(top)
        if verdict is None and top in stems:
            continue  # first-party script on a self-added sys.path entry
        dist = _normalise(MODULE_TO_DIST.get(top, top))
        bucket = resolved if verdict else unresolvable
        bucket.setdefault(dist, [])
        if rel not in bucket[dist]:
            bucket[dist].append(rel)
    return resolved, unresolvable


REQUIREMENTS_MAP = _parse_requirement_lines(REQUIREMENTS)
CONSTRAINTS_MAP = _parse_requirement_lines(CONSTRAINTS)
IMPORTED, UNRESOLVABLE = _imported_third_party()


# ------------------------------------------------------------------- meta


def test_guard_parsed_a_real_dependency_set() -> None:
    """Meta-check: a broken parser would make every assertion below vacuous."""
    assert len(REQUIREMENTS_MAP) >= 10, f"parsed too few requirements: {REQUIREMENTS_MAP}"
    assert len(_iter_source_files()) > 100, "the source scanner found almost no files"


def test_guard_resolved_a_real_dependency_set() -> None:
    """Meta-check: the environment must actually have the deps installed.

    Resolution is how local and third-party imports are told apart, so in an
    environment where nothing is installed every import falls into the
    "unresolvable" bucket and the findings below are meaningless. Fail loudly
    instead of passing vacuously.
    """
    assert len(IMPORTED) >= 8, (
        f"only {len(IMPORTED)} imports resolved to an installed distribution "
        f"({sorted(IMPORTED)}). Install requirements.txt before running the "
        f"suite, or this guard is not checking anything."
    )


def test_module_to_dist_map_has_no_stale_entries() -> None:
    """An entry naming a module that is no longer imported is dead weight."""
    imported_tops = {top for top, _ in _iter_imported_top_level_names()}
    stale = sorted(set(MODULE_TO_DIST) - imported_tops)
    assert not stale, (
        f"MODULE_TO_DIST maps module names nothing imports any more: {stale}. "
        f"Drop them - a stale alias silently makes the guard pass."
    )


# ------------------------------------------------------- both directions


@pytest.mark.parametrize(
    "dist",
    sorted(set(REQUIREMENTS_MAP) - set(IMPORTED) - set(UNRESOLVABLE)
           - set(DECLARED_NOT_IMPORTED)),
    ids=lambda d: d,
)
def test_declared_dependency_is_actually_imported(dist: str) -> None:
    assert False, (
        f"requirements.txt declares {dist!r}{REQUIREMENTS_MAP[dist]} but no "
        f"first-party module imports it. This is how `selenium` survived audits "
        f"for months: declared, pinned in constraints.txt, and referenced by "
        f"nothing at all. Delete it, or - if it really is needed - add it to "
        f"DECLARED_NOT_IMPORTED with a reason."
    )


@pytest.mark.parametrize(
    "dist",
    sorted((set(IMPORTED) | set(UNRESOLVABLE)) - set(REQUIREMENTS_MAP)),
    ids=lambda d: d,
)
def test_imported_dependency_is_declared(dist: str) -> None:
    files = sorted(IMPORTED.get(dist) or UNRESOLVABLE[dist])[:3]
    where = ", ".join(files)
    if dist in UNRESOLVABLE:
        detail = (
            f" It is not installed in this environment either, so no test can "
            f"catch it - which is exactly the situation this guard exists for."
        )
    else:
        detail = ""
    assert False, (
        f"{dist!r} is imported by {where} but is not declared in "
        f"requirements.txt. This is exactly the `nodriver` bug: it worked on the "
        f"developer's machine and silently degraded to 'not installed' on every "
        f"fresh install. Add it to requirements.txt and pin it in "
        f"constraints.txt.{detail}"
    )


def test_declared_not_imported_entries_are_all_still_true() -> None:
    """Guard the allowlist: an entry nobody needs any more must be deleted."""
    stale = sorted(set(DECLARED_NOT_IMPORTED) - set(REQUIREMENTS_MAP))
    assert not stale, (
        f"DECLARED_NOT_IMPORTED lists {stale}, which requirements.txt no longer "
        f"declares. Remove the entry - a stale allowlist hides a real finding."
    )
    for dist in DECLARED_NOT_IMPORTED:
        if dist in IMPORTED or dist in UNRESOLVABLE:
            # Not fatal, but it means the exemption is doing nothing.
            print(f"[dependency-guard] {dist} is now imported; drop its exemption")


# ------------------------------------------- requirements <-> constraints


def test_requirements_and_constraints_declare_the_same_packages() -> None:
    only_req = sorted(set(REQUIREMENTS_MAP) - set(CONSTRAINTS_MAP))
    only_con = sorted(set(CONSTRAINTS_MAP) - set(REQUIREMENTS_MAP))
    assert not only_req, (
        f"declared in requirements.txt but not pinned in constraints.txt: "
        f"{only_req}. An unpinned direct dependency makes installs "
        f"non-reproducible - that is the entire point of constraints.txt."
    )
    assert not only_con, (
        f"pinned in constraints.txt but not declared in requirements.txt: "
        f"{only_con}. A constraint without a requirement is dead weight."
    )


def test_constraints_versions_satisfy_requirements_specifiers() -> None:
    """A pin that contradicts its own requirement breaks `pip install -c`."""
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - packaging ships with pip
        pytest.skip("packaging is not available")

    problems: list[str] = []
    for dist, spec in REQUIREMENTS_MAP.items():
        pinned = CONSTRAINTS_MAP.get(dist)
        if not pinned:
            continue
        version = pinned.lstrip("=~<>!").strip()
        if not version:
            continue
        requirement = Requirement(f"{dist}{spec}") if spec else Requirement(dist)
        if not requirement.specifier.contains(version):
            problems.append(
                f"{dist}: requirements says '{spec or 'any'}' but constraints "
                f"pins '{pinned}'"
            )
    assert not problems, "contradictory version declarations:\n  " + "\n  ".join(problems)
