import json
import os
import threading
import time
import uuid

from .sanitizer import sanitize


IPC_PREFIX = "@@SMSWORKBENCH_V2@@"
EVENT_PREFIX = IPC_PREFIX
EVENT_ENV = "SMSWORKBENCH_EVENTS"
_sequence_lock = threading.Lock()
_sequences: dict[str, int] = {}


def normalize_stage_event(payload):
    """Return the stable cross-domain progress-event contract."""
    source = dict(payload or {})
    status = str(source.get("status") or source.get("state") or "running").strip().lower()
    account_terminal = bool(source.get("account_terminal"))
    batch_terminal = bool(source.get("batch_terminal"))
    return {
        "domain": str(source.get("domain") or "workflow"),
        "run_id": str(source.get("run_id") or source.get("operation_id") or ""),
        "batch_id": str(source.get("batch_id") or ""),
        "account_ref": str(source.get("account_ref") or ""),
        "method": str(source.get("method") or ""),
        "operation": str(source.get("operation") or ""),
        "stage": str(source.get("stage") or "unknown"),
        "status": status,
        "detail": str(source.get("detail") or source.get("message") or ""),
        "attempt": int(source.get("attempt") or 0),
        "max_attempts": int(source.get("max_attempts") or 0),
        "country": str(source.get("country") or ""),
        "duration_ms": int(source.get("duration_ms") or 0),
        "last_failed_stage": str(source.get("last_failed_stage") or ""),
        "account_terminal": account_terminal,
        "batch_terminal": batch_terminal,
        "total": int(source.get("total") or 0),
        **{key: value for key, value in source.items() if key not in {
            "domain", "run_id", "operation_id", "batch_id", "account_ref", "method",
            "operation", "stage", "status", "state", "detail", "message", "attempt",
            "max_attempts", "country", "duration_ms", "last_failed_stage",
            "account_terminal", "batch_terminal", "total",
        }},
    }


def _envelope(message_type, payload):
    body = dict(payload or {})
    run_id = str(body.get("run_id") or body.get("operation_id") or uuid.uuid4().hex)
    with _sequence_lock:
        sequence = _sequences.get(run_id, 0) + 1
        _sequences[run_id] = sequence
    return {
        "schema": "smsworkbench.ipc.v2",
        "version": 2,
        "type": message_type,
        "run_id": run_id,
        "sequence": sequence,
        "timestamp_ms": int(time.time() * 1000),
        "terminal": message_type == "result" or str(body.get("status") or body.get("state") or "").lower() in {"completed", "success", "failed", "cancelled", "error"},
        "payload": sanitize(body),
    }


def emit_result(payload, *, enabled=False):
    """Emit one versioned, single-line desktop result or normal CLI JSON."""
    payload = sanitize(payload)
    if enabled:
        envelope = _envelope("result", payload)
        print(IPC_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def desktop_events_enabled():
    return os.environ.get(EVENT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def emit_event(payload, *, enabled=None):
    """Emit one sanitized, line-delimited progress event for the WPF client."""
    active = desktop_events_enabled() if enabled is None else bool(enabled)
    if not active:
        return False
    envelope = _envelope("event", normalize_stage_event(payload))
    print(IPC_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)
    return True
