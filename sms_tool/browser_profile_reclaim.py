"""Find live processes still holding a browser profile directory.

Why this exists
---------------
The browser drivers launch with a *persistent* profile (``user_data_dir``), and
that directory is per-account: ``runtime/browser_profiles/camoufox/<email>/``.
A browser that survives its parent -- the CLI was killed, the WPF host was closed
mid-run, an attempt raised past ``PlaywrightBrowserSession.close`` -- keeps the
profile open, and the next launch on that same directory fails with a
"profile is already in use" error that says nothing about *which* process holds
it.  The operator is left with a dead profile directory and no way to see why.

Detecting "is it stale?" is not a file question
-----------------------------------------------
Every Camoufox (Firefox-based) profile contains ``parent.lock`` -- it is created
at startup and normally **left behind**, so its presence says nothing about
whether anyone holds the profile (measured 2026-09-22: all four profiles under
``runtime/browser_profiles/camoufox/`` carry one while no browser is running).
Chromium's ``SingletonLock`` symlink is a different story but does not exist on
Windows.  The only portable answer is to look for **live processes whose command
line references the profile directory**, which is what this module does.

Termination is opt-in and image-guarded
--------------------------------------
``reclaim_stale_profile`` defaults to ``terminate=False``: it reports.  Killing
is a destructive action on someone else's process, so when it *is* requested it
only touches processes whose image name looks like a browser we could have
launched.  An unrelated process that merely mentions the path (a backup tool, a
grep) is reported and never killed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

_LOGGER = logging.getLogger(__name__)

#: Image-name fragments that identify a browser this project could have started.
#: Used as the guard on termination, never as the way a holder is *found*.
BROWSER_IMAGE_MARKERS = (
    "camoufox", "firefox", "chrome", "chromium", "msedge", "playwright", "node",
)

_PROCESS_TIMEOUT_S = 15.0

#: How a browser is actually told which profile to use. A profile path that is
#: not introduced this way is almost always an accident of the command line --
#: measured 2026-09-22: a plain substring match flagged the *python* process that
#: merely had the path inside a `-c` script, which would have made every
#: diagnosis wrong in the one direction that wastes an operator's time.
_PROFILE_FLAG_PREFIXES = (
    "--user-data-dir=",
    "--profile=",
    "-profile=",
    "--user-data-dir",
    "--profile",
    "-profile",
)
_TOKEN_SPLIT_RE = re.compile(r'[\s"]+')


def _normalize(path: Any) -> str:
    """Case/separator-insensitive form for matching against command lines."""
    return str(path or "").strip().replace("\\", "/").rstrip("/").lower()


def _references_profile(command_line: str, target: str) -> bool:
    """Does ``command_line`` name ``target`` as a profile location?

    Exact token matching, not substring: the path must either stand alone as an
    argument (``-profile <dir>``) or follow a profile flag (``--profile=<dir>``).
    A sibling directory, or a path that merely appears inside some other
    argument, is not a holder.
    """
    if not target:
        return False
    for token in _TOKEN_SPLIT_RE.split(_normalize(command_line)):
        if not token:
            continue
        if token == target:
            return True
        for prefix in _PROFILE_FLAG_PREFIXES:
            if token.startswith(prefix) and token[len(prefix):].strip("=").rstrip("/") == target:
                return True
    return False


def _decode(raw: Any) -> str:
    """Decode subprocess output that is not guaranteed to be UTF-8.

    ``text=True`` is not usable here: on a non-English Windows the PowerShell
    pipe is encoded in the console code page, and the decoder raises
    ``UnicodeDecodeError`` **inside the reader thread** -- which ``subprocess``
    does not surface as an exception, it just leaves ``stdout`` empty. The lookup
    then reports "no holders" on a machine that has plenty, i.e. exactly the
    silent failure this module exists to remove. Measured 2026-09-22 on this
    machine: byte 0xcc at offset 45159 (a GBK path) killed the enumeration.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _iter_processes_windows() -> Iterable[tuple[int, str, str]]:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=_PROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.warning("could not enumerate processes: %s", exc)
        return
    if proc.returncode != 0:
        _LOGGER.warning(
            "process enumeration exited %s: %s", proc.returncode, _decode(proc.stderr)[:200]
        )
        return
    try:
        rows = json.loads(_decode(proc.stdout) or "[]")
    except ValueError as exc:
        _LOGGER.warning("could not parse process list: %s", exc)
        return
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        try:
            yield int(row.get("ProcessId") or 0), str(row.get("Name") or ""), str(row.get("CommandLine") or "")
        except (TypeError, ValueError):
            continue


def _iter_processes_posix() -> Iterable[tuple[int, str, str]]:
    root = Path("/proc")
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        parts = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\x00") if chunk]
        if not parts:
            continue
        yield int(entry.name), os.path.basename(parts[0]), " ".join(parts)


def iter_processes() -> Iterable[tuple[int, str, str]]:
    """Yield ``(pid, image_name, command_line)`` for every visible process."""
    if sys.platform == "win32":
        yield from _iter_processes_windows()
    else:
        yield from _iter_processes_posix()


def is_browser_image(image: str) -> bool:
    lowered = str(image or "").strip().lower()
    return any(marker in lowered for marker in BROWSER_IMAGE_MARKERS)


def profile_holders(
    profile_dir: Any,
    *,
    enumerate_processes: Callable[[], Iterable[tuple[int, str, str]]] = iter_processes,
) -> list[dict[str, Any]]:
    """Processes whose command line references ``profile_dir``.

    Matched on the normalized path, so Windows' backslashes and case differences
    do not hide a holder.
    """
    target = _normalize(profile_dir)
    if not target:
        return []
    holders: list[dict[str, Any]] = []
    for pid, image, cmdline in enumerate_processes() or ():
        if not cmdline or not _references_profile(cmdline, target):
            continue
        holders.append({
            "pid": int(pid),
            "image": str(image or ""),
            "command_line": str(cmdline or ""),
            "is_browser": is_browser_image(image),
        })
    return holders


def describe_contention(
    profile_dir: Any,
    *,
    enumerate_processes: Callable[[], Iterable[tuple[int, str, str]]] = iter_processes,
) -> str:
    """One-line, operator-facing explanation of who holds ``profile_dir``.

    Empty string when nothing holds it -- callers use that as the "no contention"
    signal rather than parsing a message.
    """
    holders = profile_holders(profile_dir, enumerate_processes=enumerate_processes)
    if not holders:
        return ""
    described = ", ".join(
        f"PID {holder['pid']} ({holder['image'] or 'unknown'})" for holder in holders[:4]
    )
    extra = "" if len(holders) <= 4 else f" and {len(holders) - 4} more"
    return (
        f"profile {profile_dir} is held by {described}{extra}; "
        "close it or run the profile reclaim path before retrying"
    )


def reclaim_stale_profile(
    profile_dir: Any,
    *,
    terminate: bool = False,
    enumerate_processes: Callable[[], Iterable[tuple[int, str, str]]] = iter_processes,
) -> dict[str, Any]:
    """Report -- and optionally terminate -- processes holding ``profile_dir``.

    Returns ``{"profile": str, "holders": [...], "terminated": [pid, ...],
    "skipped": [pid, ...]}``.  ``terminate`` defaults to ``False``: this function
    is a diagnosis tool first.  Even when asked to terminate, only processes whose
    image looks like a browser are touched; anything else is listed under
    ``skipped`` and left running.
    """
    holders = profile_holders(profile_dir, enumerate_processes=enumerate_processes)
    report: dict[str, Any] = {
        "profile": str(profile_dir),
        "holders": holders,
        "terminated": [],
        "skipped": [],
    }
    if not terminate:
        if holders:
            _LOGGER.warning("%s", describe_contention(profile_dir, enumerate_processes=enumerate_processes))
        return report

    for holder in holders:
        pid = int(holder["pid"])
        if pid == os.getpid() or not holder.get("is_browser"):
            report["skipped"].append(pid)
            continue
        if _terminate(pid):
            report["terminated"].append(pid)
        else:
            report["skipped"].append(pid)
    return report


def _terminate(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            # No ``text=True``: a localized taskkill message is code-page encoded
            # and would blow up the reader thread (see ``_decode``).
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=_PROCESS_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOGGER.warning("taskkill %s failed: %s", pid, exc)
            return False
        if proc.returncode != 0:
            _LOGGER.warning("taskkill %s exited %s: %s", pid, proc.returncode, _decode(proc.stderr)[:200])
        return proc.returncode == 0
    try:
        os.kill(pid, 9)
    except OSError as exc:
        _LOGGER.warning("kill %s failed: %s", pid, exc)
        return False
    return True
