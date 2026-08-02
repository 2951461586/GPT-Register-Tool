"""Concurrency gates for expensive registration stage groups.

Registration progress reporting remains independent. This module owns only
stage-to-resource mapping, bounded admission, gate lifetime, and aggregate wait
metrics.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
from typing import Any

from .config import CFG


_STAGE_GROUPS = {
    "auth_flow": "network",
    "user_register": "network",
    "email_otp_send": "network",
    "email_otp_resend": "network",
    "email_otp_validate": "network",
    "create_account": "network",
    "auth_session": "network",
    "codex_oauth": "network",
    "access_token_probe": "at_probe",
    "payment_link": "payment",
}
_DEFAULT_CAPS = {"network": 4, "at_probe": 4, "payment": 2}

_gate_lock = threading.Lock()
_stage_gates: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_held_gate: contextvars.ContextVar[tuple[str, threading.BoundedSemaphore] | None] = contextvars.ContextVar(
    "registration_stage_gate",
    default=None,
)
_metrics_lock = threading.Lock()
_metrics: dict[str, dict[str, float]] = {}


def enter_registration_stage(stage: str) -> float:
    """Enter the resource group for ``stage`` and return queue wait time."""
    group = _STAGE_GROUPS.get(str(stage or ""), "")
    held = _held_gate.get()
    if held is not None and held[0] == group:
        return 0.0
    release_registration_stage()
    if not group:
        return 0.0

    gate = _gate_for(group)
    started = time.perf_counter()
    gate.acquire()
    waited_ms = (time.perf_counter() - started) * 1000
    _held_gate.set((group, gate))
    with _metrics_lock:
        metrics = _metrics.setdefault(group, {"transitions": 0, "wait_ms": 0.0})
        metrics["transitions"] += 1
        metrics["wait_ms"] += round(waited_ms, 3)
    return waited_ms


def release_registration_stage() -> None:
    held = _held_gate.get()
    if held is None:
        return
    _held_gate.set(None)
    try:
        held[1].release()
    except ValueError:
        pass


def registration_stage_metrics(reset: bool = False) -> dict[str, dict[str, float]]:
    with _metrics_lock:
        snapshot = json.loads(json.dumps(_metrics))
        if reset:
            _metrics.clear()
        return snapshot


def registration_stage_group(stage: str) -> str:
    return _STAGE_GROUPS.get(str(stage or ""), "")


def _stage_cap(group: str) -> int:
    registration = CFG.get("registration") if isinstance(CFG.get("registration"), dict) else {}
    values = registration.get("stage_concurrency") if isinstance(registration.get("stage_concurrency"), dict) else {}
    default = _DEFAULT_CAPS.get(group, 4)
    try:
        return max(1, min(int(values.get(group) or default), 20))
    except (TypeError, ValueError):
        return default


def _gate_for(group: str) -> threading.BoundedSemaphore:
    key = (group, _stage_cap(group))
    with _gate_lock:
        return _stage_gates.setdefault(key, threading.BoundedSemaphore(key[1]))
