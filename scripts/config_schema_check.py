"""Verify the shared config ownership manifest matches both runtimes."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _python_ownership() -> dict[str, str]:
    from sms_tool.config import SHARD_FILES, SHARD_OWNERSHIP

    return {key: owner for key, owner in SHARD_OWNERSHIP.items() if owner in SHARD_FILES}


def _csharp_ownership() -> dict[str, str]:
    text = (ROOT / "SmsWorkbench" / "ConfigStore.cs").read_text(encoding="utf-8")
    block = text.split("private static readonly Dictionary<string, string> ShardOwnership", 1)[1]
    block = block.split("};", 1)[0]
    pairs = re.findall(r'\["([^"\\]+)"\]\s*=\s*(ProxyShard|RuntimeShard|PaymentShard)', block)
    return {key: {"ProxyShard": "proxy", "RuntimeShard": "runtime", "PaymentShard": "payment"}[value] for key, value in pairs}


def main() -> int:
    manifest = json.loads((ROOT / "config_schema.json").read_text(encoding="utf-8"))
    expected = manifest["ownership"]
    actual_py = _python_ownership()
    actual_cs = _csharp_ownership()
    failures = []
    if actual_py != expected:
        failures.append(f"Python ownership differs: {sorted(set(actual_py.items()) ^ set(expected.items()))}")
    if actual_cs != expected:
        failures.append(f"C# ownership differs: {sorted(set(actual_cs.items()) ^ set(expected.items()))}")
    if failures:
        print("Config schema check failed")
        print("\n".join(failures))
        return 1
    print("Config schema check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
