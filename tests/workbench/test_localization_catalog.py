"""The EN/CN catalog must cover every translatable string the UI asks for.

The desktop uses the Chinese source string as the translation key, so a missing
English entry fails *silently* (the UI falls back to Chinese).  This scan makes
that loud: every ``{loc:Tr ...}`` in XAML and every ``Loc.T``/``Loc.F`` literal in
C# must exist in ``SmsWorkbench/Localization/StringsEn.cs``, and no XAML text
attribute may keep a hard-coded Chinese value that would never switch.

The Python backend is deliberately out of scope: its CLI output and log lines are
Chinese by contract (see ``docs/architecture.md``), so only WPF-owned strings are
checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = ROOT / "SmsWorkbench"
CATALOG = WORKBENCH / "Localization" / "StringsEn.cs"

#: One window-chrome tooltip lives in a ControlTemplate where WPF's markup
#: compiler rejects a binding on ``ToolTip`` (MC1000), so it keeps its Chinese
#: literal.  Tracked explicitly rather than by a broad allowlist.
KNOWN_LITERAL_TOOLTIPS = {
    ("Themes/Controls.xaml", "关闭"),
}

_CATALOG_ENTRY = re.compile(r'\["((?:[^"\\]|\\.)*)"\]\s*=\s*"((?:[^"\\]|\\.)*)"')
_XAML_KEY = re.compile(r"\{loc:Tr\s+(?:Key='([^']*)'|'([^']*)')\}")
_CS_KEY = re.compile(r'\bLoc\.(?:T|F)\("((?:[^"\\]|\\.)*)"')
_XAML_HAN_TEXT = re.compile(
    r'(Text|Header|Content|ToolTip|Title|AutomationProperties\.Name)="[^"]*[\u4e00-\u9fff][^"]*"'
)
_SYS_STRING_HAN = re.compile(r"<sys:String>[^<]*[\u4e00-\u9fff][^<]*</sys:String>")


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _catalog_keys() -> set[str]:
    return {_unescape(match.group(1)) for match in _CATALOG_ENTRY.finditer(CATALOG.read_text(encoding="utf-8"))}


def _xaml_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for path in sorted(WORKBENCH.rglob("*.xaml")):
        if any(part in {"obj", "bin"} for part in path.parts):
            continue
        for match in _XAML_KEY.finditer(path.read_text(encoding="utf-8")):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            keys.setdefault(value, set()).add(path.relative_to(WORKBENCH).as_posix())
    return keys


def _csharp_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for path in sorted(WORKBENCH.rglob("*.cs")):
        if any(part in {"obj", "bin"} for part in path.parts):
            continue
        for match in _CS_KEY.finditer(path.read_text(encoding="utf-8")):
            keys.setdefault(_unescape(match.group(1)), set()).add(path.relative_to(WORKBENCH).as_posix())
    return keys


def test_the_catalog_is_not_empty_and_has_no_duplicate_keys():
    text = CATALOG.read_text(encoding="utf-8")
    entries = _CATALOG_ENTRY.findall(text)
    assert entries, "the English catalog is empty"
    keys = [_unescape(key) for key, _ in entries]
    assert len(keys) == len(set(keys)), "duplicate keys in StringsEn.Map"
    assert all(value.strip() for _, value in entries), "a catalog entry has an empty translation"


def test_every_xaml_key_has_an_english_translation():
    catalog = _catalog_keys()
    missing = {key: where for key, where in _xaml_keys().items() if key not in catalog}
    assert not missing, f"XAML keys without an English translation: {missing}"


def test_every_csharp_key_has_an_english_translation():
    catalog = _catalog_keys()
    missing = {key: where for key, where in _csharp_keys().items() if key not in catalog}
    assert not missing, f"C# keys without an English translation: {missing}"


def test_no_xaml_text_attribute_keeps_a_hard_coded_chinese_value():
    offenders: list[tuple[str, str]] = []
    for path in sorted(WORKBENCH.rglob("*.xaml")):
        if any(part in {"obj", "bin"} for part in path.parts):
            continue
        relative = path.relative_to(WORKBENCH).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in _XAML_HAN_TEXT.finditer(text):
            value = match.group(0).split('="', 1)[1][:-1]
            # A localized value carries its Chinese key inside a markup
            # extension; only a *literal* Chinese value is a regression.
            if value.lstrip().startswith("{"):
                continue
            if (relative, value) not in KNOWN_LITERAL_TOOLTIPS:
                offenders.append((relative, match.group(0)[:120]))
        for match in _SYS_STRING_HAN.finditer(text):
            offenders.append((relative, match.group(0)[:120]))
    assert not offenders, f"hard-coded Chinese left in XAML: {offenders}"


def test_the_only_known_literal_is_still_the_known_one():
    """A stale allowlist entry would hide a real regression."""
    assert KNOWN_LITERAL_TOOLTIPS == {("Themes/Controls.xaml", "关闭")}
    controls = (WORKBENCH / "Themes" / "Controls.xaml").read_text(encoding="utf-8")
    assert 'ToolTip="关闭"' in controls
