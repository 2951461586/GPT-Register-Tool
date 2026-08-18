"""Resident desktop-read server: one JSONL request/response per stdin/stdout line.

Every ``--desktop-read`` call used to pay a full Python cold start (~0.6-1s of
imports plus interpreter boot) for a few milliseconds of work. The WPF desktop
client keeps one ``python chatgpt_phone_reg.py --desktop-serve`` process alive
and sends requests here instead; long-running tasks (registration, payment
batches) still use one-shot processes.

Wire format (UTF-8, one JSON object per line, responses always flushed):

    request:  {"id": 1, "op": "accounts", "account_id": "", "email": "",
               "extra_files": []}
    response: {"id": 1, "ok": true, "payload": {...}}
              {"id": 1, "ok": false, "error": "..."}

``payload`` mirrors the corresponding ``--desktop-read`` IPC payloads exactly,
so the desktop client only swaps the transport. The ``pools`` op returns the
account index and the mailbox pool in one response, replacing two cold starts
per pool refresh. Config is re-resolved per request so Settings edits apply
without a restart.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import load_runtime_config
from .desktop_read import (
    create_account_file,
    create_mailbox_file,
    create_payment_url_file,
    read_account,
    read_accounts,
    read_mailbox_pool,
)


def _payload_for(op: str, request: dict[str, Any]) -> dict[str, Any]:
    account_id = str(request.get("account_id") or "")
    email = str(request.get("email") or "")
    extra_files = request.get("extra_files") or []
    if not isinstance(extra_files, list):
        extra_files = []
    config = load_runtime_config()

    if op == "accounts":
        return {"ok": True, "accounts": read_accounts(config)}
    if op == "mailbox-pool":
        return {"ok": True, **read_mailbox_pool(config, extra_files=tuple(extra_files))}
    if op == "pools":
        pool = read_mailbox_pool(config, extra_files=tuple(extra_files))
        return {"ok": True, "accounts": read_accounts(config), **pool}
    if op == "account":
        return {"ok": True, "account": read_account(account_id, email, config)}
    if op == "account-file":
        return dict(create_account_file(account_id, email, config))
    if op == "mailbox-file":
        return dict(create_mailbox_file(account_id, email, config))
    if op == "payment-url-file":
        return dict(create_payment_url_file(account_id, email, config))
    raise ValueError(f"unknown desktop-serve op: {op}")


def handle_request(request: Any) -> dict[str, Any]:
    """Serve one decoded request; never raises (errors become responses).

    The payload is NOT re-sanitized here: desktop_read already sanitizes
    secret-bearing nested values (session objects) and exposes only non-secret
    public columns — a whole-payload pass costs ~2.4s per 751-row response for
    zero additional coverage.
    """
    request_id = 0
    try:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        request_id = int(request.get("id") or 0)
        payload = _payload_for(str(request.get("op") or ""), request)
        return {"id": request_id, "ok": True, "payload": payload}
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
        return {
            "id": request_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def _unsanitized_stream(stream):
    """Unwrap the CLI's per-write sanitizing proxy.

    ``install_safe_stdio`` runs the redaction regexes on every write; responses
    here are assembled from already-sanitized field data, and re-scanning one
    multi-megabyte response line costs seconds per refresh.
    """
    from .diagnostics import SanitizingTextIO

    if isinstance(stream, SanitizingTextIO):
        return getattr(stream, "_wrapped", stream)
    return stream


def serve_forever(stdin=None, stdout=None) -> int:
    """Read requests until stdin closes; returns an exit code."""
    reader = stdin if stdin is not None else sys.stdin
    writer = _unsanitized_stream(stdout if stdout is not None else sys.stdout)
    while True:
        line = reader.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            request = None
        response = handle_request(request)
        writer.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        writer.flush()
