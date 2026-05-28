"""Small grpcurl boundary used by optional local provider services."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .gen_pp_link import DEFAULT_CONFIG_PATH


def call_grpcurl(
    method: str,
    body: dict[str, Any],
    *,
    addr: str,
    service: str,
    grpcurl: str = "grpcurl",
    proto_path: str = "",
    proto_import_path: str = "",
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    if not str(addr or "").strip():
        return {"success": False, "errorMessage": "grpc addr is required"}
    command = [
        str(grpcurl or "grpcurl"),
        "-plaintext",
        "-max-time",
        str(int(timeout_seconds or 600)),
    ]
    resolved_proto = _resolve_project_path(proto_path)
    if resolved_proto:
        resolved_import = _resolve_project_path(proto_import_path) or str(Path(resolved_proto).parent)
        command.extend(["-import-path", resolved_import, "-proto", str(Path(resolved_proto).name)])
    command.extend([
        "-d",
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        str(addr).strip(),
        f"{service}/{method}",
    ])
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=int(timeout_seconds or 600) + 5,
        )
    except FileNotFoundError:
        return {"success": False, "errorMessage": f"grpcurl not found: {grpcurl}"}
    except Exception as exc:
        return {"success": False, "errorMessage": str(exc)}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"success": False, "errorMessage": stderr or stdout or f"grpcurl exited {proc.returncode}"}
    if not stdout:
        return {"success": True}
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            return parsed
        return {"success": True, "data": parsed}
    except Exception:
        return {"success": False, "errorMessage": stdout[:500]}


def _resolve_project_path(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = Path(DEFAULT_CONFIG_PATH).resolve().parent / path
    return str(path)
