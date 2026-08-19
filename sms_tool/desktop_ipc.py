import json
import os

from .sanitizer import sanitize


IPC_PREFIX = "@@SMSWORKBENCH_IPC_V1@@"
EVENT_PREFIX = "@@SMSWORKBENCH_EVENT_V1@@"
EVENT_ENV = "SMSWORKBENCH_EVENTS"


def emit_result(payload, *, enabled=False):
    """Emit one versioned, single-line desktop result or normal CLI JSON."""
    payload = sanitize(payload)
    if enabled:
        envelope = {"version": 1, "type": "result", "payload": payload}
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
    envelope = {"version": 1, "type": "event", "payload": sanitize(dict(payload or {}))}
    print(EVENT_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)
    return True
