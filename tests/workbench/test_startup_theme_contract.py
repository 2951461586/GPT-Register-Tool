"""Guard the startup theme path: two dictionaries, one log line.

Two independent defects used to live here and both were invisible to the suite.

**1. Wpf.Ui's theme dictionary was never merged.** ``ApplicationThemeManager.Apply``
swaps only the merged dictionary whose ``Source`` carries both ``wpf.ui;`` and
``theme`` (``Wpf.Ui.Appearance.ResourceDictionaryManager.UpdateDictionary``), and
Wpf.Ui 4.3.0's ``Wpf.Ui.xaml`` merges the control templates plus the static colour
tables but *not* ``Resources/Theme/{Light,Dark}.xaml``. ``App.xaml`` merged only
``Wpf.Ui.xaml``, so ``Apply`` returned before caching anything: ``GetAppTheme()``
answered ``Unknown`` forever, ``Changed`` never fired, and every WPF-UI template
key this app does not override resolved to nothing.

**2. Two call sites logged the startup theme.** The ``MainWindow`` constructor
applied a *guess* (``Unknown`` is not ``Dark``, so ``Light``) and logged it, then
``App.OnStartup`` applied the value persisted in ``runtime/desktop_theme.txt`` and
logged again -- every start printed ``Light`` and then ``Dark``.

The C# side is covered behaviourally by ``DesktopWindowSmokeTests`` (the dictionary
really swaps; the constructor really stays silent). This module pins the two source
invariants those tests cannot see: the merge and its position, and the fact that
exactly one call site is allowed to write the log line.

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = PROJECT_ROOT / "SmsWorkbench"

APP_XAML = WORKBENCH / "App.xaml"
APP_CODE = WORKBENCH / "App.xaml.cs"
MAIN_WINDOW_CODE = WORKBENCH / "Views" / "MainWindow.xaml.cs"
THEME_CODE = WORKBENCH / "Views" / "MainWindow.Theme.cs"

MERGED_SOURCE = re.compile(r'<ResourceDictionary\s+Source="([^"]+)"')
CALL = re.compile(r"ApplyStartupTheme\s*\(")

#: The pair Wpf.Ui supplies and swaps itself, and the pair this app swaps itself.
WPFUI_THEME_FILE = "Theme/Light.xaml"
WPFUI_TEMPLATES_FILE = "Wpf.Ui.xaml"
APP_PALETTE_FILE = "Themes/Colors.Light.xaml"
APP_PALETTE_COUNTERPART = "Themes/Colors.Dark.xaml"


def _merged_sources():
    return MERGED_SOURCE.findall(APP_XAML.read_text(encoding="utf-8-sig"))


def _relative(path):
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def _call_sites():
    """Map ``file -> [call text]`` for every ``ApplyStartupTheme`` reference in the app."""
    sites = {}
    for path in sorted(WORKBENCH.rglob("*.cs")):
        if "obj" in path.parts or "bin" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig")
        for match in CALL.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            line = text[start : end if end >= 0 else len(text)]
            sites.setdefault(_relative(path), []).append(line.strip())
    return sites


class AppResourceMergeTests(unittest.TestCase):
    """``App.xaml`` must merge both theme pairs, in the order resolution needs."""

    def test_wpfui_theme_dictionary_is_merged(self):
        """Without it ``ApplicationThemeManager.Apply`` is a silent no-op."""
        swappable = [
            source for source in _merged_sources() if "wpf.ui;" in source.lower() and "theme" in source.lower()
        ]
        self.assertEqual(
            len(swappable),
            1,
            "App.xaml must merge exactly one WPF-UI source that matches the "
            '"wpf.ui;" + "theme" search UpdateDictionary performs; found: %s' % (swappable,),
        )
        self.assertTrue(
            swappable[0].lower().endswith(WPFUI_THEME_FILE.lower()),
            "the merged WPF-UI theme dictionary must be %s, not %s" % (WPFUI_THEME_FILE, swappable[0]),
        )

    def test_theme_dictionary_sits_between_the_templates_and_the_app_palette(self):
        """Position is load-bearing in both directions.

        After ``Wpf.Ui.xaml``: the theme dictionary resolves its
        ``SystemAccentColorPrimary`` through the static tables merged there.
        Before ``Themes/Colors.Light.xaml``: the app palette must keep winning the
        keys both declare (``ComboBoxDropDownBackground`` and its siblings are the
        ones that actually collide), or dark mode would inherit WPF-UI's light
        ComboBox popup.
        """
        sources = _merged_sources()

        def index_of(fragment):
            for position, source in enumerate(sources):
                if source.lower().endswith(fragment.lower()):
                    return position
            self.fail("App.xaml no longer merges %s: %s" % (fragment, sources))

        templates = index_of(WPFUI_TEMPLATES_FILE)
        theme = index_of(WPFUI_THEME_FILE)
        palette = index_of(APP_PALETTE_FILE)
        self.assertLess(
            templates,
            theme,
            "%s must be merged before %s" % (WPFUI_TEMPLATES_FILE, WPFUI_THEME_FILE),
        )
        self.assertLess(
            theme,
            palette,
            "%s must be merged before %s so the app palette keeps the shared keys"
            % (WPFUI_THEME_FILE, APP_PALETTE_FILE),
        )

    def test_exactly_one_app_palette_is_merged(self):
        """``SwapColorScheme`` finds the palette by name and needs a single candidate.

        Merging both ``Colors.Light.xaml`` and ``Colors.Dark.xaml`` would leave one
        of them in the list forever, and a key read from the stale one wins or loses
        by merge order rather than by theme.
        """
        palettes = [
            source
            for source in _merged_sources()
            if source.lower().endswith(APP_PALETTE_FILE.lower())
            or source.lower().endswith(APP_PALETTE_COUNTERPART.lower())
        ]
        self.assertEqual(
            1,
            len(palettes),
            "App.xaml must merge exactly one app palette dictionary; found: %s" % (palettes,),
        )


class StartupThemeLogTests(unittest.TestCase):
    """The startup theme line is written from exactly one call site."""

    def test_only_app_onstartup_logs_the_startup_theme(self):
        sites = _call_sites()
        self.assertEqual(
            sorted(sites),
            sorted([_relative(APP_CODE), _relative(MAIN_WINDOW_CODE), _relative(THEME_CODE)]),
            "ApplyStartupTheme is called from an unexpected file. Keep the logging call "
            "site unique (App.OnStartup) and pass log: false everywhere else; call sites: %s" % (sites,),
        )
        self.assertEqual(1, len(sites[_relative(APP_CODE)]), sites)
        self.assertEqual(1, len(sites[_relative(MAIN_WINDOW_CODE)]), sites)
        self.assertEqual(1, len(sites[_relative(THEME_CODE)]), sites)

    def test_the_constructor_call_is_silent(self):
        """The constructor seeds a palette; it may not print 'theme applied'."""
        (call,) = _call_sites()[_relative(MAIN_WINDOW_CODE)]
        self.assertIn(
            "log: false",
            call,
            "MainWindow's constructor must call ApplyStartupTheme(..., log: false); "
            "logging there printed the guess before App.OnStartup applied the truth: %s" % call,
        )

    def test_the_authority_call_logs(self):
        """``App.OnStartup`` owns the persisted value, so it owns the only line."""
        (call,) = _call_sites()[_relative(APP_CODE)]
        self.assertNotIn(
            "log: false",
            call,
            "App.OnStartup is the single authoritative application and must log it: %s" % call,
        )

    def test_the_declaration_defaults_to_logging(self):
        """``log`` keeps defaulting to true so a plain call is still observable."""
        (declaration,) = _call_sites()[_relative(THEME_CODE)]
        self.assertIn("bool log = true", declaration, declaration)
        self.assertIn(
            "if (log)",
            THEME_CODE.read_text(encoding="utf-8-sig"),
            ("ApplyStartupTheme must gate the log line on its parameter"),
        )


if __name__ == "__main__":
    unittest.main()
