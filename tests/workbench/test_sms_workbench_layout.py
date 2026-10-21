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

import re
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
        if path.is_file() and path.suffix in {".cs", ".xaml"} and "bin" not in path.parts and "obj" not in path.parts
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


#: A literal resource reference in XAML. ``ContentSource="Icon"`` (Wpf.Ui's
#: TitleBar) ends in the same word, so the lookbehind is load-bearing; a binding
#: starts with ``{`` and never matches.
_XAML_SOURCE = re.compile(r'(?<![\w.])Source="([^"{][^"]*)"')


def test_xaml_resource_references_resolve_from_the_xaml_files_own_folder():
    """WPF resolves a relative ``Source=`` against the XAML file, not the project root.

    Measured 2026-10-22: 695354d moved every window into ``Views/`` as a "pure
    ``git mv``", so ``Source="Assets/app-icon.ico"`` in ``MainWindow.xaml``
    started asking for ``Views/Assets/app-icon.ico``. WPF answers a missing pack
    resource with a **null** ``ImageSource`` -- no exception, no warning, no log
    line -- so the sidebar brand logo went blank in the first build made after
    the move, and neither the build nor any gate noticed. A probe against the
    real assembly pinned the mechanism: relative -> ``NULL``, leading slash ->
    ``BitmapFrameDecode 128x128``.

    The fix is the leading slash (``/Assets/...``), which anchors the reference
    at the assembly root, or a full
    ``pack://application:,,,/SmsWorkbench;component/...`` URI. Both are accepted
    here; a relative reference is accepted only when the file it names really is
    next to the XAML, which is what WPF itself would do.
    """
    failures: list[str] = []
    for xaml in PROJECT.rglob("*.xaml"):
        if "bin" in xaml.parts or "obj" in xaml.parts:
            continue
        for value in _XAML_SOURCE.findall(xaml.read_text(encoding="utf-8")):
            if value.startswith(("pack://", "http://", "https://")):
                continue
            target = (PROJECT / value.lstrip("/")) if value.startswith("/") else (xaml.parent / value)
            if target.is_file():
                continue
            failures.append(
                f'{xaml.relative_to(PROJECT)}: Source="{value}" resolves to '
                f"{target.relative_to(PROJECT)}, which does not exist "
                f'(prefix it with "/" to anchor it at the project root)'
            )
    assert not failures, "\n".join(failures)
