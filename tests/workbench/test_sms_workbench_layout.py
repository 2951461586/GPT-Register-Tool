"""Pin the role-folder layout of ``SmsWorkbench/``.

Why this exists
---------------
Until 2026-10-20 all 69 ``.cs`` files sat in the project root, next to the
``.csproj``: windows, view models, service clients and the Python-process
clients were indistinguishable without opening each file. The move into role
folders is a pure ``git mv`` -- C# namespaces and XAML ``x:Class`` do not follow
the file path -- so nothing in the compiler keeps the next file from landing at
the root again. This test does.

The root exception list is deliberately tiny and named: the application shell.
Everything else must live in one of the role folders, and a new folder needs a
decision here plus a line in ``docs/directory-map.md``.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT = PROJECT_ROOT / "SmsWorkbench"

#: The application shell: entry point, composition root, assembly attributes,
#: and the implicit-usings file. Nothing feature-specific belongs here.
ROOT_SHELL = {"App.xaml", "App.xaml.cs", "AppHost.cs", "AssemblyInfo.cs", "GlobalUsings.cs"}

ROLE_FOLDERS = {"Views", "ViewModels", "Services", "Interop", "Models", "Dialogs"}


def _project_sources() -> list[Path]:
    return [
        path
        for path in PROJECT.rglob("*")
        if path.is_file()
        and path.suffix in {".cs", ".xaml"}
        and "bin" not in path.parts
        and "obj" not in path.parts
    ]


def test_the_root_holds_only_the_application_shell():
    root_files = {path.name for path in PROJECT.iterdir() if path.is_file() and path.suffix in {".cs", ".xaml"}}
    assert root_files == ROOT_SHELL


def test_every_source_lives_in_a_declared_role_folder():
    stray = sorted(
        str(path.relative_to(PROJECT))
        for path in _project_sources()
        if path.parent != PROJECT and path.parent.name not in ROLE_FOLDERS
    )
    assert stray == [], f"place these in one of {sorted(ROLE_FOLDERS)}: {stray}"


def test_every_role_folder_exists_and_is_populated():
    for folder in sorted(ROLE_FOLDERS):
        directory = PROJECT / folder
        assert directory.is_dir(), f"missing role folder: {folder}"
        assert any(directory.glob("*.cs")), f"empty role folder: {folder}"


def test_xaml_and_its_code_behind_stay_together():
    """A window's .xaml and .xaml.cs must be siblings, or the next reader looks in two places."""
    for xaml in PROJECT.rglob("*.xaml"):
        if "bin" in xaml.parts or "obj" in xaml.parts:
            continue
        code_behind = xaml.with_suffix(".xaml.cs")
        assert code_behind.is_file(), f"{xaml.relative_to(PROJECT)} has no code-behind beside it"
