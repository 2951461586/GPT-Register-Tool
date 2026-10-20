"""Gate: the Python tree must stay ``ruff format``-clean.

Why this exists
---------------
``ruff format --check`` was never run on ``sms_tool/auth_flow/`` and
``sms_tool/registration_*.py``, so on 2026-10-08 a mangled tuple survived review
in ``auth_flow/otp.py``::

    return (
{        "ok": False,
        "error": f"existing_login_landed_on_profile_step:{final_url[:120]}",
    }        , None)

It is syntactically valid, so every existing gate was blind to it --
``ruff check`` (the configured rule set is ``E9/F63/F7/F82`` only), ``compileall``
and the whole pytest suite all pass a file whose layout no longer resembles its
intent.

Scope history
-------------
Scope was the registration lane until 2026-10-20, deliberately: widening it then
would have reformatted ~430 unrelated files in one commit and buried the real
edit. On 2026-10-20 that reformat was done as its own commit (with the ruff
version pinned in ``constraints-tools.txt``) and the scope widened to **every
Python tree this repository owns**, because a lane-only gate leaves the other
~700 files free to drift the same way. ``MIXED`` scopes are still data
(``SCOPE_DIRS`` / ``SCOPE_GLOBS``), so excluding a tree again is a one-line edit
rather than a command-line change in three places.

Standard library only; shells out to ``python -m ruff format`` with *this*
interpreter, so the tool pinned in ``constraints-tools.txt`` is the one that
runs. A missing ``ruff`` fails the gate rather than silently passing it -- a
gate that skips itself is decoration.

Usage:
    python scripts/format_guard.py            # check the tree (gate)
    python scripts/format_guard.py --list     # print the scope and exit
    python scripts/format_guard.py --fix      # reformat (developer convenience)
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Directories whose whole ``.py`` tree is in scope. Every Python tree this
#: repository owns: the application package, the subprocess services, the
#: operator scripts and the offline test suite.
SCOPE_DIRS = ("sms_tool", "services", "scripts", "tests")

#: File globs (relative to the repo root) that are in scope, on top of
#: ``SCOPE_DIRS``. Empty today -- here so a future tree can be included without
#: widening a directory.
SCOPE_GLOBS: tuple[str, ...] = ()

_SKIP_DIR_NAMES = {"__pycache__"}


def scope_paths(root: Path = ROOT) -> list[str]:
    """Every in-scope ``.py`` file as a sorted repo-relative posix path."""
    found: set[str] = set()
    for relative in SCOPE_DIRS:
        base = root / relative
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if _SKIP_DIR_NAMES & set(path.parts):
                continue
            found.add(path.relative_to(root).as_posix())
    for pattern in SCOPE_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and path.suffix == ".py" and not (_SKIP_DIR_NAMES & set(path.parts)):
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


#: Windows caps a process command line at 32,767 characters. Handing every
#: scoped path (765 files) to one ``ruff format`` invocation fails with
#: ``WinError 206: filename or extension is too long`` -- measured on 2026-10-20
#: right after the scope widened. Batches keep the command line short; the
#: number is chosen so a typical repo path fits with room to spare.
MAX_PATHS_PER_INVOCATION = 60


def _run_ruff(paths, *, fix: bool, root: Path) -> tuple[int, str]:
    """Run one ``ruff format`` invocation over *paths*."""
    command = [sys.executable, "-m", "ruff", "format"]
    if not fix:
        command.append("--check")
    command.extend(str(Path(root) / path) for path in paths)
    completed = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        # Pin the codec: on Windows the default is the locale codec (GBK here),
        # and ruff's diff output contains bytes it cannot decode -- the reader
        # thread then dies and the captured output is lost, which would report
        # "BLOCKED" with no explanation.
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def check(paths, *, fix: bool = False, root: Path = ROOT) -> tuple[int, str]:
    """Run ruff over *paths* in batches; return ``(returncode, combined_output)``.

    ``--check`` stops at the first failing batch (one counter-example is enough to
    block); ``--fix`` walks every batch, because the caller wants the whole tree
    reformatted, and keeps the first non-zero code.
    """
    paths = list(paths)
    returncode = 0
    output = ""
    for start in range(0, len(paths), MAX_PATHS_PER_INVOCATION):
        batch = paths[start : start + MAX_PATHS_PER_INVOCATION]
        code, text = _run_ruff(batch, fix=fix, root=root)
        if code != 0:
            returncode = code
            output += text
            if not fix:
                break
    return returncode, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the scope and exit")
    parser.add_argument("--fix", action="store_true", help="reformat instead of checking")
    args = parser.parse_args(argv)

    paths = scope_paths()
    if args.list:
        for path in paths:
            print(path)
        return 0

    if not paths:
        print("format-guard: scope is empty -- the trees were renamed or moved", file=sys.stderr)
        return 1

    if importlib.util.find_spec("ruff") is None:
        print(
            "format-guard: ruff is not installed in this interpreter; the tree cannot be checked.\n"
            "  Install it with: python -m pip install ruff -c constraints-tools.txt",
            file=sys.stderr,
        )
        return 1

    returncode, output = check(paths, fix=args.fix)
    if returncode == 0:
        action = "reformatted" if args.fix else "clean"
        print(f"format-guard: {action} ({len(paths)} files in the Python tree)")
        return 0

    header = "reformatted what it could" if args.fix else "would reformat"
    print(f"format-guard: BLOCKED -- the Python tree is not ruff-format clean ({header})", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print(output.rstrip(), file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print("Fix with: python scripts/format_guard.py --fix", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
