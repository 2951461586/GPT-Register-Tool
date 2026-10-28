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

2026-10-24: ``Localization/`` joined as a role folder (the EN/CN engine,
the markup extension and the English catalog).

2026-10-23: ``Themes/`` joined as a **resource-only** folder (no C#, no
code-behind). It holds the ``ResourceDictionary`` files ``App.xaml`` merges,
split out of a 719-line ``App.xaml``. Since 2026-10-28 there are five of them --
``Tokens.xaml`` (spacing/radius/type), ``Glyphs.xaml`` (``Glyph.*`` geometries),
``Colors.Light.xaml`` / ``Colors.Dark.xaml`` (one key set, swapped at runtime by
``MainWindow.SwapColorScheme``) and ``Controls.xaml`` (styles) -- so the check
below is "every ``*.xaml`` in the folder is a ``ResourceDictionary``", not a
fixed file list. A ``ResourceDictionary`` is not a window, so the "xaml and its
code-behind stay together" rule below applies only to XAML whose root element is
a ``Window``/``UserControl``.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT = PROJECT_ROOT / "SmsWorkbench"

#: The application shell: entry point, composition root, assembly attributes,
#: and the implicit-usings file. Nothing feature-specific belongs here.
ROOT_SHELL = {"App.xaml", "App.xaml.cs", "AppHost.cs", "AssemblyInfo.cs", "GlobalUsings.cs"}

ROLE_FOLDERS = {"Views", "ViewModels", "Services", "Interop", "Models", "Dialogs", "Localization"}

#: Resource-only folders: XAML dictionaries, no C#. Kept apart from
#: ``ROLE_FOLDERS`` so the "populated by ``.cs``" assertion stays meaningful.
RESOURCE_FOLDERS = {"Themes"}

#: Root elements that carry no code-behind by construction.
CODELESS_XAML_ROOTS = ("ResourceDictionary",)


def _xaml_root_element(path: Path) -> str:
    """The document element's local name, without needing an XML parser."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<([A-Za-z_][\w.:]*)[\s>/]", text)
    return match.group(1).rsplit(":", 1)[-1] if match else ""


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
    declared = ROLE_FOLDERS | RESOURCE_FOLDERS
    stray = sorted(
        str(path.relative_to(PROJECT))
        for path in _project_sources()
        if path.parent != PROJECT and path.parent.name not in declared
    )
    assert stray == [], f"place these in one of {sorted(declared)}: {stray}"


def test_every_role_folder_exists_and_is_populated():
    for folder in sorted(ROLE_FOLDERS):
        directory = PROJECT / folder
        assert directory.is_dir(), f"missing role folder: {folder}"
        assert any(directory.glob("*.cs")), f"empty role folder: {folder}"


def test_every_resource_folder_holds_a_dictionary():
    for folder in sorted(RESOURCE_FOLDERS):
        directory = PROJECT / folder
        assert directory.is_dir(), f"missing resource folder: {folder}"
        dictionaries = [path for path in directory.glob("*.xaml") if _xaml_root_element(path) == "ResourceDictionary"]
        assert dictionaries, f"{folder}/ has no ResourceDictionary"


def test_xaml_and_its_code_behind_stay_together():
    """A window's .xaml and .xaml.cs must be siblings, or the next reader looks in two places.

    A ``ResourceDictionary`` is the exception: it has no code-behind by
    construction, so requiring one would force an empty partial class.
    """
    for xaml in PROJECT.rglob("*.xaml"):
        if "bin" in xaml.parts or "obj" in xaml.parts:
            continue
        if _xaml_root_element(xaml) in CODELESS_XAML_ROOTS:
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


#: A ``<ScrollBar>`` *element* with a numeric size is the bug this guards: a local
#: value beats the style setter, so that one surface silently keeps an old size
#: after the token changes. Measured 2026-10-28: ``SettingsWindow``'s custom
#: ScrollViewer template pinned ``Width="10"`` / ``Height="10"`` on its two bars,
#: so the settings dialog kept a 10px track (6px thumb) while the account grid,
#: the log panel and the ComboBox drop-downs rendered the tokenised 12px one.
_SCROLLBAR_ELEMENT = re.compile(r"<ScrollBar\b(?P<attrs>[^>]*)>", re.S)
_SIZE_ATTRIBUTE = re.compile(r'\b(?P<name>Width|Height|MinWidth|MinHeight)="\s*[\d.]+"')


def test_scrollbar_sizes_come_from_tokens_not_call_sites():
    """Every ScrollBar takes its size from ``Size.ScrollBar.*`` via the style.

    A template may position a scroll bar; it must not size it. Sizes live in
    ``Themes/Tokens.xaml`` so one edit reaches the account grid, the log panel and
    the settings dialog at once.
    """
    offenders = []
    for path in sorted(PROJECT.rglob("*.xaml")):
        if "obj" in path.parts or "bin" in path.parts:
            continue
        source = path.read_text(encoding="utf-8-sig")
        for element in _SCROLLBAR_ELEMENT.finditer(source):
            for attribute in _SIZE_ATTRIBUTE.finditer(element.group("attrs")):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {attribute.group('name')}")
    assert offenders == [], "a <ScrollBar> element pins its own size, which beats the style token: " + ", ".join(
        offenders
    )


def test_one_implicit_scrollbar_style_covers_the_app():
    """The app-wide ScrollBar style must be the one the tokens feed."""
    controls = (PROJECT / "Themes" / "Controls.xaml").read_text(encoding="utf-8")
    assert '<Style TargetType="{x:Type ScrollBar}" BasedOn="{StaticResource StableScrollBarStyle}"/>' in controls
    tokens = (PROJECT / "Themes" / "Tokens.xaml").read_text(encoding="utf-8")
    for key in ("Size.ScrollBar.Track", "Size.ScrollBar.Thumb", "Size.ScrollBar.ThumbMinLength"):
        assert key in tokens, key


#: The three panel-layout regressions reported on 2026-10-28. All three were
#: geometry/rendering facts that the compiler cannot see, and all three had a
#: single cause each, so the guard is a structural one rather than a screenshot.
_LOG_BOX = re.compile(r"<TextBox\b[^>]*x:Name=\"LogTextBox\"(?P<attrs>[^>]*)>", re.S)


def _xaml(relative: str) -> str:
    return (PROJECT / relative).read_text(encoding="utf-8")


def test_the_log_box_sizes_to_its_row():
    """`MinHeight` cannot cancel the implicit TextBox Height=32 — it only raises it.

    The log box was therefore a fixed 220px inside a shorter console row and its
    scroll bar was clipped at the bottom. The fix is the ``MultilineTextBox`` style,
    which re-applies Height=Auto (NaN).
    """
    console = _xaml("Views/WorkbenchConsole.xaml")
    element = _LOG_BOX.search(console)
    assert element is not None, "LogTextBox not found"
    attrs = element.group("attrs")
    assert 'Style="{StaticResource MultilineTextBox}"' in attrs, "LogTextBox must use MultilineTextBox"
    assert "MinHeight=" not in attrs, "a MinHeight here only raises the pinned Height=32"
    controls = _xaml("Themes/Controls.xaml")
    assert 'x:Key="MultilineTextBox"' in controls
    assert "<sys:Double>NaN</sys:Double>" in controls, "Height=Auto is spelled NaN in WPF"


def test_the_account_grid_hides_the_stock_scrollbar_corner():
    """That square is painted from SystemColors.ControlBrushKey, ignoring the palette."""
    grid = _xaml("Views/AccountGridView.xaml")
    assert 'x:Key="{x:Static SystemColors.ControlBrushKey}" Color="Transparent"' in grid


def test_the_sidebar_logo_is_a_sharp_png():
    """22px from an ICO with default bilinear filtering is what looked blurred."""
    sidebar = _xaml("Views/Sidebar.xaml")
    image = re.search(r"<Image\b[^>]*black-kitten\.png(?P<attrs>[^>]*)/>", sidebar, re.S)
    assert image is not None, "the brand mark must come from the large PNG, not app-icon.ico"
    attrs = image.group("attrs")
    assert 'RenderOptions.BitmapScalingMode="HighQuality"' in attrs
    assert 'Width="24"' in attrs and 'Height="24"' in attrs


def test_scrollbar_metrics_are_pinned_for_framework_templates():
    """Framework templates size their bars from SystemParameters, not from a style.

    Measured 2026-10-28: the DataGrid's vertical bar was 17px (the stock value)
    while the app's own bars are 12px, so inside the grid the themed thumb sat in a
    wider stock track and the end of the bar looked cut off. Both metrics are now
    declared at application scope.
    """
    controls = _xaml("Themes/Controls.xaml")
    assert 'x:Key="{x:Static SystemParameters.VerticalScrollBarWidthKey}"' in controls
    assert 'x:Key="{x:Static SystemParameters.HorizontalScrollBarHeightKey}"' in controls


def test_tooltips_follow_the_palette():
    """WPF paints tooltips from SystemColors.Info*, which no palette swap reaches.

    Dark mode therefore showed near-white text inherited from the control on the
    system cream box — unreadable.
    """
    controls = _xaml("Themes/Controls.xaml")
    tooltip = re.search(r'<Style TargetType="ToolTip">(?P<body>.*?)</Style>', controls, re.S)
    assert tooltip is not None, "no implicit ToolTip style"
    body = tooltip.group("body")
    assert '"{DynamicResource Surface.Card}"' in body
    assert '"{DynamicResource TextMain}"' in body
