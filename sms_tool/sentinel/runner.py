"""Python adapter for the compiled Rust Sentinel VM (`sentinel_vm` FFI cdylib)."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .bundle import SentinelBundleError, sentinel_version, validate_runtime_bundle
from .native_ffi import SentinelNativeError, native_version, solve_challenge


class SentinelRunnerError(RuntimeError):
    """Stable failure raised by the Sentinel native VM runner adapter."""


def worker_enabled() -> bool:
    """In-process Rust FFI needs no background worker process."""
    return False


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
    """Return the ``userAgentData.platform`` / ``sec-ch-ua-platform`` value."""
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


def _safe_error(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:500]


def _as_int(value: Any, default: int) -> int:
    """Coerce a profile field to int, falling back on anything malformed."""
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


def check_sentinel_runner_readiness() -> None:
    """Validate the compiled Rust Sentinel VM library without using an account.

    Only fixed reason codes cross this boundary: filesystem and loader errors
    may otherwise contain sensitive local paths.
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
        version = native_version(verify_hash=False)
    except (SentinelBundleError, SentinelNativeError, OSError):
        raise SentinelRunnerError("sentinel_native_load_failed") from None
    if not str(version or "").strip():
        raise SentinelRunnerError("sentinel_native_load_failed")


#: Backward-compatible alias kept for callers/tests referencing the legacy name.
check_node_runner_readiness = check_sentinel_runner_readiness


def _build_native_opts(
    *,
    flow: str,
    device_id: str,
    profile: Mapping[str, Any],
    cookie: str,
    page_url: str,
    challenge_proof: str,
) -> dict[str, Any]:
    screen = str(profile.get("screen") or "1920x1080")
    width_raw, _, height_raw = screen.partition("x")
    width = _as_int(width_raw, 1920)
    height = _as_int(height_raw, 1080)
    language = str(profile.get("lang") or "en-US")
    languages = [
        item.split(";", 1)[0].strip()
        for item in str(profile.get("lang_full") or language).split(",")
        if item.split(";", 1)[0].strip()
    ]
    impersonate = str(profile.get("impersonate") or "firefox144")
    family = _impersonate_family(impersonate)
    major = _impersonate_version(impersonate) or "144"
    user_agent = str(profile.get("user_agent") or "Mozilla/5.0")
    platform = _platform_for_family(family, profile)
    chrome_full = str(profile.get("full_version") or "").strip() or (
        user_agent.split("Chrome/", 1)[1].split(" ", 1)[0] if "Chrome/" in user_agent else f"{major}.0.0.0"
    )
    return {
        "flow": flow,
        "device_id": device_id,
        "challenge_proof": challenge_proof,
        "session_id": str(profile.get("session_id") or ""),
        "page_url": str(page_url or ""),
        "script_src": str(
            profile.get("script_src") or f"https://sentinel.openai.com/sentinel/{sentinel_version()}/sdk.js"
        ),
        "cookie": str(cookie or f"oai-did={device_id}"),
        "user_agent": user_agent,
        "impersonate": impersonate,
        "browser_family": family,
        "navigator_platform": str(profile.get("navigator_platform") or "Win32"),
        "navigator_vendor": "" if family == "firefox" else str(profile.get("navigator_vendor") or "Google Inc."),
        "max_touch_points": _as_int(profile.get("max_touch_points"), 0),
        "lang": language,
        "languages": languages or [language],
        "timezone": str(profile.get("timezone") or "UTC"),
        "timezone_name": str(profile.get("timezone_name") or profile.get("timezone") or "UTC"),
        "timezone_offset_minutes": _as_int(profile.get("timezone_offset_minutes"), 0),
        "time_origin": _as_float(profile.get("time_origin"), 0.0),
        "hardware_concurrency": _as_int(profile.get("hardware_concurrency"), 8),
        "js_heap_size_limit": _as_int(profile.get("js_heap_size_limit"), 4_395_630_592),
        "device_memory": _as_int(profile.get("device_memory"), 8),
        "device_pixel_ratio": _as_float(profile.get("device_pixel_ratio"), 1.0),
        "chrome_major": major,
        "chrome_full_version": chrome_full,
        "sec_ch_ua": str(profile.get("sec_ch_ua") or ""),
        "sec_ch_ua_platform": platform,
        "sec_ch_ua_full_version_list": str(profile.get("sec_ch_ua_full_version_list") or ""),
        "sec_ch_ua_platform_version": str(profile.get("sec_ch_ua_platform_version") or "10.0.0"),
        "sec_ch_ua_arch": str(profile.get("sec_ch_ua_arch") or "x86"),
        "sec_ch_ua_bitness": str(profile.get("sec_ch_ua_bitness") or "64"),
        "sec_ch_ua_model": str(profile.get("sec_ch_ua_model") or ""),
        "sec_ch_ua_mobile": str(profile.get("sec_ch_ua_mobile") or ("?1" if platform in {"iOS", "Android"} else "?0")),
        "screen_width": width,
        "screen_height": height,
    }


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
    """Solve one server Sentinel challenge in-process via the Rust VM cdylib."""
    _ = timeout_seconds  # kept in signature for caller compatibility
    flow = str(flow or "").strip()
    device_id = str(device_id or "").strip()
    if not flow:
        raise SentinelRunnerError("sentinel_runner_invalid_flow")
    if not device_id:
        raise SentinelRunnerError("sentinel_runner_missing_device_id")
    if not isinstance(challenge, Mapping) or not str(challenge.get("token") or "").strip():
        raise SentinelRunnerError("sentinel_runner_invalid_challenge")

    opts = _build_native_opts(
        flow=flow,
        device_id=device_id,
        profile=profile,
        cookie=cookie,
        page_url=page_url,
        challenge_proof=str(challenge_proof or "").strip(),
    )
    try:
        token = solve_challenge(challenge, opts, verify_hash=verify_bundle_hash)
    except (SentinelBundleError, SentinelNativeError) as exc:
        raise SentinelRunnerError(f"sentinel_runner_failed:native:{_safe_error(exc)}") from exc

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


__all__ = [
    "SentinelRunnerError",
    "check_node_runner_readiness",
    "check_sentinel_runner_readiness",
    "run_sentinel_sdk",
    "worker_enabled",
]
