"""Cover for the stdout mirror that persists ``print()`` diagnostics.

The protocol lane explains itself through ``print()``. Until this mirror existed
those lines only reached the desktop host's IPC pipe and were never written to
disk, so a run that died before its first progress record (2026-09-13 20:46,
``exited with code 2`` after 10m51s of silent proxy probing) left no evidence at
all. Two properties matter and are asserted directly here:

1. the mirror actually persists what ``print`` emits, and
2. it is *passive* -- byte-identical stdout, sanitized input, no new root
   handlers, and a broken mirror cannot take the stream down.
"""

from __future__ import annotations

import io
import json
import logging

from sms_tool.diagnostics import SanitizingTextIO
from sms_tool.stdout_mirror import StdoutMirror, install_stdout_mirror


def _mirror(tmp_path):
    return StdoutMirror(tmp_path / "backend_stdout.log")


def _text(tmp_path):
    lines = (tmp_path / "backend_stdout.log").read_text(encoding="utf-8").splitlines()
    return "".join(json.loads(line)["message"] + "\n" for line in lines)


def test_mirror_persists_complete_lines(tmp_path):
    mirror = _mirror(tmp_path)
    mirror.write("  Status: 409\n")
    mirror.write("  Existing account OTP send: /resend 409\n")
    mirror.close()

    assert _text(tmp_path) == (
        "  Status: 409\n  Existing account OTP send: /resend 409\n"
    )


def test_disk_mirror_correlates_without_changing_stdout(tmp_path, monkeypatch):
    from sms_tool.telemetry import current_run_id

    monkeypatch.setenv("SMS_TOOL_COMMAND_ID", "fixture-command")
    marker = current_run_id.set("fixture-run")
    try:
        mirror = _mirror(tmp_path)
        stream = SanitizingTextIO(io.StringIO(), mirror=mirror)
        stream.write("stage detail\n")
        mirror.close()
        row = json.loads((tmp_path / "backend_stdout.log").read_text())
        assert row["command_id"] == "fixture-command"
        assert row["run_id"] == "fixture-run"
        assert row["timestamp"]
        assert row["message"] == "stage detail"
    finally:
        current_run_id.reset(marker)


def test_default_logs_are_isolated_by_process(tmp_path, monkeypatch):
    from sms_tool import logging_setup
    from sms_tool.stdout_mirror import default_stdout_log_path

    monkeypatch.setattr(logging_setup, "default_log_dir", lambda: tmp_path)
    monkeypatch.setattr(logging_setup.os, "getpid", lambda: 101)
    first = default_stdout_log_path()
    monkeypatch.setattr(logging_setup.os, "getpid", lambda: 202)
    second = default_stdout_log_path()
    assert first != second
    assert first.name == second.name == "backend_stdout.jsonl"
    assert logging_setup._default_log_path().parent == second.parent


def test_mirror_buffers_a_partial_line_until_flush(tmp_path):
    """A line without its newline must not be split across two records."""
    mirror = _mirror(tmp_path)
    mirror.write("no newline yet")
    assert _text(tmp_path) == ""

    mirror.write(" and more\n")
    assert _text(tmp_path) == "no newline yet and more\n"


def test_flush_releases_a_pending_partial_line(tmp_path):
    mirror = _mirror(tmp_path)
    mirror.write("trailing fragment")
    mirror.flush()

    assert _text(tmp_path) == "trailing fragment\n"


def test_mirror_does_not_change_what_the_wrapped_stream_receives(tmp_path):
    """stdout is the IPC channel; the mirror must be a pure observer."""
    sink = io.StringIO()
    mirror = _mirror(tmp_path)
    stream = SanitizingTextIO(sink, mirror=mirror)

    stream.write("@@SMSWORKBENCH_V2@@ {\"stage\": \"started\"}\n")
    stream.flush()
    mirror.close()

    assert sink.getvalue() == "@@SMSWORKBENCH_V2@@ {\"stage\": \"started\"}\n"
    assert _text(tmp_path) == "@@SMSWORKBENCH_V2@@ {\"stage\": \"started\"}\n"


def test_mirror_receives_sanitized_text_not_raw_credentials(tmp_path):
    """The new file must not become a fresh leak surface.

    The proxy URL below is the shape the protocol lane prints when a route is
    probed, so this is the realistic case rather than a synthetic token.
    """
    sink = io.StringIO()
    mirror = _mirror(tmp_path)
    stream = SanitizingTextIO(sink, mirror=mirror)

    stream.write("proxy_bridge -> http://user:hunter2@us.example:7878\n")
    stream.flush()
    mirror.close()

    recorded = _text(tmp_path)
    assert "hunter2" not in recorded
    assert "[REDACTED]" in recorded


def test_mirror_records_unbuffered_stderr_fragments(tmp_path):
    """stderr opts out of line buffering; the mirror still must not lose it."""
    sink = io.StringIO()
    mirror = _mirror(tmp_path)
    stream = SanitizingTextIO(sink, line_buffer=False, mirror=mirror)

    stream.write("Traceback (most recent call last):")
    stream.write("\nRuntimeError: boom\n")
    stream.flush()
    mirror.close()

    assert sink.getvalue() == "Traceback (most recent call last):\nRuntimeError: boom\n"
    assert "RuntimeError: boom" in _text(tmp_path)


def test_a_failing_mirror_never_breaks_the_stream(tmp_path):
    """Losing a line is bad; killing the run because the observer failed is worse."""

    class Exploding:
        def write(self, text):
            raise OSError("disk gone")

        def flush(self):
            raise OSError("disk gone")

    sink = io.StringIO()
    stream = SanitizingTextIO(sink, mirror=Exploding())

    stream.write("still reaches the host\n")
    stream.flush()

    assert sink.getvalue() == "still reaches the host\n"


def test_install_stdout_mirror_attaches_without_replacing_the_streams(tmp_path, monkeypatch):
    sink = io.StringIO()
    err = io.StringIO()
    out_stream = SanitizingTextIO(sink)
    err_stream = SanitizingTextIO(err, line_buffer=False)
    monkeypatch.setattr("sys.stdout", out_stream)
    monkeypatch.setattr("sys.stderr", err_stream)

    install_stdout_mirror(tmp_path / "backend_stdout.log")
    import sys

    assert sys.stdout is out_stream
    assert sys.stderr is err_stream

    print("from print", file=sys.stdout)
    print("from stderr", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()

    recorded = (tmp_path / "backend_stdout.log").read_text(encoding="utf-8")
    assert "from print" in recorded
    assert "from stderr" in recorded


def test_mirror_adds_no_handler_to_the_root_logger(tmp_path):
    """``propagate=False`` plus a private logger keeps sms_tool.log untouched."""
    before = list(logging.getLogger().handlers)
    mirror = _mirror(tmp_path)

    mirror.write("a line\n")
    mirror.close()

    assert logging.getLogger().handlers == before
    assert mirror._logger.propagate is False
