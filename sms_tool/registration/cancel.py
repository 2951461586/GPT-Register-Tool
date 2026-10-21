"""Cooperative cancellation for CLI/desktop registration batches.

Two channels feed one in-process event:

* :func:`request_registration_cancel` -- programmatic producers (IPC command,
  tests, future desktop wiring).
* ``SIGINT``/``SIGBREAK`` handlers installed by :func:`cancel_scope` -- the
  first Ctrl+C requests a graceful stop, a second one hard-interrupts.

Workers observe the event at bounded checkpoints (between batch accounts,
between retry attempts, at registration stage boundaries, inside OTP polling,
before browser page operations). They stop with ``registration_cancelled``
results instead of dying mid-account, so persisted state and browser cleanup
stay consistent.
"""

from __future__ import annotations

import contextlib
import signal
import threading


_cancel_event = threading.Event()


class RegistrationCancelled(Exception):
    """Raised at a cooperative checkpoint when cancellation was requested."""

    code = "registration_cancelled"

    def __str__(self) -> str:
        return self.code


def request_registration_cancel() -> None:
    _cancel_event.set()


def clear_registration_cancel() -> None:
    _cancel_event.clear()


def registration_cancel_requested() -> bool:
    return _cancel_event.is_set()


def ensure_not_cancelled() -> None:
    """Raise :class:`RegistrationCancelled` at a cooperative checkpoint."""
    if _cancel_event.is_set():
        raise RegistrationCancelled()


def _install_cancel_signal_handlers() -> list[tuple[int, object]]:
    def _handler(signum, frame):
        if not registration_cancel_requested():
            request_registration_cancel()
            print(
                "\n[!] Cancel requested; registration stops at the next safe checkpoint. "
                "Press Ctrl+C again to interrupt immediately.",
                flush=True,
            )
            return
        raise KeyboardInterrupt

    installed: list[tuple[int, object]] = []
    signals = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    for sig in signals:
        try:
            previous = signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses the handler.
            continue
        installed.append((sig, previous))
    return installed


@contextlib.contextmanager
def cancel_scope(*, install_signals: bool = True):
    """Own the global cancel flag for one batch run.

    On exit the signal handlers are restored and the flag is cleared, so a
    cancellation can never leak into a later batch in the same process.
    """
    installed = _install_cancel_signal_handlers() if install_signals else []
    try:
        yield
    finally:
        for sig, previous in reversed(installed):
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass
        clear_registration_cancel()


def cancellable_sleep(seconds: float, *, poll_interval: float = 2.0, requested=None) -> bool:
    """Sleep ``seconds`` in bounded slices, aborting early on cancellation.

    ``requested`` is an extra predicate (e.g. a per-batch event) OR-ed with the
    global cancel flag. Returns True when cancellation was requested during the
    sleep (the caller should abort), False when the full duration elapsed.

    The budget is sliced up front and each slice is charged after its sleep
    returns, so the loop is finite even when ``time.sleep`` is faked to a no-op
    by tests or sandboxes.
    """
    import time

    def _stop() -> bool:
        if registration_cancel_requested():
            return True
        return bool(requested()) if requested is not None else False

    total = max(0.0, float(seconds or 0.0))
    if total <= 0:
        return _stop()
    step = max(0.05, min(float(poll_interval or 2.0), total))
    slept = 0.0
    while slept < total:
        if _stop():
            return True
        slice_seconds = min(step, total - slept)
        time.sleep(slice_seconds)
        slept += slice_seconds
    return _stop()


__all__ = [
    "RegistrationCancelled",
    "cancellable_sleep",
    "cancel_scope",
    "clear_registration_cancel",
    "ensure_not_cancelled",
    "registration_cancel_requested",
    "request_registration_cancel",
]
