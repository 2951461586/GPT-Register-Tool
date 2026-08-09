import json

from .sanitizer import sanitize


IPC_PREFIX = "@@SMSWORKBENCH_IPC_V1@@"


def emit_result(payload, *, enabled=False):
    """Emit one versioned, single-line desktop result or normal CLI JSON."""
    payload = sanitize(payload)
    if enabled:
        envelope = {"version": 1, "type": "result", "payload": payload}
        print(IPC_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))
