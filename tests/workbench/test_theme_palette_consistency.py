"""Guard the colour contract the two palette dictionaries share.

Before 2026-10-28 the same brush keys were declared twice: ``Themes/Palette.xaml``
for the first frame and ``MainWindow.Theme.cs`` via ~120 ``SetBrush("Key", "#Hex")``
calls for both palettes.  This module compared the two sides so an edit to one
could not ship half-applied.

Phase 0 of ``plan-2026-10-28-wpf-visual-system`` deleted ``Palette.xaml`` **and**
the ``SetBrush`` code: a theme switch now swaps one dictionary
(``Colors.Light.xaml`` <-> ``Colors.Dark.xaml``) and nothing re-declares a brush
in C#.  The old comparison therefore had nothing left to compare and failed on a
file that no longer exists -- which is what it did on HEAD until this rewrite.

What is still a real, unguarded invariant is the **pair** itself:

* the two files must declare exactly the same key set, because a key missing from
  one side silently falls back to a WPF default brush in that theme (there is no
  error, the control just goes white/black);
* the ``Surface.*`` ladder must keep the direction its own header documents --
  light alternates paper and grey, dark only ever climbs -- because that ordering
  *is* the replacement for structural borders;
* the legacy aliases (``PanelBg`` / ``Line`` / ``SidebarBg`` ...) must keep
  resolving to the ladder they were folded onto, since ``WindowThemeService`` and
  ``DesktopWindowSmokeTests`` read them by name;
* every ``{DynamicResource X}`` a view asks for must be declared somewhere, or
  the view silently renders with a default brush.

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = PROJECT_ROOT / "SmsWorkbench"
THEMES = WORKBENCH / "Themes"
LIGHT_XAML = THEMES / "Colors.Light.xaml"
DARK_XAML = THEMES / "Colors.Dark.xaml"

#: The legacy aliases, folded onto the ladder by Phase 0. Each must keep the
#: value of the rung it was folded onto; ``WindowThemeService`` reads
#: ``PanelBg``/``TextMain`` for the DWM caption and the smoke tests resolve
#: ``SettingsSelectionBg`` by name.
ALIAS_TO_RUNG = {
    "AppBg": "Surface.Page",
    "PanelBg": "Surface.Card",
    "PanelBg2": "Surface.Block",
    "SidebarBg": "Surface.Chrome",
    "LogBg": "Surface.Inset",
}

#: The ladder, outermost first. Light alternates and dark climbs, so only the
#: *direction between neighbours* is asserted (see ``test_surface_ladder_*``).
LADDER = ("Surface.Page", "Surface.Card", "Surface.Block", "Surface.Inset")

XAML_BRUSH = re.compile(r'<SolidColorBrush\s+x:Key="(?P<key>[^"]+)"\s+Color="(?P<color>#[0-9A-Fa-f]{6,8})"')
XAML_KEY = re.compile(r'x:Key="([^"]+)"')
DYNAMIC_RESOURCE = re.compile(r"\{DynamicResource\s+([A-Za-z0-9_.]+)\}")


def _normalise(value):
    """Drop an opaque alpha prefix so #FFFFFF and #FFFFFFFF compare equal."""
    value = value.upper()
    if len(value) == 9 and value[1:3] == "FF":
        return "#" + value[3:]
    return value


def _brushes(path):
    return {key: _normalise(color) for key, color in XAML_BRUSH.findall(path.read_text(encoding="utf-8-sig"))}


def _declared_keys(path):
    return XAML_KEY.findall(path.read_text(encoding="utf-8-sig"))


def _luminance(color):
    """WCAG relative luminance of ``#RRGGBB`` (alpha ignored)."""
    raw = color.lstrip("#")
    if len(raw) == 8:
        raw = raw[2:]
    channels = [int(raw[index : index + 2], 16) / 255.0 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _xaml_files():
    for path in sorted(WORKBENCH.rglob("*.xaml")):
        if "obj" in path.parts or "bin" in path.parts:
            continue
        yield path


def _relative(path):
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


class PalettePairTests(unittest.TestCase):
    """Light and dark are one contract: same keys, both sides parse."""

    def test_both_palettes_parse(self):
        for path in (LIGHT_XAML, DARK_XAML):
            self.assertTrue(path.is_file(), _relative(path))
            self.assertGreater(len(_brushes(path)), 40, _relative(path))

    def test_palettes_declare_the_same_key_set(self):
        """A key in one file only falls back to a WPF default brush in the other.

        That fallback is silent -- there is no parse error and no warning, the
        control just paints white on the dark theme. So the key sets are compared
        as sets, not as "dark covers everything light has".
        """
        light = set(_declared_keys(LIGHT_XAML))
        dark = set(_declared_keys(DARK_XAML))
        self.assertEqual(
            sorted(light - dark),
            [],
            "declared in Colors.Light.xaml but missing from Colors.Dark.xaml "
            "(falls back to a WPF default brush in dark)",
        )
        self.assertEqual(
            sorted(dark - light),
            [],
            "declared in Colors.Dark.xaml but missing from Colors.Light.xaml "
            "(falls back to a WPF default brush in light)",
        )

    def test_no_duplicate_brush_key(self):
        """A repeated x:Key is legal XAML but only the last one wins."""
        for path in (LIGHT_XAML, DARK_XAML):
            keys = _declared_keys(path)
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            self.assertEqual(duplicates, [], "%s declares a key twice" % _relative(path))


class SurfaceLadderTests(unittest.TestCase):
    """The ladder replaces structural borders, so its direction is load-bearing.

    ``Colors.Light.xaml`` states: page -> card is lighter, card -> block is
    darker, block -> inset is darker again (light alternates paper and grey).
    ``Colors.Dark.xaml`` states the opposite rule for a different reason: dark
    only ever gets lighter, because a darker step inside a lighter surface reads
    as a hole rather than a layer.
    """

    def test_light_alternates_paper_and_grey(self):
        light = _brushes(LIGHT_XAML)
        page, card, block, inset = (_luminance(light[key]) for key in LADDER)
        self.assertGreater(card, page, "light: card must be lighter than the page")
        self.assertLess(block, card, "light: a block must be darker than the card")
        self.assertLess(inset, block, "light: an inset must be darker than the block")

    def test_dark_only_climbs(self):
        dark = _brushes(DARK_XAML)
        levels = [_luminance(dark[key]) for key in LADDER]
        for index in range(1, len(levels)):
            self.assertGreater(
                levels[index],
                levels[index - 1],
                "dark: %s must be lighter than %s (dark only climbs)" % (LADDER[index], LADDER[index - 1]),
            )

    def test_chrome_sits_off_the_page_in_the_documented_direction(self):
        """Chrome is the sidebar/title bar fill: 4 tones away from the page.

        Light steps down from the page, dark steps up from it; either way it must
        not equal the page, or the chrome divider becomes the only separation and
        the sidebar merges with the content.
        """
        for path, expect_darker in ((LIGHT_XAML, True), (DARK_XAML, False)):
            brushes = _brushes(path)
            page = _luminance(brushes["Surface.Page"])
            chrome = _luminance(brushes["Surface.Chrome"])
            self.assertNotAlmostEqual(page, chrome, places=4, msg=_relative(path))
            if expect_darker:
                self.assertLess(chrome, page, "light: chrome sits below the page")
            else:
                self.assertGreater(chrome, page, "dark: chrome sits above the page")

    def test_legacy_aliases_still_resolve_to_their_rung(self):
        """Phase 0 folded the old semantic names onto the ladder, by name.

        ``WindowThemeService`` reads ``PanelBg`` + ``TextMain`` for the DWM
        caption and the desktop smoke tests resolve ``SettingsSelectionBg`` as a
        literal key, so an alias may not drift away from its rung even though the
        ladder is now the authority.
        """
        for path in (LIGHT_XAML, DARK_XAML):
            brushes = _brushes(path)
            for alias, rung in ALIAS_TO_RUNG.items():
                if alias not in brushes:
                    self.fail("%s no longer declares the legacy alias %s" % (_relative(path), alias))
                self.assertEqual(
                    brushes[alias],
                    brushes[rung],
                    "%s: %s must stay equal to %s" % (_relative(path), alias, rung),
                )

    def test_text_ladder_keeps_three_distinct_levels(self):
        """Three levels, three values -- in both themes.

        The palette this system replaced had ``TextMain == TextSub == TextMuted``,
        which collapsed titles, labels and metadata into one weight. Equality is
        the regression, so the assertion is on distinctness, not on a ratio.
        """
        for path in (LIGHT_XAML, DARK_XAML):
            brushes = _brushes(path)
            values = [brushes["TextMain"], brushes["TextSub"], brushes["TextMuted"]]
            self.assertEqual(len(set(values)), 3, "%s collapsed a text level" % _relative(path))


class DynamicResourceResolutionTests(unittest.TestCase):
    """Every ``{DynamicResource X}`` a view asks for must be declared somewhere."""

    def test_every_dynamic_resource_target_is_declared(self):
        declared = set()
        for path in _xaml_files():
            declared.update(_declared_keys(path))
        missing = {}
        for path in _xaml_files():
            for key in DYNAMIC_RESOURCE.findall(path.read_text(encoding="utf-8-sig")):
                if key not in declared:
                    missing.setdefault(key, set()).add(_relative(path))
        self.assertEqual(
            missing,
            {},
            "DynamicResource targets that no dictionary declares (renders with a WPF default brush, silently)",
        )


class FieldLabelStyleTests(unittest.TestCase):
    """`FieldLabel` is the one x:Key defined in two different files."""

    def test_field_label_is_declared_exactly_once_app_wide(self):
        declarations = []
        for path in _xaml_files():
            for _match in re.finditer(r'x:Key="FieldLabel"', path.read_text(encoding="utf-8-sig")):
                declarations.append(_relative(path))
        self.assertLessEqual(
            len(declarations),
            1,
            "FieldLabel declared more than once: %s" % declarations,
        )


if __name__ == "__main__":
    unittest.main()
