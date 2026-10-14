"""Regression cover for the serialising stdout wrapper.

`SanitizingTextIO` is the only thing standing between a multi-worker run and a
stdout stream the host cannot parse, so it needs its own tests rather than
incidental cover from something else.
"""

from __future__ import annotations

import io
import re
import threading

from sms_tool.diagnostics import SanitizingTextIO


def _concurrent_lines(threads: int, per_thread: int, payload_len: int) -> list[str]:
    sink = io.StringIO()
    stream = SanitizingTextIO(sink)
    payload = "x" * payload_len

    def worker(index: int) -> None:
        tag = "L%02d-" % index
        for _ in range(per_thread):
            print(tag + payload, file=stream)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for task in workers:
        task.start()
    for task in workers:
        task.join()
    stream.flush()
    return [line for line in sink.getvalue().split("\n") if line]


def test_concurrent_writes_never_tear_a_line():
    """Two threads must not end up inside one line.

    ``print`` issues one ``write`` for the text and another for the newline,
    and the lock is held only *inside* each call -- so a shared partial-write
    buffer lets a second thread splice itself into the first thread's line
    between those two calls. The buffer has to be per-thread for a line to be
    indivisible. Measured before the per-thread fix: 184 of 800 lines torn.
    """
    threads, per_thread, payload_len = 8, 50, 180
    lines = _concurrent_lines(threads, per_thread, payload_len)

    assert len(lines) == threads * per_thread
    torn = [
        line
        for line in lines
        if not re.fullmatch(r"L\d\d-x{%d}" % payload_len, line)
    ]
    assert torn == []


def test_concurrent_writes_keep_every_worker_represented():
    """No thread's output may be swallowed by another's buffer."""
    lines = _concurrent_lines(6, 20, 64)

    assert len(lines) == 6 * 20
    assert {line[:4] for line in lines} == {"L%02d-" % i for i in range(6)}


def test_unbuffered_stream_writes_through():
    """stderr opts out of line buffering: a traceback fragment must not wait
    for a newline that may never come."""
    sink = io.StringIO()
    stream = SanitizingTextIO(sink, line_buffer=False)

    stream.write("partial fragment without newline")

    assert sink.getvalue() == "partial fragment without newline"


def test_flush_releases_a_partial_write():
    sink = io.StringIO()
    stream = SanitizingTextIO(sink)

    stream.write("no newline yet")
    assert sink.getvalue() == ""
    stream.flush()
    assert sink.getvalue() == "no newline yet"
