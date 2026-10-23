"""Every pytest-xdist worker must get its own config directory.

Why this exists
---------------
``tests/conftest.py`` creates a temporary config directory and exports it as
``GPT_REGISTER_CONFIG_DIR`` before any ``sms_tool`` import. The pytest
*controller* imports that file too, and xdist workers are subprocesses that
inherit the controller's environment -- so the naive form of that fixture hands
all four workers the SAME directory.

Each worker then runs the first-load migration (legacy ``config.json`` -> the
proxy/runtime/payment shards) at the same moment. ``load_merged_config`` treats
"any shard exists" as "the sharded layout is authoritative" and deliberately
never backfills from the legacy file, so a worker that looks while only
``proxy.json`` is published sees a config with no ``chatgpt`` section:

    ERROR collecting tests/mailbox/test_email_otp_filtering.py
    sms_tool.config.schema.ConfigError: chatgpt must be an object

That is the 2026-10-23 CI failure: both runs died on that one file (gw1 in one
run, gw2 in the other), because it imports ``sms_tool.registration`` -- which
reads the config at import time, i.e. during collection, before any test ran.
``tests/mailbox`` alone reproduced it locally once the fsync window was widened.

The gate replays the controller -> worker handoff in real subprocesses: it
imports ``tests/conftest.py`` twice, the second time with the environment the
first one produced plus ``PYTEST_XDIST_WORKER``, exactly as xdist does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "conftest.py"

CONFIG_DIR_ENV = "GPT_REGISTER_CONFIG_DIR"
CONFIG_DIR_OWNER_ENV = "GPT_REGISTER_TEST_CONFIG_DIR_OWNER"
WORKER_ENV = "PYTEST_XDIST_WORKER"
_PROBE_PREFIX = "config-dir-probe:"


class _ProbeResult(TypedDict):
    """What the child process reports about the config directory it resolved."""

    dir: str
    owner: str | None
    config_json: bool


_PROBE = """
import importlib.util
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("tests_conftest_probe", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print({prefix!r} + json.dumps({{
    "dir": os.environ.get({dir_env!r}),
    "owner": os.environ.get({owner_env!r}),
    "config_json": (__import__("pathlib").Path(os.environ.get({dir_env!r}) or "") / "config.json").is_file(),
}}))
"""


def _probe(overrides: dict[str, str | None]) -> "_ProbeResult":
    """Import ``tests/conftest.py`` in a child process and report its config dir.

    ``overrides`` sets or (with ``None``) removes variables for the child, so a
    caller can build the exact environment xdist gives a worker.
    """
    env = {
        key: value for key, value in os.environ.items() if key not in {CONFIG_DIR_ENV, CONFIG_DIR_OWNER_ENV, WORKER_ENV}
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    script = _PROBE.format(
        prefix=_PROBE_PREFIX,
        dir_env=CONFIG_DIR_ENV,
        owner_env=CONFIG_DIR_OWNER_ENV,
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), str(CONFTEST)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr).strip()
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_PROBE_PREFIX):
            return cast("_ProbeResult", json.loads(line[len(_PROBE_PREFIX) :]))
    raise AssertionError(f"probe printed no result: {completed.stdout!r} {completed.stderr!r}")


def test_controller_creates_a_seeded_directory_it_owns() -> None:
    controller = _probe({WORKER_ENV: None})

    assert controller["owner"] == "1", "conftest must mark the directory it created as its own"
    assert controller["config_json"] is True, "the directory must be seeded from config.example.json"


def test_worker_does_not_reuse_the_controller_directory() -> None:
    controller = _probe({WORKER_ENV: None})
    worker = _probe(
        {
            CONFIG_DIR_ENV: controller["dir"],
            CONFIG_DIR_OWNER_ENV: "1",
            WORKER_ENV: "gw0",
        }
    )

    assert worker["dir"] != controller["dir"], (
        "an xdist worker inherited the controller's config directory; all workers then migrate "
        "the same directory concurrently and a partial shard set reads as 'chatgpt must be an object'"
    )
    assert worker["config_json"] is True, "a worker's own directory must be seeded as well"


def test_operator_supplied_directory_is_never_replaced(tmp_path: Path) -> None:
    """An explicit ``GPT_REGISTER_CONFIG_DIR`` is an operator decision, worker or not."""
    chosen = str(tmp_path / "operator_config")
    worker = _probe(
        {
            CONFIG_DIR_ENV: chosen,
            CONFIG_DIR_OWNER_ENV: None,
            WORKER_ENV: "gw0",
        }
    )

    assert worker["dir"] == chosen
