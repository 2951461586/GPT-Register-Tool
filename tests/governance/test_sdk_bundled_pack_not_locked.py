"""SDK-bundled packs must not be recorded in a NuGet lock file.

``Microsoft.NET.ILLink.Tasks`` is not a package this repo chooses: the .NET SDK
supplies it (single-file publish links the installer's embedded payload) and pins
it per feature band. Recording it in ``packages.lock.json`` duplicates a fact the
SDK already owns, one band behind, and CI restores in locked mode
(``Directory.Build.props`` sets ``RestoreLockedMode`` when ``CI`` is true), so the
copy is not advisory — it is a hard failure:

* 2026-11-20, the first tag-driven release: the runner resolved a newer feature
  band (``global.json`` has ``rollForward: latestFeature``) that ships ILLink
  10.0.15, the lock recorded 10.0.8, and restore died with

      NU1004 ... version has changed from [10.0.8, ) to [10.0.15, )

* the same lock drifted the other way for years on the maintainer's machine
  (``.dotnet`` 10.0.300 downgrades the entry), which is why "revert
  ``scripts/installer/packages.lock.json`` before publishing" was a ritual.

The installer project therefore opts out of lock files entirely. If a real
package dependency is ever added to it, lock that dependency — not the SDK's
implicit one.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Packs the SDK resolves implicitly; their version follows the feature band.
SDK_BUNDLED_PACKS = ("Microsoft.NET.ILLink.Tasks",)

SKIPPED_DIRS = {"bin", "obj", "dist", "node_modules", ".git", ".venv", ".dotnet", ".pytest_cache"}


def lock_files() -> list[Path]:
    found: list[Path] = []
    for path in PROJECT_ROOT.rglob("packages.lock.json"):
        if SKIPPED_DIRS.intersection(path.relative_to(PROJECT_ROOT).parts):
            continue
        found.append(path)
    return found


def test_every_project_lock_file_is_discovered():
    """防假绿：路径规则写错会让下面的比较退化成空集通过。"""
    names = {path.relative_to(PROJECT_ROOT).as_posix() for path in lock_files()}
    assert names, "no packages.lock.json found — the discovery rule broke"
    assert "SmsWorkbench/packages.lock.json" in names, names


def test_no_lock_file_contains_an_sdk_bundled_pack():
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in lock_files()
        for pack in SDK_BUNDLED_PACKS
        if pack in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "these lock files record an SDK-bundled pack, which follows the resolved "
        "feature band and fails locked-mode restore on the runner:\n  "
        + "\n  ".join(offenders)
        + "\nDrop the lock file (scripts/installer/GPTRegisterToolSetup.csproj opts out)."
    )
