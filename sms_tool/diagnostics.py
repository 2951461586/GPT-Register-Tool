"""Mandatory operator-output seam backed by the shared sensitive policy."""

from __future__ import annotations

import sys
import threading
from typing import Any, TextIO

from .sanitizer import sanitize_command_args, sanitize_text


def safe_print(
    *values: Any,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    target = file or sys.stdout
    text = sep.join(str(value) for value in values)
    print(sanitize_text(text), end=end, file=target, flush=flush)


def safe_exception(value: BaseException | Any) -> str:
    return sanitize_text(value)



class SanitizingTextIO:
    """Text stream proxy that guarantees policy enforcement at process output.

    Doubles as the serialisation point for a multi-worker run. ``print(*args)``
    issues one ``write`` per argument plus the separators, so without a lock two
    worker threads can interleave *inside* a single line: you get two half
    lines, and the host -- which parses stdout one line at a time -- can read
    neither. A torn line is strictly worse than a reordered one, because a
    reordered line still parses.

    With ``line_buffer`` the stream holds each thread's partial writes until a
    newline shows up, then hands one complete line downstream under the lock,
    flushed immediately so the host is never left waiting. An explicit
    ``flush()`` releases whatever is pending, which keeps
    ``print(..., flush=True)`` and dot-style progress output behaving as before.
    """

    def __init__(self, wrapped: TextIO, line_buffer: bool = True) -> None:
        self._wrapped = wrapped
        self._line_buffer = line_buffer
        self._lock = threading.RLock()
        # Per-thread, not shared. A single shared buffer looks like it
        # serialises output but does not: print() calls write() once for the
        # text and again for the newline, and the lock is held only *inside*
        # each call, so between those two calls another thread appends to the
        # same buffer and both halves end up on one line. Keeping the partial
        # write per thread is what actually makes a line indivisible.
        self._buffers = threading.local()

    def _pending(self) -> str:
        try:
            return self._buffers.pending
        except AttributeError:
            self._buffers.pending = ""
            return ""

    def write(self, value: str) -> int:
        text = sanitize_text(value)
        if not self._line_buffer:
            with self._lock:
                self._wrapped.write(text)
            return len(text)
        pending = self._pending() + text
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            with self._lock:
                self._wrapped.write(line + "\n")
                self._wrapped.flush()
        self._buffers.pending = pending
        return len(text)

    def flush(self) -> None:
        pending = self._pending()
        if pending:
            with self._lock:
                self._wrapped.write(pending)
            self._buffers.pending = ""
        with self._lock:
            self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def install_safe_stdio() -> None:
    if not isinstance(sys.stdout, SanitizingTextIO):
        sys.stdout = SanitizingTextIO(sys.stdout)
    if not isinstance(sys.stderr, SanitizingTextIO):
        # stderr is not line-oriented: a traceback arrives in many fragments
        # and buffering it would delay the one stream an operator reads when
        # everything else has already gone wrong. Lock it, do not buffer it.
        sys.stderr = SanitizingTextIO(sys.stderr, line_buffer=False)
