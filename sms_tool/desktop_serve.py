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
              {"id": 1, "ok": false, "code": "backend_error", "error": "..."}

``payload`` mirrors the corresponding ``--desktop-read`` IPC payloads exactly,
so the desktop client only swaps the transport. The ``pools`` op returns the
account index and the mailbox pool in one response, replacing two cold starts
per pool refresh. Config is re-resolved per request so Settings edits apply
without a restart.

Protocol rules (mirror of ``SmsWorkbench.Contracts/DesktopReadProtocol.cs``):

* ``hello`` must be answered first; it returns ``protocol`` so a client paired
  with a backend from a different release falls back to one-shot reads instead
  of misreading payloads.
* Errors carry a machine-readable ``code`` (see ``_CODES``) so the client can
  classify a failure without string-matching a human message.
* The wire format is **additive only**: every field a client does not know is
  ignored, so raising ``PROTOCOL_VERSION`` is the only breaking change.

A watchdog thread arms a deadline around each request. A handler that blocks
forever would otherwise leave the process alive but useless for the rest of
the session — the client-side 120s timeout abandons the request, yet every
later request piles onto the same wedged process.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
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

#: Bump together with ``DesktopReadProtocol.Version`` on the C# side. Any
#: mismatch makes the client refuse this backend and use one-shot reads.
PROTOCOL_VERSION = 1

#: Ops a client may rely on. ``hello`` reports this so a version skew is
#: detectable even before an unknown op is reached.
SUPPORTED_OPS = (
    "hello",
    "ping",
    "accounts",
    "mailbox-pool",
    "pools",
    "account",
    "account-file",
    "mailbox-file",
    "payment-url-file",
)

#: How long a single request may run before the watchdog kills the process.
#: Deliberately longer than the client's 120s request timeout so the client's
#: own timeout fires first and produces a clean, attributable error; the
#: watchdog is the backstop for a handler that never returns at all.
DEFAULT_WATCHDOG_SECONDS = 150.0

#: Exit code used when the watchdog kills the process, so it is distinguishable
#: from a clean EOF (0) and an unhandled crash in operator logs.
WATCHDOG_EXIT_CODE = 3

CODE_BAD_REQUEST = "bad_request"
CODE_UNKNOWN_OP = "unknown_operation"
CODE_BACKEND_ERROR = "backend_error"
CODE_WATCHDOG_TIMEOUT = "watchdog_timeout"
CODE_INTERNAL = "internal"


class _OpError(Exception):
    """Carrier for a failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolve_watchdog_seconds(explicit: float | None) -> float:
    if explicit is not None:
        return max(0.0, float(explicit))
    raw = os.environ.get("SMS_DESKTOP_SERVE_WATCHDOG", "")
    if raw.strip():
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_WATCHDOG_SECONDS


def _hard_exit(code: int) -> None:
    """Indirection around ``os._exit`` so tests can observe the watchdog."""
    os._exit(code)


class _Watchdog:
    """Kill the process if one request runs past the deadline.

    The main loop is single threaded and the handlers do blocking IO, so an
    in-thread timeout is impossible (``signal.alarm`` is Unix-only and would
    not interrupt a blocked read anyway). The watchdog therefore escalates to
    ``os._exit`` after writing a final error response: unwinding a wedged
    thread is not achievable, but abandoning the process is safe because the
    client restarts the channel on the next read.
    """

    _POLL_SECONDS = 1.0

    def __init__(self, writer, seconds: float) -> None:
        self._writer = writer
        self._seconds = seconds
        self._lock = threading.Lock()
        self._deadline: float | None = None
        self._request_id = 0
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="desktop-serve-watchdog", daemon=True
        )
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._seconds > 0

    def arm(self, request_id: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._request_id = request_id
            self._deadline = time.monotonic() + self._seconds

    def disarm(self) -> None:
        with self._lock:
            self._deadline = None

    def stop(self) -> None:
        self.disarm()
        self._stopped.set()

    def _run(self) -> None:
        while not self._stopped.wait(self._POLL_SECONDS):
            with self._lock:
                deadline = self._deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self._expire()
            return

    def _expire(self) -> None:
        with self._lock:
            request_id = self._request_id
        response = {
            "id": request_id,
            "ok": False,
            "code": CODE_WATCHDOG_TIMEOUT,
            "error": f"request did not finish within {self._seconds:g}s; process exiting",
        }
        try:
            self._writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            self._writer.flush()
        except Exception:  # noqa: BLE001 - the pipe may already be gone
            pass
        # Intentionally not a graceful exit: the main thread is wedged and
        # cannot be unwound, and atexit/GC hooks would run on a broken state.
        _hard_exit(WATCHDOG_EXIT_CODE)


def _payload_for(op: str, request: dict[str, Any]) -> dict[str, Any]:
    # Cheap ops first: they must not require a resolvable config, or a broken
    # config would make the handshake fail and hide the real diagnosis.
    if op == "hello":
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "ops": list(SUPPORTED_OPS),
        }
    if op == "ping":
        return {"ok": True, "pong": True}

    account_id = str(request.get("account_id") or "")
    email = str(request.get("email") or "")
    extra_files = request.get("extra_files") or []
    if not isinstance(extra_files, list):
        extra_files = []
    config = load_runtime_config()

    if op == "accounts":
        return {"ok": True, "accounts": read_accounts(config, include_session=False)}
    if op == "mailbox-pool":
        return {"ok": True, **read_mailbox_pool(config, extra_files=tuple(extra_files))}
    if op == "pools":
        pool = read_mailbox_pool(config, extra_files=tuple(extra_files))
        return {"ok": True, "accounts": read_accounts(config, include_session=False), **pool}
    if op == "account":
        return {"ok": True, "account": read_account(account_id, email, config)}
    if op == "account-file":
        return dict(create_account_file(account_id, email, config))
    if op == "mailbox-file":
        return dict(create_mailbox_file(account_id, email, config))
    if op == "payment-url-file":
        return dict(create_payment_url_file(account_id, email, config))
    raise _OpError(CODE_UNKNOWN_OP, f"unknown desktop-serve op: {op}")


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
            raise _OpError(CODE_BAD_REQUEST, "request must be a JSON object")
        request_id = int(request.get("id") or 0)
        payload = _payload_for(str(request.get("op") or ""), request)
        return {"id": request_id, "ok": True, "payload": payload}
    except _OpError as exc:
        return {
            "id": request_id,
            "ok": False,
            "code": exc.code,
            "error": str(exc)[:500],
        }
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
        return {
            "id": request_id,
            "ok": False,
            "code": CODE_BACKEND_ERROR,
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


def serve_forever(stdin=None, stdout=None, watchdog_seconds: float | None = None) -> int:
    """Read requests until stdin closes; returns an exit code.

    ``watchdog_seconds=0`` disables the watchdog (used by tests driving the
    loop with in-memory streams).
    """
    reader = stdin if stdin is not None else sys.stdin
    writer = _unsanitized_stream(stdout if stdout is not None else sys.stdout)
    watchdog = _Watchdog(writer, _resolve_watchdog_seconds(watchdog_seconds))
    try:
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
            request_id = int(request.get("id") or 0) if isinstance(request, dict) else 0
            watchdog.arm(request_id)
            try:
                response = handle_request(request)
            finally:
                watchdog.disarm()
            writer.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
            writer.flush()
    finally:
        watchdog.stop()
