"""Verify the resident desktop-read protocol manifest against both runtimes."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest = json.loads((ROOT / "ipc_schema.json").read_text(encoding="utf-8"))
    py = (ROOT / "sms_tool" / "desktop_serve.py").read_text(encoding="utf-8")
    cs = (ROOT / "SmsWorkbench.Contracts" / "DesktopReadProtocol.cs").read_text(encoding="utf-8")
    failures = []
    py_version = int(re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", py).group(1))
    cs_version = int(re.search(r"public const int Version =\s*(\d+)", cs).group(1))
    if py_version != manifest["version"] or cs_version != manifest["version"]:
        failures.append(f"protocol version mismatch: manifest={manifest['version']} python={py_version} csharp={cs_version}")
    tree = ast.parse(py)
    supported = next(node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SUPPORTED_OPS" for t in node.targets))
    py_ops = [elt.value for elt in supported.value.elts if isinstance(elt, ast.Constant)]
    if py_ops != manifest["ops"]:
        failures.append(f"Python ops differ: {py_ops}")
    if failures:
        print("IPC schema check failed")
        print("\n".join(failures))
        return 1
    print("IPC schema check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
