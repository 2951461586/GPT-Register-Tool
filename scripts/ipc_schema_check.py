"""Verify the resident desktop-read protocol manifest against both runtimes.

What this actually enforces
---------------------------
1. ``protocol_version`` (legacy ``version`` fallback) -- Python, C# and the
   resident desktop-read manifest must agree.
2. ``ops``      -- Python's ``SUPPORTED_OPS`` must equal the manifest (pre-existing).
3. ``error_codes`` (new) -- the backend's ``CODE_*`` constants must equal the
   manifest, **and** every one of them must be parseable by C#. The rule is
   deliberately *not* "all three sets are equal": C# legitimately carries
   client-local codes (``timeout``, ``cancelled``, ``protocol_mismatch``,
   ``channel_unavailable``) that never arrive from the backend. The contract is
   one-directional -- anything the backend puts on the wire, the client must
   understand.
4. ``event_envelope_version``/``event_envelope_schema`` and
   ``event_envelope_keys`` -- the v2 event envelope
   (``smsworkbench.ipc.v2``) that ``desktop_ipc._envelope`` builds must match
   the manifest, and C# must not read an envelope key that is not in it.

Before (3) and (4) existed, ``error_codes`` was a decorative field: it looked
like a validated contract but nothing read it, and the v2 envelope had no
declaration at all.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS_CS = ROOT / "SmsWorkbench.Contracts" / "BackendProgressEvents.cs"


def _py_error_codes(py_source: str) -> list[str]:
    """``CODE_*`` module constants in ``desktop_serve.py``."""
    return sorted(set(re.findall(r'^CODE_[A-Z_]+\s*=\s*"([a-z_]+)"', py_source, re.M)))


def _cs_parseable_codes(cs_source: str) -> list[str]:
    """Wire strings the C# ``Parse`` switch understands (wire -> enum)."""
    return sorted(set(re.findall(r'^\s*"([a-z_]+)"\s*=>\s*DesktopReadErrorCode\.', cs_source, re.M)))


def _py_envelope_keys(desktop_ipc_source: str) -> list[str]:
    """Keys of the dict literal that ``_envelope`` returns."""
    tree = ast.parse(desktop_ipc_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_envelope":
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                    return sorted(
                        key.value
                        for key in child.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
    return []


def _cs_envelope_keys_read(cs_source: str) -> list[str]:
    """Envelope keys C# reads off the frame root.

    Two call shapes occur: ``root.GetProperty("x")`` / ``root.TryGetProperty("x")``
    and the JSON helpers ``Text(root, "x")`` / ``Number(root, "x")``. Matching
    only the first silently misses ``type`` and ``sequence``, which is exactly
    the drift this check exists to catch.
    """
    direct = re.findall(r'root\.(?:GetProperty|TryGetProperty)\(\s*"([A-Za-z_]+)"', cs_source)
    helpers = re.findall(r'\b\w+\(\s*root\s*,\s*"([A-Za-z_]+)"', cs_source)
    return sorted(set(direct) | set(helpers))


def check_manifest(manifest, py, py_ipc, cs, cs_events="") -> list[str]:
    """Compare the manifest against both runtimes. Returns failure messages.

    Split out of :func:`main` so the contract can be exercised with synthetic
    sources -- otherwise every negative case would need a real file on disk.
    """
    failures = []
    py_version = int(re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", py).group(1))
    cs_version = int(re.search(r"public const int Version =\s*(\d+)", cs).group(1))
    protocol_version = int(manifest.get("protocol_version", manifest.get("version", 0)))
    if py_version != protocol_version or cs_version != protocol_version:
        failures.append(f"protocol version mismatch: manifest={protocol_version} python={py_version} csharp={cs_version}")
    if "event_envelope_version" in manifest or "event_envelope_schema" in manifest:
        envelope_version = int(manifest.get("event_envelope_version", 0))
        envelope_schema = str(manifest.get("event_envelope_schema") or "")
        py_envelope = py_ipc
        version_match = re.search(r'"version"\s*:\s*(\d+)', py_envelope)
        schema_match = re.search(r'"schema"\s*:\s*"([^"]+)"', py_envelope)
        if not version_match or int(version_match.group(1)) != envelope_version:
            failures.append(f"event envelope version mismatch: manifest={envelope_version} python={version_match.group(1) if version_match else 'missing'}")
        if not schema_match or schema_match.group(1) != envelope_schema:
            failures.append(f"event envelope schema mismatch: manifest={envelope_schema} python={schema_match.group(1) if schema_match else 'missing'}")
    tree = ast.parse(py)
    supported = next(node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SUPPORTED_OPS" for t in node.targets))
    py_ops = [elt.value for elt in supported.value.elts if isinstance(elt, ast.Constant)]
    if py_ops != manifest["ops"]:
        failures.append(f"Python ops differ: {py_ops}")

    # --- error codes: manifest == backend, and C# must understand each one ---
    manifest_codes = sorted(manifest.get("error_codes") or [])
    py_codes = _py_error_codes(py)
    if py_codes != manifest_codes:
        failures.append(
            f"error codes differ: manifest={manifest_codes} python={py_codes}"
        )
    cs_known = _cs_parseable_codes(cs)
    unparseable = sorted(set(py_codes) - set(cs_known))
    if unparseable:
        failures.append(
            "error codes the C# client cannot parse: " + ", ".join(unparseable)
        )

    # --- v2 event envelope ---
    declared = sorted(manifest.get("event_envelope_keys") or [])
    built = _py_envelope_keys(py_ipc)
    if built != declared:
        failures.append(f"event envelope keys differ: manifest={declared} python={built}")
    if cs_events:
        read = _cs_envelope_keys_read(cs_events)
        undeclared = sorted(set(read) - set(declared))
        if undeclared:
            failures.append(
                "C# reads event envelope keys the backend does not emit: "
                + ", ".join(undeclared)
            )

    return failures


def main() -> int:
    manifest = json.loads((ROOT / "ipc_schema.json").read_text(encoding="utf-8"))
    py = (ROOT / "sms_tool" / "desktop_serve.py").read_text(encoding="utf-8")
    py_ipc = (ROOT / "sms_tool" / "desktop_ipc.py").read_text(encoding="utf-8")
    cs = (ROOT / "SmsWorkbench.Contracts" / "DesktopReadProtocol.cs").read_text(encoding="utf-8")
    cs_events = EVENTS_CS.read_text(encoding="utf-8") if EVENTS_CS.exists() else ""
    failures = check_manifest(manifest, py, py_ipc, cs, cs_events)
    if failures:
        print("IPC schema check failed")
        print("\n".join(failures))
        return 1
    print("IPC schema check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
