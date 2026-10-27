"""Pins the two shard-ownership tables (C# and Python) to each other.

``sms_tool/config/loader.py`` decides which shard owns a top-level config key, and
``SmsWorkbench/Services/ConfigStore.cs`` repeats that table so the desktop can
route a save to the same file. Both file headers say they mirror each other, and
nothing enforced it: adding a top-level key (``phone_reuse`` was the last one)
means editing two tables in two languages.

When they drift, the failure is silent and destructive: the desktop writes
``proxy.phone_reuse`` into ``runtime.json`` while the backend reads
``proxy.json``, so the setting appears to save and then never applies -- and the
next shard write from either side may drop the other's copy.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CS_SOURCE = ROOT / "SmsWorkbench" / "Services" / "ConfigStore.cs"

#: C# constant name -> shard key used by ``SHARD_FILES``/``SHARD_OWNERSHIP``.
SHARD_CONSTANTS = {
    "ProxyShard": "proxy",
    "RuntimeShard": "runtime",
    "PaymentShard": "payment",
}

_OWNERSHIP_RE = re.compile(r'\["(?P<key>[^"]+)"\]\s*=\s*(?P<constant>\w+Shard)\b')
_FILE_RE = re.compile(r'public\s+const\s+string\s+(?P<constant>\w+Shard)\s*=\s*"(?P<file>[^"]+)"')


def parse_ownership(source: str) -> dict[str, str]:
    """``{top-level key: shard}`` from the C# ``ShardOwnership`` initialiser."""
    block = source.split("ShardOwnership = new()", 1)[1].split("};", 1)[0]
    parsed: dict[str, str] = {}
    for match in _OWNERSHIP_RE.finditer(block):
        constant = match.group("constant")
        assert constant in SHARD_CONSTANTS, f"unknown shard constant in ConfigStore.cs: {constant}"
        key = match.group("key")
        assert key not in parsed, f"duplicate ownership entry in ConfigStore.cs: {key}"
        parsed[key] = SHARD_CONSTANTS[constant]
    return parsed


def parse_file_names(source: str) -> dict[str, str]:
    """``{shard: file name}`` from the C# shard-name constants."""
    return {
        SHARD_CONSTANTS[match.group("constant")]: match.group("file")
        for match in _FILE_RE.finditer(source)
        if match.group("constant") in SHARD_CONSTANTS
    }


def test_csharp_ownership_matches_the_python_table():
    from sms_tool.config.loader import SHARD_OWNERSHIP

    source = CS_SOURCE.read_text(encoding="utf-8")
    csharp = parse_ownership(source)

    assert csharp, "parsed no ownership entries from ConfigStore.cs -- the parser or the file shape changed"
    assert csharp == dict(SHARD_OWNERSHIP), (
        "ConfigStore.cs and sms_tool/config/loader.py disagree about which shard owns a key; "
        "a desktop save and a backend read would go to different files"
    )


def test_csharp_shard_file_names_match_the_python_table():
    from sms_tool.config.loader import SHARD_FILES

    csharp = parse_file_names(CS_SOURCE.read_text(encoding="utf-8"))
    assert csharp == dict(SHARD_FILES)


def test_parsers_detect_a_planted_divergence():
    # Guard the guard: both halves must actually read what they claim to read.
    synthetic = """
        public const string ProxyShard = "proxy.json";
        public const string RuntimeShard = "runtime.json";
        private static readonly Dictionary<string, string> ShardOwnership = new()
        {
            ["proxy"] = ProxyShard,
            ["phone_reuse"] = ProxyShard,
            ["registration"] = RuntimeShard,
        };
    """
    assert parse_ownership(synthetic) == {
        "proxy": "proxy",
        "phone_reuse": "proxy",
        "registration": "runtime",
    }
    assert parse_file_names(synthetic)["proxy"] == "proxy.json"
