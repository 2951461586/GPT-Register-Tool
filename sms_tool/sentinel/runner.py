"""Python adapter for the reference Sentinel Node VM runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .bundle import SentinelBundleError, sentinel_version, validate_runtime_bundle


class SentinelRunnerError(RuntimeError):
    """Stable failure raised by the Node runner adapter."""


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
        env = dict(os.environ)
        for key in (
            "SENTINEL_COOKIE",
            "CHATGPT_COOKIE",
            "SENTINEL_AUTHORIZATION",
            "CHATGPT_BEARER_TOKEN",
            "SENTINEL_HEADERS_JSON",
        ):
            env.pop(key, None)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=max(10, min(int(timeout_seconds or 60), 120)),
                cwd=str(runner_path.parent),
                env=env,
                creationflags=creationflags,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SentinelRunnerError("sentinel_runner_node_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise SentinelRunnerError("sentinel_runner_timeout") from exc

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


__all__ = ["SentinelRunnerError", "check_node_runner_readiness", "run_sentinel_sdk"]
