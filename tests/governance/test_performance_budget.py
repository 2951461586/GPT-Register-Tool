"""Coarse performance budgets for the entrypoints users feel.

These are **regression detectors with generous ceilings**, not benchmarks: a
shared CI runner varies by seconds, so the numbers are chosen to catch a change
of kind (a heavy import returning, a resident-only path regressing to a cold
start) rather than a change of degree. The measured baselines live in
``docs/current/performance-budgets.md``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

#: Cold `import sms_tool.cli` (lazy facade). Measured 0.17-0.28s on Windows;
#: a leaked top-level import of requests/curl_cffi/playwright adds ~0.5-1.5s.
IMPORT_SECONDS = 3.0
#: RSS right after that import. Measured ~24 MB; the fast path must not pull
#: the browser/HTTP stack (~40-80 MB).
IMPORT_RSS_MB = 150
#: `--help` builds the whole argparse surface (~190 options). Measured 0.7-1.1s.
HELP_SECONDS = 6.0
#: Resident desktop channel: 795 rows, one full `read_accounts` (every session
#: parsed and sanitized into the row caches), then ten deltas. Measured 37 MB;
#: the ceiling catches a cache that stops being bounded, or a resident-only
#: import pulling the browser/HTTP stack. The entrypoint budgets above never
#: covered this path -- it is the process the operator leaves running all day.
RESIDENT_RSS_MB = 150


def _measure(args: list[str]) -> tuple[float, str]:
    started = time.perf_counter()
    result = subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    elapsed = time.perf_counter() - started
    assert result.returncode == 0, result.stderr[-800:]
    return elapsed, result.stdout


def test_cli_import_stays_within_its_time_and_memory_budget() -> None:
    elapsed, stdout = _measure(
        [
            PYTHON,
            "-c",
            "import psutil, sms_tool.cli; print(round(psutil.Process().memory_info().rss / 1048576))",
        ]
    )
    rss_mb = int(stdout.strip().splitlines()[-1])

    assert elapsed < IMPORT_SECONDS, f"import sms_tool.cli took {elapsed:.2f}s"
    assert rss_mb < IMPORT_RSS_MB, f"import sms_tool.cli kept {rss_mb} MB resident"


def test_help_stays_within_its_budget() -> None:
    elapsed, stdout = _measure([PYTHON, str(ROOT / "chatgpt_phone_reg.py"), "--help"])

    assert "--registration-driver" in stdout
    assert elapsed < HELP_SECONDS, f"--help took {elapsed:.2f}s"


def test_resident_desktop_read_stays_within_its_memory_budget(tmp_path) -> None:
    _, stdout = _measure(
        [
            PYTHON,
            str(ROOT / "tests" / "fixtures" / "resident_rss_probe.py"),
            str(tmp_path / "resident.sqlite3"),
        ]
    )
    rss_mb = int(stdout.strip().splitlines()[-1])
    assert rss_mb < RESIDENT_RSS_MB, (
        f"resident read kept {rss_mb} MB after 795 rows + 10 deltas "
        f"(budget {RESIDENT_RSS_MB} MB); check the row caches in sms_tool/desktop_read.py"
    )
