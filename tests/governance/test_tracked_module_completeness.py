"""Every module the committed tree imports must itself be committed.

Why this exists
---------------
`.gitignore` ignores ``_*.py`` in every directory, on purpose: the one-off
diagnostic scripts that rule was added for often carry inline credentials. The
cost is that a *legitimate* module whose name happens to start with an underscore
is dropped from a commit **silently** -- ``git add`` prints nothing, the local
suite still passes because the file is on disk, and only a clean checkout fails.

That happened twice:

* 2026-10-22 -- ``sms_tool/config/_state.py`` was dropped, so the committed tree
  could not import ``sms_tool.config`` at all.
* 2026-11-20 -- ``scripts/_baseline.py`` (the shared baseline plumber) was never
  committed. ``tests/governance/test_baseline_helpers.py`` and
  ``test_unittest_style_ratchet.py`` import it, so collection in CI died with
  ``ModuleNotFoundError: No module named '_baseline'`` while the full local suite
  was green at 7692 passed.

Both were caught the same way -- diffing the working tree against ``git ls-files``
-- and both were repaired by a ``!`` exception in `.gitignore`. This file is that
diff, run every time. It asks git rather than parsing `.gitignore`, because the
question is "what is in the commit", not "what does the pattern look like".

A file that is imported but missing from the commit is the failure; a file that
is neither tracked nor imported (the one-off scripts the rule exists for) is not
this test's business.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git_z(*args: str) -> list[str]:
    """Run a git command and split its NUL-separated output (paths with spaces)."""
    proc = subprocess.run(
        ["git", *args, "-z"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return [entry for entry in proc.stdout.split("\0") if entry]


def _tracked_paths() -> set[str]:
    return set(_git_z("ls-files"))


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _module_file(base: Path, name: str) -> Path | None:
    flat = base / f"{name}.py"
    if flat.is_file():
        return flat
    package = base / name / "__init__.py"
    return package if package.is_file() else None


def test_every_imported_module_in_the_tree_is_committed():
    tracked = _tracked_paths()
    missing: list[str] = []

    for rel in sorted(path for path in tracked if path.endswith(".py")):
        importer = ROOT / rel
        try:
            tree = ast.parse(importer.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - a tracked file that will not parse
            continue
        for name in sorted(_imported_top_level_names(tree)):
            for base in (importer.parent, ROOT):
                found = _module_file(base, name)
                if found is None:
                    continue
                found_rel = found.relative_to(ROOT).as_posix()
                if found_rel not in tracked:
                    missing.append(
                        f"{rel} imports {name!r}, which lives in {found_rel} -- the file exists "
                        "on disk but is not in the commit (a `.gitignore` `_*.py` exception is missing)"
                    )
                break

    assert not missing, "the committed tree cannot import itself:\n  - " + "\n  - ".join(missing)
