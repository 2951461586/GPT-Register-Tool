"""Cross-process counting semaphore backed by OS file locks.

``registration_concurrency`` stage gates and the ``sentinel_tokens`` admission
gate are in-process ``BoundedSemaphore`` objects, so running the CLI and the
desktop workbench (or two CLIs) at the same time oversells the proxy/sentinel
quota each gate is meant to enforce.  This module provides a slot-file
semaphore: acquiring takes an exclusive OS lock on one of ``limit`` slot files
under ``runtime/gates/<name>/``, so the cap holds across every process on the
machine.  The probe cache pattern (atomic temp + replace) is not needed here
because the lock itself lives in the filesystem.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

try:  # Windows
    import msvcrt

    def _lock_byte(handle) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_byte(handle) -> None:
        try:
            os.lseek(handle.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
except ImportError:  # POSIX fallback
    import fcntl

    def _lock_byte(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_byte(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class GateTimeoutError(TimeoutError):
    """Raised when a slot cannot be acquired within the timeout."""


class CrossProcessSemaphore:
    """Counting semaphore shared by every process using the same gate directory."""

    def __init__(self, name: str, limit: int, *, base_dir: str | Path) -> None:
        self.name = str(name or "gate").strip() or "gate"
        self.limit = max(1, int(limit))
        self.gate_dir = Path(base_dir) / "gates" / _safe_name(self.name)
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        self._local_lock = __import__("threading").RLock()
        self._handles: list = []

    def _slot_path(self, slot: int) -> Path:
        return self.gate_dir / f"slot-{slot}.lock"

    def acquire(self, timeout: float = 600.0, *, poll_interval: float = 0.1) -> None:
        """Block until a slot is free, up to ``timeout`` seconds."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._local_lock:
                if len(self._handles) >= self.limit:
                    pass  # this process already holds every slot
                else:
                    handle = self._try_acquire_any_slot()
                    if handle is not None:
                        self._handles.append(handle)
                        return
            if time.monotonic() >= deadline:
                raise GateTimeoutError(
                    f"cross-process gate '{self.name}' did not free a slot within {timeout}s"
                )
            time.sleep(poll_interval)

    def _try_acquire_any_slot(self):
        for slot in range(self.limit):
            path = self._slot_path(slot)
            try:
                handle = open(path, "a+b")
            except OSError:
                continue
            try:
                _lock_byte(handle)
            except OSError:
                handle.close()
                continue
            return handle
        return None

    def release(self) -> None:
        with self._local_lock:
            if not self._handles:
                return
            handle = self._handles.pop()
            try:
                _unlock_byte(handle)
            finally:
                handle.close()

    def __enter__(self) -> "CrossProcessSemaphore":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in name)


@contextlib.contextmanager
def cross_process_write_lock(lock_path: str | Path, *, timeout: float = 30.0, poll_interval: float = 0.05):
    """Exclusive cross-process lock for serialising writes to a shared file.

    Reuses the same OS byte-lock primitive as ``CrossProcessSemaphore`` so two
    backend processes (CLI + workbench, or two CLIs) cannot interleave writes to
    the same state file and hand out the same phone number / proxy slot twice.
    Blocks up to ``timeout`` seconds; raises ``GateTimeoutError`` if it cannot
    acquire the lock in time (never silently proceeds with a torn write).
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    try:
        while True:
            try:
                _lock_byte(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GateTimeoutError(
                        f"cross-process write lock {lock_path} not acquired within {timeout}s"
                    )
                time.sleep(poll_interval)
        yield
    finally:
        if acquired:
            _unlock_byte(handle)
        try:
            handle.close()
        except OSError:
            pass
