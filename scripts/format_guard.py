"""Gate: the protocol-registration lane must stay ``ruff format``-clean.

Why this exists
---------------
``sms_tool/auth_flow/`` and ``sms_tool/registration_*.py`` are the protocol
registration lane.  ``ruff format --check`` was never run on them, so on
2026-10-08 a mangled tuple survived review in ``auth_flow/otp.py``::

    return (
{        "ok": False,
        "error": f"existing_login_landed_on_profile_step:{final_url[:120]}",
    }        , None)

It is syntactically valid, so every existing gate was blind to it --
``ruff check`` (the configured rule set is ``E9/F63/F7/F82`` only), ``compileall``
and the whole pytest suite all pass a file whose layout no longer resembles its
intent.

Scope is deliberately the *lane*, not the whole package: widening it to
``sms_tool/`` would reformat ~100 unrelated files in one commit and bury the
real edit.  It is data (``SCOPE_DIRS`` / ``SCOPE_GLOBS``), so a new registration
module is covered by adding it in one place rather than by editing a command
line in three.

Standard library only; shells out to ``python -m ruff format`` with *this*
interpreter, so the tool pinned in ``constraints-tools.txt`` is the one that
runs.  A missing ``ruff`` fails the gate rather than silently passing it -- a
gate that skips itself is decoration.

Usage:
    python scripts/format_guard.py            # check the lane (gate)
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

#: Directories whose whole ``.py`` tree is in scope.
SCOPE_DIRS = ("sms_tool/auth_flow",)

#: File globs (relative to the repo root) that are in scope.
SCOPE_GLOBS = ("sms_tool/registration_*.py",)

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


def check(paths, *, fix: bool = False, root: Path = ROOT) -> tuple[int, str]:
    """Run ruff on *paths*; return ``(returncode, combined_output)``."""
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
        print("format-guard: scope is empty -- the lane was renamed or moved", file=sys.stderr)
        return 1

    if importlib.util.find_spec("ruff") is None:
        print(
            "format-guard: ruff is not installed in this interpreter; the lane cannot be checked.\n"
            "  Install it with: python -m pip install ruff -c constraints-tools.txt",
            file=sys.stderr,
        )
        return 1

    returncode, output = check(paths, fix=args.fix)
    if returncode == 0:
        action = "reformatted" if args.fix else "clean"
        print(f"format-guard: {action} ({len(paths)} files in the protocol-registration lane)")
        return 0

    header = "reformatted what it could" if args.fix else "would reformat"
    print(
        f"format-guard: BLOCKED -- the protocol-registration lane is not ruff-format clean ({header})", file=sys.stderr
    )
    print("-" * 100, file=sys.stderr)
    print(output.rstrip(), file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print("Fix with: python scripts/format_guard.py --fix", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
