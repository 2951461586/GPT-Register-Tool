"""Install (or remove) the repository's pre-commit credential guard.

The guard lives in ``.githooks/`` rather than ``.git/hooks/`` so that it is
tracked in git and survives a fresh clone -- but ``core.hooksPath`` is a local
setting, so every clone has to run this once.

Usage:
    python scripts/install_git_hooks.py             # install
    python scripts/install_git_hooks.py --status    # report current state
    python scripts/install_git_hooks.py --uninstall # restore .git/hooks
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / ".githooks"
HOOK_REL = ".githooks"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True
    )


def current_hooks_path() -> str:
    out = git("config", "--get", "core.hooksPath")
    return out.stdout.strip() if out.returncode == 0 else ""


def install() -> int:
    if not (ROOT / ".git").exists():
        print("install_git_hooks: not a git repository", file=sys.stderr)
        return 1

    hook = HOOKS_DIR / "pre-commit"
    if not hook.exists():
        print(f"install_git_hooks: missing {hook.relative_to(ROOT)}", file=sys.stderr)
        return 1

    # Preserve any hook the user already had in .git/hooks.
    legacy = ROOT / ".git" / "hooks" / "pre-commit"
    if legacy.exists():
        backup = ROOT / ".git" / "hooks" / "pre-commit.before-guard"
        if backup.exists():
            print(f"install_git_hooks: {backup.name} already exists, leaving it alone")
        else:
            legacy.rename(backup)
            print(f"install_git_hooks: existing hook backed up to {backup.name}")

    try:
        os.chmod(hook, 0o755)
    except OSError:
        pass  # Windows ignores the execute bit; the shebang is enough.

    out = git("config", "core.hooksPath", HOOK_REL)
    if out.returncode != 0:
        print(f"install_git_hooks: git config failed: {out.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"install_git_hooks: core.hooksPath -> {HOOK_REL}")
    print("install_git_hooks: pre-commit credential guard is active")
    print("Verify with:  python scripts/precommit_guard.py --all")
    return 0


def uninstall() -> int:
    out = git("config", "--unset", "core.hooksPath")
    if out.returncode != 0 and "no such section" not in (out.stderr or "").lower():
        print(f"install_git_hooks: {out.stderr.strip()}", file=sys.stderr)
        return 1
    print("install_git_hooks: core.hooksPath unset, .git/hooks is active again")
    return 0


def status() -> int:
    path = current_hooks_path()
    hook = HOOKS_DIR / "pre-commit"
    print(f"guard script : {hook.relative_to(ROOT).as_posix()} exists={hook.exists()}")
    print(f"core.hooksPath: {path or '(unset - .git/hooks)'}")
    if path == HOOK_REL and hook.exists():
        print("state        : ACTIVE")
        return 0
    print("state        : INACTIVE (run: python scripts/install_git_hooks.py)")
    return 1


def main(argv: list[str]) -> int:
    if "--uninstall" in argv:
        return uninstall()
    if "--status" in argv:
        return status()
    return install()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
