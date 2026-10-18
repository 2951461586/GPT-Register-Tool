"""Python adapter for the reference Sentinel Node VM runner."""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .bundle import SentinelBundleError, sentinel_version, validate_runtime_bundle

_LOGGER = logging.getLogger(__name__)


class SentinelRunnerError(RuntimeError):
    """Stable failure raised by the Node runner adapter."""


class _WorkerTransportError(RuntimeError):
    """The persistent worker is gone, wedged or timed out.

    Distinct from :class:`SentinelRunnerError`: a runner *verdict* (bad
    challenge, incomplete token) must not be retried through a cold start, while
    a transport failure means the answer is unknown and the one-shot path is a
    safe fallback.
    """


#: Environment switch. Default off: Sentinel is a risk-control path, so the
#: worker is opt-in until a controlled live comparison backs it (this repo's
#: convention for every new behaviour in this lane).
WORKER_ENV = "SMS_SENTINEL_WORKER"


def worker_enabled() -> bool:
    return str(os.environ.get(WORKER_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _runner_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "SENTINEL_COOKIE",
        "CHATGPT_COOKIE",
        "SENTINEL_AUTHORIZATION",
        "CHATGPT_BEARER_TOKEN",
        "SENTINEL_HEADERS_JSON",
    ):
        env.pop(key, None)
    return env


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class _NodeWorker:
    """One persistent Node process serving token requests over stdin/stdout."""

    def __init__(self, node_binary: str, worker_path: Path, runner_path: Path) -> None:
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._next_id = 0
        self._process = subprocess.Popen(
            [node_binary, str(worker_path), str(runner_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(worker_path.parent),
            env=_runner_env(),
            creationflags=_creation_flags(),
        )
        threading.Thread(target=self._read_stdout, name="sentinel-worker-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="sentinel-worker-stderr", daemon=True).start()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        try:
            if stream is not None:
                for line in stream:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        message = json.loads(text)
                    except ValueError:
                        continue  # non-JSON noise must not wedge the waiter
                    if isinstance(message, dict):
                        self._responses.put(message)
        finally:
            self._responses.put({"__closed__": True})

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                text = line.strip()
                if text:
                    self._stderr_tail.append(text)
        except Exception:  # noqa: BLE001 - a dying pipe is not actionable here
            pass

    def run(self, args: list[str], *, timeout: float) -> str:
        # A wedged worker holds the request lock; waiting forever here would
        # turn one bad token into a stalled batch, so the acquire itself is
        # bounded by the same timeout.
        if not self._lock.acquire(timeout=max(1.0, timeout)):
            raise _WorkerTransportError("worker busy")
        try:
            if not self.alive:
                raise _WorkerTransportError("worker exited")
            self._next_id += 1
            request_id = self._next_id
            try:
                stdin = self._process.stdin
                if stdin is None:
                    raise OSError("worker stdin is closed")
                stdin.write(json.dumps({"id": request_id, "args": list(args)}) + "\n")
                stdin.flush()
            except (OSError, ValueError) as exc:
                raise _WorkerTransportError(f"worker write failed: {exc}") from exc
            deadline = time.monotonic() + max(1.0, timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _WorkerTransportError("worker timeout")
                try:
                    message = self._responses.get(timeout=remaining)
                except queue.Empty:
                    raise _WorkerTransportError("worker timeout") from None
                if message.get("__closed__"):
                    raise _WorkerTransportError("worker closed stdout")
                if message.get("id") != request_id:
                    continue
                if message.get("ok"):
                    return str(message.get("text") or "")
                raise SentinelRunnerError(f"sentinel_runner_failed:worker:{_safe_error(message.get('error'))}")
        finally:
            self._lock.release()

    def failure_detail(self) -> str:
        return " | ".join(self._stderr_tail)[:500]

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        except OSError:
            pass
        finally:
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


_WORKER: _NodeWorker | None = None
_WORKER_KEY: tuple[str, str, str] | None = None
_WORKER_LOCK = threading.Lock()


def _worker_path() -> Path:
    return Path(__file__).with_name("runtime") / "sentinel-worker.js"


def _worker_for(node_binary: str, runner_path: Path) -> _NodeWorker | None:
    """Return the shared worker, starting it on first use.

    A worker that cannot start is not an error: the caller falls back to the
    one-shot node run, which is the pre-existing path.
    """
    global _WORKER, _WORKER_KEY
    path = _worker_path()
    if not path.is_file():
        return None
    key = (node_binary, str(path), str(runner_path))
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER_KEY == key and _WORKER.alive:
            return _WORKER
        if _WORKER is not None:
            _WORKER.close()
            _WORKER = None
            _WORKER_KEY = None
        try:
            _WORKER = _NodeWorker(node_binary, path, runner_path)
        except (OSError, ValueError) as exc:
            _LOGGER.warning("Sentinel worker could not start (%s); using one-shot node runs", exc)
            return None
        _WORKER_KEY = key
        return _WORKER


def _drop_worker() -> None:
    global _WORKER, _WORKER_KEY
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.close()
        _WORKER = None
        _WORKER_KEY = None


atexit.register(_drop_worker)


def _impersonate_family(impersonate: str) -> str:
    """Classify an impersonate token as 'firefox' / 'chrome' / 'safari' / 'edge'."""
    name = str(impersonate or "").lower()
    if name.startswith("firefox"):
        return "firefox"
    if name.startswith("chrome"):
        return "chrome"
    if name.startswith("safari"):
        return "safari"
    if name.startswith("edge"):
        return "edge"
    return "chrome"


def _impersonate_version(impersonate: str) -> str:
    """Extract the numeric major version from an impersonate token (e.g. '144')."""
    match = re.match(r"^[a-z]+(\d+)", str(impersonate or "").lower())
    return match.group(1) if match else ""


def _platform_for_family(family: str, profile: Mapping[str, Any]) -> str:
    """Return the ``userAgentData.platform`` / ``sec-ch-ua-platform`` value.

    The profile already carries the canonical ``sec_ch_ua_platform``
    (``"Windows"`` / ``"macOS"`` / ``"iOS"``, from
    ``auth_headers._HARDWARE_PROFILES``).  These two runner options used to be
    hardcoded to ``"Windows"``, so a Safari/iOS profile reported macOS/iOS in
    its UA while the JS environment claimed a Windows platform -- a free
    contradiction.  Prefer the declared value; fall back to the OS implied by
    the impersonate token so a caller that omits the field still cannot pair a
    macOS UA with a Windows platform.
    """
    declared = str(profile.get("sec_ch_ua_platform") or "").strip().strip('"')
    if declared:
        return declared
    impersonate = str(profile.get("impersonate") or "").lower()
    if impersonate.endswith("_ios"):
        return "iOS"
    if impersonate.endswith("_android"):
        return "Android"
    if family == "safari":
        return "macOS"
    return "Windows"


def _node_binary() -> str:
    configured = (
        str(os.getenv("OPENAI_SENTINEL_NODE_PATH") or "").strip() or str(os.getenv("NODE_EXECUTABLE") or "").strip()
    )
    return configured or ("node.exe" if os.name == "nt" else "node")


def check_node_runner_readiness() -> None:
    """Validate local runner prerequisites without executing Node or using an account.

    Only fixed reason codes cross this boundary: filesystem and configured
    executable errors may otherwise contain sensitive local paths.
    """
    try:
        validate_runtime_bundle()
    except (SentinelBundleError, OSError) as exc:
        if isinstance(exc, OSError):
            reason = "sentinel_runtime_unavailable"
        else:
            message = str(exc)
            if message.startswith("sentinel_runtime_missing:"):
                reason = "sentinel_runtime_missing"
            elif message.startswith("sentinel_runtime_hash_mismatch:"):
                reason = "sentinel_runtime_hash_mismatch"
            else:
                reason = "sentinel_runtime_invalid"
        raise SentinelRunnerError(reason) from None
    try:
        available = shutil.which(_node_binary())
    except (OSError, ValueError):
        available = None
    if not available:
        raise SentinelRunnerError("sentinel_runner_node_missing")


def _safe_error(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:500]


def _as_int(value: Any, default: int) -> int:
    """Coerce a profile field to int, falling back on anything malformed.

    ``profile`` is a ``Mapping[str, Any]`` built by several callers, so a
    non-numeric seed (or a caller that passes ``"eight"``) must not crash
    issuance with a bare ``int()`` -- the runner degrades to the documented
    default instead.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """Coerce a profile field to float, falling back on anything malformed."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _run_runner_once(command: list[str], runner_path: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run the pinned runner once (the pre-existing one-shot transport)."""
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=str(runner_path.parent),
            env=_runner_env(),
            creationflags=_creation_flags(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise SentinelRunnerError("sentinel_runner_node_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise SentinelRunnerError("sentinel_runner_timeout") from exc


def run_sentinel_sdk(
    challenge: Mapping[str, Any],
    *,
    flow: str,
    device_id: str,
    profile: Mapping[str, Any],
    cookie: str,
    page_url: str,
    timeout_seconds: int = 60,
    verify_bundle_hash: bool = True,
    challenge_proof: str = "",
) -> str:
    """Run the SDK against one server challenge and return its JSON token."""
    flow = str(flow or "").strip()
    device_id = str(device_id or "").strip()
    if not flow:
        raise SentinelRunnerError("sentinel_runner_invalid_flow")
    if not device_id:
        raise SentinelRunnerError("sentinel_runner_missing_device_id")
    if not isinstance(challenge, Mapping) or not str(challenge.get("token") or "").strip():
        raise SentinelRunnerError("sentinel_runner_invalid_challenge")
    # 🔴 turnstile.dx 与取 challenge 的那份 requirements p 绑定（见 runner.js
    # ``handleIframeMessage`` 注释）。不带这份 proof 时，VM 内 SDK 会拿它
    # 重新采样的另一份 p 去 dx 解码 —— 与服务器加密用的密钥不一致。
    challenge_proof = str(challenge_proof or "").strip()

    sdk_path, runner_path = validate_runtime_bundle(verify_hash=verify_bundle_hash)
    screen = str(profile.get("screen") or "1920x1080")
    width, _, height = screen.partition("x")
    language = str(profile.get("lang") or "en-US")
    languages = [
        item.split(";", 1)[0].strip()
        for item in str(profile.get("lang_full") or language).split(",")
        if item.split(";", 1)[0].strip()
    ]
    impersonate = str(profile.get("impersonate") or "chrome136")
    family = _impersonate_family(impersonate)
    major = _impersonate_version(impersonate) or "136"
    user_agent = str(profile.get("user_agent") or "Mozilla/5.0")
    # Platform-class fields must agree with the profile's real OS (see
    # ``_platform_for_family``).  ``maxTouchPoints`` travels with them so an
    # iOS profile is not pinned to a 0-touch desktop.
    platform = _platform_for_family(family, profile)
    chrome_full = user_agent.split("Chrome/", 1)[1].split(" ", 1)[0] if "Chrome/" in user_agent else f"{major}.0.0.0"
    config = {
        "flow": flow,
        "deviceId": device_id,
        "sentinelSid": str(profile.get("session_id") or ""),
        "pageUrl": str(page_url or ""),
        "scriptSrc": str(
            profile.get("script_src") or f"https://sentinel.openai.com/sentinel/{sentinel_version()}/sdk.js"
        ),
        "cookie": str(cookie or f"oai-did={device_id}"),
        "userAgent": user_agent,
        "browserFamily": family,
        "navigatorPlatform": str(profile.get("navigator_platform") or "Win32"),
        "navigatorVendor": "" if family == "firefox" else str(profile.get("navigator_vendor") or "Google Inc."),
        "userAgentDataPlatform": platform,
        "maxTouchPoints": _as_int(profile.get("max_touch_points"), 0),
        "requestIdleCallback": True,
        "language": language,
        "languages": languages or [language],
        "timeZone": str(profile.get("timezone") or "UTC"),
        "timezoneName": str(profile.get("timezone_name") or profile.get("timezone") or "UTC"),
        "timezoneOffsetMinutes": _as_int(profile.get("timezone_offset_minutes"), 0),
        "hardwareConcurrency": _as_int(profile.get("hardware_concurrency"), 8),
        "jsHeapSizeLimit": _as_int(profile.get("js_heap_size_limit"), 4_395_630_592),
        "deviceMemory": _as_int(profile.get("device_memory"), 8),
        "devicePixelRatio": _as_float(profile.get("device_pixel_ratio"), 1.0),
        "chromeMajor": major,
        "chromeFullVersion": chrome_full,
        "secChUa": str(profile.get("sec_ch_ua") or ""),
        "secChUaPlatform": platform,
        "secChUaFullVersionList": str(profile.get("sec_ch_ua_full_version_list") or ""),
        "secChUaPlatformVersion": str(profile.get("sec_ch_ua_platform_version") or "10.0.0"),
        "secChUaArch": str(profile.get("sec_ch_ua_arch") or "x86"),
        "secChUaBitness": str(profile.get("sec_ch_ua_bitness") or "64"),
        "secChUaModel": str(profile.get("sec_ch_ua_model") or ""),
        "width": _as_int(width, 1920),
        "height": _as_int(height, 1080),
        "sdkPath": str(sdk_path),
    }

    with tempfile.TemporaryDirectory(prefix="sms-tool-sentinel-") as tmp:
        tmp_dir = Path(tmp)
        challenge_path = tmp_dir / "challenge.json"
        config_path = tmp_dir / "runner.json"
        challenge_path.write_text(json.dumps(dict(challenge), ensure_ascii=False), encoding="utf-8")
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        command = [
            _node_binary(),
            str(runner_path),
            "--config",
            str(config_path),
            "--challenge-file",
            str(challenge_path),
        ]
        if challenge_proof:
            command.extend(["--challenge-proof", challenge_proof])
        # Everything after the interpreter and script path; the worker receives
        # the same arguments and rebuilds the same one-shot command.
        runner_args = command[2:]
        timeout = max(10, min(int(timeout_seconds or 60), 120))
        token = ""
        if worker_enabled():
            worker = _worker_for(_node_binary(), runner_path)
            if worker is not None:
                try:
                    token = worker.run(runner_args, timeout=timeout)
                except _WorkerTransportError as exc:
                    _LOGGER.warning(
                        "Sentinel worker transport failed (%s: %s); falling back to a one-shot node run",
                        exc,
                        _safe_error(worker.failure_detail()),
                    )
                    _drop_worker()
        if not token:
            completed = _run_runner_once(command, runner_path, timeout)
            if completed.returncode != 0:
                raise SentinelRunnerError(
                    f"sentinel_runner_failed:{completed.returncode}:{_safe_error(completed.stderr or completed.stdout)}"
                )
            token = str(completed.stdout or "").strip()

    if not token:
        raise SentinelRunnerError("sentinel_runner_empty_output")
    try:
        parsed = json.loads(token)
    except (TypeError, ValueError) as exc:
        raise SentinelRunnerError("sentinel_runner_invalid_json") from exc
    if not isinstance(parsed, dict):
        raise SentinelRunnerError("sentinel_runner_invalid_payload")
    if str(parsed.get("id") or "") != device_id:
        raise SentinelRunnerError("sentinel_runner_device_mismatch")
    if str(parsed.get("flow") or "") != flow:
        raise SentinelRunnerError("sentinel_runner_flow_mismatch")
    if "p" not in parsed or "t" not in parsed or not str(parsed.get("c") or "").strip():
        raise SentinelRunnerError("sentinel_runner_incomplete_token")
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


__all__ = ["SentinelRunnerError", "check_node_runner_readiness", "run_sentinel_sdk", "worker_enabled"]
