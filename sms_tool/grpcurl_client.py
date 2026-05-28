"""Small grpcurl boundary used by optional local provider services."""

from __future__ import annotations

import json
import importlib
import subprocess
import sys
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
        fallback = _call_python_grpc(
            method,
            body,
            addr=addr,
            service=service,
            proto_path=proto_path,
            proto_import_path=proto_import_path,
            timeout_seconds=timeout_seconds,
        )
        if fallback is not None:
            return fallback
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


def _call_python_grpc(
    method: str,
    body: dict[str, Any],
    *,
    addr: str,
    service: str,
    proto_path: str = "",
    proto_import_path: str = "",
    timeout_seconds: int = 600,
) -> dict[str, Any] | None:
    if not str(service or "").endswith("PaymentService"):
        return None
    module_dir = _payment_generated_module_dir(proto_path, proto_import_path)
    if not module_dir:
        return None
    sys_path_inserted = False
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
        sys_path_inserted = True
    try:
        grpc = importlib.import_module("grpc")
        json_format = importlib.import_module("google.protobuf.json_format")
        payment_pb2 = importlib.import_module("payment_pb2")
        payment_pb2_grpc = importlib.import_module("payment_pb2_grpc")
        request_cls = getattr(payment_pb2, f"{method}Request", None)
        if request_cls is None:
            return None
        request = request_cls()
        json_format.ParseDict(body or {}, request, ignore_unknown_fields=True)
        channel = grpc.insecure_channel(str(addr).strip())
        try:
            stub = payment_pb2_grpc.PaymentServiceStub(channel)
            rpc = getattr(stub, method)
            response = rpc(request, timeout=int(timeout_seconds or 600))
        finally:
            channel.close()
        parsed = json_format.MessageToDict(response, preserving_proto_field_name=False)
        if isinstance(parsed, dict):
            return parsed if parsed else {"success": True}
        return {"success": True, "data": parsed}
    except Exception as exc:
        code = getattr(exc, "code", lambda: None)()
        detail = getattr(exc, "details", lambda: "")()
        code_name = getattr(code, "name", "") if code is not None else ""
        message = detail or str(exc)
        if code_name:
            message = f"{code_name}: {message}"
        return {"success": False, "errorMessage": message[:500]}
    finally:
        if sys_path_inserted:
            try:
                sys.path.remove(module_dir)
            except ValueError:
                pass


def _payment_generated_module_dir(proto_path: str, proto_import_path: str) -> str:
    candidates: list[Path] = []
    resolved_proto = _resolve_project_path(proto_path)
    if resolved_proto:
        proto_file = Path(resolved_proto)
        candidates.extend([proto_file.parent, proto_file.parent.parent])
    resolved_import = _resolve_project_path(proto_import_path)
    if resolved_import:
        import_dir = Path(resolved_import)
        candidates.extend([import_dir, import_dir.parent])
    project_root = Path(DEFAULT_CONFIG_PATH).resolve().parent
    candidates.append(project_root / "services" / "gopay-flow")
    for candidate in candidates:
        if (candidate / "payment_pb2.py").exists() and (candidate / "payment_pb2_grpc.py").exists():
            return str(candidate)
    return ""
