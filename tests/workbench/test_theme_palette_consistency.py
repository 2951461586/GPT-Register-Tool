"""Guard against palette drift between App.xaml and MainWindow.Theme.cs (round 6).

The same brush keys are declared twice:

* the XAML side declares ~48 ``SolidColorBrush`` resources (the first-frame
  values, resolved before any code runs). Since 2026-10-23 they live in
  ``Themes/Palette.xaml``, merged into ``App.xaml`` before ``Themes/Controls.xaml``;
  this test reads both XAML files so the split cannot hide a brush from the
  check.
* ``MainWindow.Theme.cs`` re-declares them with ``SetBrush("Key", "#Hex")`` for
  both the light and dark palettes, and ``MainWindow.xaml.cs`` calls
  ``ApplyCustomThemeColors`` from the constructor -- so the C# values win from
  the very first frame.

That makes App.xaml look authoritative while being inert for every key the theme
code also sets: editing the XAML appears to do nothing. This module does not
pick a winner (the XAML is still needed for keys the theme never touches and for
design-time rendering); it only asserts the two agree wherever they overlap, so
the next person who edits one side finds out instead of shipping a half-applied
colour change.

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_XAML = PROJECT_ROOT / "SmsWorkbench" / "App.xaml"
PALETTE_XAML = PROJECT_ROOT / "SmsWorkbench" / "Themes" / "Palette.xaml"
#: Every XAML file that may declare a brush. ``Palette.xaml`` owns them since the
#: 2026-10-23 split; ``App.xaml`` stays in the list so a brush added back to the
#: application file is still compared against the theme code.
BRUSH_XAML_FILES = (PALETTE_XAML, APP_XAML)
THEME_CS = PROJECT_ROOT / "SmsWorkbench" / "Views" / "MainWindow.Theme.cs"

XAML_BRUSH = re.compile(r'<SolidColorBrush\s+x:Key="(?P<key>[^"]+)"\s+Color="(?P<color>#[0-9A-Fa-f]{6,8})"')
CS_BRUSH = re.compile(r'SetBrush\(\s*"(?P<key>[^"]+)"\s*,\s*"(?P<color>#[0-9A-Fa-f]{6,8})"\s*\)')


def _normalise(value):
    """Drop an opaque alpha prefix so #FFFFFF and #FFFFFFFF compare equal."""
    value = value.upper()
    if len(value) == 9 and value[1:3] == "FF":
        return "#" + value[3:]
    return value


def _xaml_brushes():
    found = {}
    for path in BRUSH_XAML_FILES:
        for key, color in XAML_BRUSH.findall(path.read_text(encoding="utf-8-sig")):
            found[key] = _normalise(color)
    return found


def _csharp_brushes():
    """Collect every SetBrush call; a key set twice (light + dark) keeps both."""
    found = {}
    for key, color in CS_BRUSH.findall(THEME_CS.read_text(encoding="utf-8-sig")):
        found.setdefault(key, []).append(_normalise(color))
    return found


class PaletteDriftTests(unittest.TestCase):
    def test_both_sides_parse(self):
        for path in BRUSH_XAML_FILES:
            self.assertTrue(path.is_file(), path)
        self.assertTrue(THEME_CS.is_file())
        self.assertGreater(len(_xaml_brushes()), 20)
        self.assertGreater(len(_csharp_brushes()), 20)

    def test_overlapping_keys_have_at_least_one_matching_value(self):
        """A key the theme also sets must agree with XAML in one of the palettes.

        The theme sets each key twice (light + dark), so an exact match with the
        XAML is only expected for the active palette. What must never happen is a
        key whose XAML value appears in neither palette -- that is a colour
        somebody changed on one side only.
        """
        xaml = _xaml_brushes()
        cs = _csharp_brushes()
        shared = sorted(set(xaml) & set(cs))
        self.assertGreater(len(shared), 10, "expected a meaningful overlap to check")
        mismatched = {key: (xaml[key], cs[key]) for key in shared if xaml[key] not in cs[key]}
        self.assertEqual(
            mismatched,
            {},
            "palette drift: these keys differ between App.xaml and "
            "MainWindow.Theme.cs in BOTH palettes (xaml -> csharp values)",
        )

    def test_every_key_the_theme_sets_exists_in_xaml(self):
        """The theme can only override brushes that are actually declared.

        A SetBrush on an undeclared key silently injects a resource at runtime,
        so nothing can reference it before the first theme pass resolves.
        """
        xaml = _xaml_brushes()
        cs = _csharp_brushes()
        undeclared = sorted(set(cs) - set(xaml))
        self.assertEqual(undeclared, [], "SetBrush targets keys the XAML side never declares")

    def test_no_duplicate_brush_key(self):
        """A repeated x:Key is legal XAML but only the last one wins.

        Duplicates are checked across the two dictionaries as well: merging
        ``Palette.xaml`` and ``App.xaml`` means a key declared in both silently
        resolves to whichever dictionary wins, which is exactly the half-applied
        colour change this module exists to prevent.
        """
        keys = []
        for path in BRUSH_XAML_FILES:
            keys.extend(re.findall(r'<SolidColorBrush\s+x:Key="([^"]+)"', path.read_text(encoding="utf-8-sig")))
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        self.assertEqual(duplicates, [])


class FieldLabelStyleTests(unittest.TestCase):
    """`FieldLabel` is the one x:Key defined in two different files."""

    def test_field_label_is_declared_exactly_once_app_wide(self):
        xaml_files = sorted((PROJECT_ROOT / "SmsWorkbench").rglob("*.xaml"))
        declarations = []
        for path in xaml_files:
            if "obj" in path.parts or "bin" in path.parts:
                continue
            for match in re.finditer(r'x:Key="FieldLabel"', path.read_text(encoding="utf-8-sig")):
                declarations.append(str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"))
        self.assertLessEqual(
            len(declarations),
            1,
            "FieldLabel declared more than once: %s" % declarations,
        )


if __name__ == "__main__":
    unittest.main()
