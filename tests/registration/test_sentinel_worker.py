"""The opt-in persistent Sentinel worker and its fallback contract.

The worker exists to stop paying Node's startup and module load once per token.
It must never change what a token *is*: the worker feeds the same runner the
same files, and every failure has a defined path --

* a runner verdict (bad challenge, incomplete token) is raised as-is;
* a transport failure (dead/wedged worker) closes the worker and retries once
  through the one-shot subprocess.

Node is required; the tests skip when it is absent rather than pretending.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sms_tool.sentinel import runner as runner_module

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the worker tests")


FAKE_RUNNER = """
const fs = require("node:fs");
exports.main = async function (argv, writeOutput) {
  let configPath = "";
  let challengePath = "";
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--config") configPath = argv[i + 1];
    if (argv[i] === "--challenge-file") challengePath = argv[i + 1];
  }
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const challenge = JSON.parse(fs.readFileSync(challengePath, "utf8"));
  if (challenge.failRunner) throw new Error("runner rejected the challenge");
  return JSON.stringify({
    id: config.deviceId,
    flow: config.flow,
    p: "proof",
    t: "turnstile",
    c: "challenge",
  });
};
if (require.main === module) {
  exports.main(process.argv.slice(2))
    .then((text) => process.stdout.write(text))
    .catch((error) => {
      process.stderr.write(String(error));
      process.exit(1);
    });
}
"""


def _write_runner(tmp_path: Path) -> Path:
    path = tmp_path / "sentinel-runner.js"
    path.write_text(FAKE_RUNNER, encoding="utf-8")
    return path


def _profile(**overrides):
    profile = {
        "screen": "1920x1080",
        "lang": "en-US",
        "impersonate": "chrome136",
        "timezone": "UTC",
    }
    profile.update(overrides)
    return profile


def _challenge():
    return {"token": "challenge-token"}


@pytest.fixture(autouse=True)
def _clean_worker():
    runner_module._drop_worker()
    yield
    runner_module._drop_worker()


def _install_fake_bundle(monkeypatch, tmp_path: Path) -> Path:
    runner_path = _write_runner(tmp_path)
    sdk_path = tmp_path / "sdk.js"
    sdk_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        runner_module,
        "validate_runtime_bundle",
        lambda verify_hash=True: (sdk_path, runner_path),
    )
    monkeypatch.setenv(runner_module.WORKER_ENV, "1")
    return runner_path


def test_worker_is_off_by_default(monkeypatch):
    monkeypatch.delenv(runner_module.WORKER_ENV, raising=False)
    assert runner_module.worker_enabled() is False


def test_worker_returns_a_validated_token_and_is_reused(monkeypatch, tmp_path):
    _install_fake_bundle(monkeypatch, tmp_path)

    first = runner_module.run_sentinel_sdk(
        _challenge(), flow="authorize_continue", device_id="did-1", profile=_profile(), cookie="", page_url=""
    )
    worker = runner_module._WORKER
    assert worker is not None
    pid = worker._process.pid

    second = runner_module.run_sentinel_sdk(
        _challenge(), flow="authorize_continue", device_id="did-1", profile=_profile(), cookie="", page_url=""
    )

    assert json.loads(first)["id"] == "did-1"
    assert json.loads(second)["flow"] == "authorize_continue"
    assert runner_module._WORKER is worker
    assert worker._process.pid == pid


def test_a_runner_verdict_is_not_retried_through_a_cold_start(monkeypatch, tmp_path):
    _install_fake_bundle(monkeypatch, tmp_path)

    calls = []
    original = runner_module._run_runner_once

    def spy(command, runner_path, timeout):
        calls.append(command)
        return original(command, runner_path, timeout)

    monkeypatch.setattr(runner_module, "_run_runner_once", spy)

    with pytest.raises(runner_module.SentinelRunnerError) as excinfo:
        runner_module.run_sentinel_sdk(
            {"token": "challenge-token", "failRunner": True},
            flow="authorize_continue",
            device_id="did-1",
            profile=_profile(),
            cookie="",
            page_url="",
        )

    assert "sentinel_runner_failed:worker" in str(excinfo.value)
    assert calls == [], "a runner verdict must not spend a second node start"


def test_a_transport_failure_falls_back_to_one_shot(monkeypatch, tmp_path):
    _install_fake_bundle(monkeypatch, tmp_path)

    def dead_worker(self, args, *, timeout):
        raise runner_module._WorkerTransportError("simulated wedge")

    monkeypatch.setattr(runner_module._NodeWorker, "run", dead_worker)

    fallback_calls = []
    original = runner_module._run_runner_once

    def spy(command, runner_path, timeout):
        fallback_calls.append(command)
        return original(command, runner_path, timeout)

    monkeypatch.setattr(runner_module, "_run_runner_once", spy)

    token = runner_module.run_sentinel_sdk(
        _challenge(), flow="authorize_continue", device_id="did-1", profile=_profile(), cookie="", page_url=""
    )

    assert json.loads(token)["id"] == "did-1"
    assert len(fallback_calls) == 1
    assert runner_module._WORKER is None, "the failed worker must be discarded"
