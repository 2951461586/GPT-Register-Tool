"""Every desktop settings field must point at a real ``config.example.json`` path.

Why this exists
---------------
`SettingsCatalog.cs` is the desktop's list of editable settings. Each entry names
a dotted path into the backend config (`codex_oauth.allow_passwordless_takeover`,
`email_registration.remail_base_url`, …). There is no compiler between the two
sides and no runtime error either: a path that the backend never reads is written
to `config.json`, the dialog says "saved", and nothing happens.

Measured 2026-11-20: three of the 112 fields did exactly that —
`codex_oauth.registration_timeout`,
`codex_oauth.require_registration_refresh_token`,
`codex_oauth.require_registration_phone_verification`. The last two rendered as
「注册要求RT」/「注册要求手机号」 and were pure decoration: `codex_oauth` only reads
`allow_passwordless_takeover` and `auto_phone_verification`, and none of the three
appeared in `config.example.json` — the very file an operator copies.

What this checks
----------------
For every settings field, its path (minus any `{placeholder}` segment, which the
desktop expands per provider) must exist in `config.example.json`. The example
file is the declared config surface: `scripts/config_schema_check.py` already
holds it against `config/schema.py`, so pinning the desktop against the same file
closes the third side without a second authority.

Deliberately not checked: *which* runtime reads a key. A field may legitimately
write a key that only one lane reads (`phone_reuse.{provider}.api_key`), and
`AccountMachineState`-style heuristics in a test would rot. Reachability is
covered where it belongs — by the backend's own config-key ratchet
(`scripts/config_key_ratchet.py`) for write/read symmetry.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "SmsWorkbench" / "Services" / "SettingsCatalog.cs"
EXAMPLE = ROOT / "config.example.json"

# `Text("key", Loc.T("label"), "dotted.path", ...)` — the helper name is the field
# kind, the third string literal that contains a dot is the config path. Slicing
# to the matching paren avoids the multi-line `Options(...)` overloads, which a
# single-line regex silently half-reads.
_FIELD = re.compile(r"\b(?:Text|Secret|Integer|Boolean|Options|Multiline)\s*\(")
_LITERAL = re.compile(r'"([^"\\]*)"')

#: Settings that intentionally write outside the backend config document.
#: Must stay small and justified; an entry here is a promise the value is used
#: somewhere other than `config.example.json`.
OUTSIDE_CONFIG = {
    # Desktop-owned runtime path, read by PythonPathResolver / the backend client.
    "runtime.python_path",
}


def _call_body(text: str, start: int) -> str:
    """Return the argument list of the call opening at ``start`` (paren-balanced)."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced call at offset {start}")


def _field_paths(source: str) -> list[str]:
    paths: list[str] = []
    for match in _FIELD.finditer(source):
        body = _call_body(source, match.start())
        literals = _LITERAL.findall(body)
        dotted = [value for value in literals if "." in value and " " not in value]
        if dotted:
            paths.append(dotted[0])
    return paths


def _has_path(document: object, dotted: str) -> bool:
    """True when ``dotted`` resolves; a ``{placeholder}`` matches any dict child.

    ``phone_reuse.{provider}.api_key`` is one desktop field expanded per provider
    (``smsbower`` / ``herosms`` / ``grizzly`` / ``nexsms``), and the example config
    does not give every provider every key — ``smsbower`` has no ``endpoint``
    because its host comes from ``SmsProviderCatalog.default_endpoint``. So the
    placeholder matches "at least one provider declares it", which is what the
    desktop's per-provider row needs.
    """
    segments = dotted.split(".")
    head, rest = segments[0], segments[1:]
    if "{" in head:
        if not isinstance(document, dict):
            return False
        children = [value for value in document.values() if isinstance(value, dict)]
        return any(_has_path(child, ".".join(rest)) for child in children) if rest else bool(children)
    if not isinstance(document, dict) or head not in document:
        return False
    return _has_path(document[head], ".".join(rest)) if rest else True


def test_the_extractor_reads_the_catalog() -> None:
    """Guard the parser itself: a renamed helper would silently find no fields."""
    paths = _field_paths(CATALOG.read_text(encoding="utf-8-sig"))
    assert len(paths) > 100, f"SettingsCatalog parsing broke: only {len(paths)} field(s)"
    assert "email_registration.remail.base_url" in paths


def test_every_settings_field_maps_to_a_declared_config_path() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
    paths = _field_paths(CATALOG.read_text(encoding="utf-8-sig"))

    missing = sorted(path for path in paths if path not in OUTSIDE_CONFIG and not _has_path(example, path))

    assert not missing, (
        "SettingsCatalog fields point at paths config.example.json does not declare, so the "
        "dialog writes settings nothing reads:\n  " + "\n  ".join(missing)
    )


def test_the_three_codex_oauth_decorations_stay_removed() -> None:
    """Regression pin for the measured bug this test was written for.

    Each of these rendered as a working checkbox/number box and had no reader in
    `sms_tool` (`codex_oauth` reads only the two booleans below). Re-adding one is
    a decision that must bring a reader, a schema entry and an example-config
    key in the same change.
    """
    source = CATALOG.read_text(encoding="utf-8-sig")
    # Code lines only: the removal note in the catalog names all three, and a pin
    # that a comment can satisfy is not a pin.
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    for removed in (
        "codex_oauth.registration_timeout",
        "codex_oauth.require_registration_refresh_token",
        "codex_oauth.require_registration_phone_verification",
    ):
        assert removed not in code, f"SettingsCatalog re-added a field with no reader: {removed}"
    assert "codex_oauth.allow_passwordless_takeover" in code
    assert "codex_oauth.auto_phone_verification" in code
