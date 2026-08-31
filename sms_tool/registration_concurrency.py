"""Concurrency gates for expensive registration stage groups.

Registration progress reporting remains independent. This module owns only
stage-to-resource mapping, bounded admission, gate lifetime, and aggregate wait
metrics.

Admission is layered: an in-process ``BoundedSemaphore`` keeps local fairness
and existing single-process behaviour, and (by default) a cross-process
file-lock semaphore enforces the same cap across every process on the machine,
so running the CLI and the desktop workbench concurrently no longer oversells
the proxy/sentinel quota a group is meant to bound. Disable the outer layer
with ``registration.stage_concurrency.cross_process: false``.

Ownership is **explicit**: :func:`acquire_registration_stage` returns a
:class:`RegistrationStageLease` that the caller owns and must release. It is
deliberately *not* tracked in a :class:`contextvars.ContextVar`. Worker threads
of a ``ThreadPoolExecutor`` are reused and ``submit()`` does not copy the
context, so a context-local "which gate do I own" marker leaks into the next
task on the same worker; that task then believes it already holds the group and
skips acquisition, silently disabling the cap. See
``tests/test_registration_stage_concurrency.py`` for the regression guard.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .config import CFG
from .cross_process_gate import CrossProcessSemaphore, GateTimeoutError
from .paths import runtime_file


_STAGE_GROUPS = {
    "auth_flow": "auth",
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
_DEFAULT_CAPS = {"auth": 1, "network": 4, "at_probe": 4, "payment": 2}
_CROSS_GATE_TIMEOUT_SECONDS = 600.0

_gate_lock = threading.Lock()
_stage_gates: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_cross_gates: dict[tuple[str, int], CrossProcessSemaphore | None] = {}
_metrics_lock = threading.Lock()
_metrics: dict[str, dict[str, float]] = {}
_rate_limit_lock = threading.Lock()
# Deliberately process-wide: an upstream HTTP 429 throttles the *egress IP*,
# and every account in this process shares the same proxy pool, so one account
# being rate limited means all of them are. Scoping this per account would
# hammer the same throttled egress instead of backing off.
_rate_limit_blocked_until = 0.0


class RegistrationStageLease:
    """Explicit ownership of one held stage gate.

    The holder is responsible for :meth:`release`. Releasing twice is a no-op,
    and releasing a gate whose permit was already returned by someone else is
    recorded in the stage metrics instead of being silently swallowed, because
    an over-release means the cap has been corrupted for the rest of the
    process lifetime.
    """

    __slots__ = ("group", "waited_ms", "_gate", "_cross", "_released")

    def __init__(
        self,
        group: str,
        gate: threading.BoundedSemaphore,
        cross: CrossProcessSemaphore | None,
        waited_ms: float,
    ) -> None:
        self.group = group
        self.waited_ms = waited_ms
        self._gate = gate
        self._cross = cross
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._cross is not None:
            try:
                self._cross.release()
            except OSError:
                # A lost lock file must not stop the in-process gate from being
                # returned; the cross-process layer re-creates it on next use.
                pass
        try:
            self._gate.release()
        except ValueError:
            # BoundedSemaphore rejects releasing more times than acquired.
            # Swallowing it hides a corrupted cap, so surface it as a metric.
            _record_over_release(self.group)

    def __enter__(self) -> "RegistrationStageLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        state = "released" if self._released else "held"
        return f"<RegistrationStageLease {self.group} {state} waited={self.waited_ms:.1f}ms>"


def mark_registration_rate_limited(retry_after_seconds: float = 300.0) -> float:
    """Pause new auth-flow admissions after an upstream HTTP 429."""
    global _rate_limit_blocked_until
    delay = max(1.0, min(float(retry_after_seconds or 300.0), 3600.0))
    with _rate_limit_lock:
        _rate_limit_blocked_until = max(_rate_limit_blocked_until, time.time() + delay)
        return _rate_limit_blocked_until


def clear_registration_rate_limit() -> None:
    global _rate_limit_blocked_until
    with _rate_limit_lock:
        _rate_limit_blocked_until = 0.0


def registration_rate_limit_remaining() -> float:
    with _rate_limit_lock:
        return max(0.0, _rate_limit_blocked_until - time.time())


def _raise_if_registration_rate_limited(group: str) -> None:
    if group != "auth":
        return
    remaining = registration_rate_limit_remaining()
    if remaining > 0:
        raise RuntimeError(f"registration_rate_limit_circuit_open:retry_after={remaining:.0f}s")


def _record_transition(group: str, waited_ms: float) -> None:
    with _metrics_lock:
        metrics = _metrics.setdefault(group, {"transitions": 0, "wait_ms": 0.0, "over_releases": 0})
        metrics["transitions"] += 1
        metrics["wait_ms"] += round(waited_ms, 3)


def _record_over_release(group: str) -> None:
    with _metrics_lock:
        metrics = _metrics.setdefault(group, {"transitions": 0, "wait_ms": 0.0, "over_releases": 0})
        metrics["over_releases"] += 1


def acquire_registration_stage(stage: str) -> RegistrationStageLease | None:
    """Acquire the gate for ``stage`` and return the lease the caller owns.

    Returns ``None`` when the stage is not concurrency-gated, so callers can
    treat "no gate" and "gate held" uniformly. The caller **must** release the
    returned lease; a leak holds one permit for the rest of the process life
    but never satisfies another caller by accident, which is exactly what the
    previous context-local ownership got wrong.
    """
    group = _STAGE_GROUPS.get(str(stage or ""), "")
    if not group:
        return None

    _raise_if_registration_rate_limited(group)

    gate = _gate_for(group)
    cross = _cross_gate_for(group)
    started = time.perf_counter()
    gate.acquire()
    try:
        _raise_if_registration_rate_limited(group)
    except Exception:
        gate.release()
        raise
    if cross is not None:
        try:
            cross.acquire(timeout=_CROSS_GATE_TIMEOUT_SECONDS)
        except GateTimeoutError as exc:
            gate.release()
            raise RuntimeError(
                f"registration stage gate '{group}' stayed saturated across processes "
                f"for over {_CROSS_GATE_TIMEOUT_SECONDS}s"
            ) from exc
        except BaseException:
            # Any other failure (missing lock dir, permission, interrupt) must
            # still return the in-process permit.
            gate.release()
            raise
    waited_ms = (time.perf_counter() - started) * 1000
    _record_transition(group, waited_ms)
    return RegistrationStageLease(group, gate, cross, waited_ms)


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


def _stage_concurrency_cfg() -> dict[str, Any]:
    registration = CFG.get("registration") if isinstance(CFG.get("registration"), dict) else {}
    values = registration.get("stage_concurrency") if isinstance(registration.get("stage_concurrency"), dict) else {}
    return values if isinstance(values, dict) else {}


def _gate_for(group: str) -> threading.BoundedSemaphore:
    key = (group, _stage_cap(group))
    with _gate_lock:
        return _stage_gates.setdefault(key, threading.BoundedSemaphore(key[1]))


def _cross_gate_for(group: str) -> CrossProcessSemaphore | None:
    if not bool(_stage_concurrency_cfg().get("cross_process", True)):
        return None
    key = (group, _stage_cap(group))
    with _gate_lock:
        if key not in _cross_gates:
            try:
                _cross_gates[key] = CrossProcessSemaphore(
                    f"registration_{group}",
                    key[1],
                    base_dir=runtime_file(CFG, ""),
                )
            except OSError:
                _cross_gates[key] = None
        return _cross_gates[key]
