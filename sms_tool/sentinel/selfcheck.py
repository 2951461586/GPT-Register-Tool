"""Offline self-check for the Rust Sentinel VM FFI runtime (bundle + native + mint).

Performs three offline checks without Node.js or network access:
1. **bundle** — verifies the pinned SHA256 of ``sms_tool/sentinel/runtime/sentinel_vm.dll``.
2. **native** — loads the cdylib via ``ctypes`` and queries ``srs_version()``.
3. **mint** — runs ``run_sentinel_sdk()`` against a synthetic offline challenge
   and asserts ``{id, flow, c, p, t}``.

Usage::

    python -m sms_tool.sentinel.selfcheck          # human-readable
    python -m sms_tool.sentinel.selfcheck --json   # machine-readable
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..transport.auth_headers import (
    sentinel_fingerprint,
    set_auth_fingerprint,
    set_fingerprint_device,
    set_fingerprint_geo,
)
from ..transport.endpoints import AUTH_EMAIL_VERIFICATION
from .bundle import validate_runtime_bundle
from .native_ffi import SentinelNativeError, native_version
from .runner import SentinelRunnerError, run_sentinel_sdk

OFFLINE_CHALLENGE: dict[str, Any] = {
    "token": "selfcheck-challenge",
    "proofofwork": {"required": False},
    "turnstile": {"required": False},
    "so": {"required": False},
}

OFFLINE_FLOW = "authorize_continue"
OFFLINE_DEVICE_ID = "00000000-0000-4000-8000-000000000000"


class SentinelSelfCheckError(RuntimeError):
    """Stable failure raised by the self-check (never carries a secret)."""


def _offline_profile() -> dict[str, Any]:
    set_auth_fingerprint("chrome146")
    set_fingerprint_geo("US")
    set_fingerprint_device(OFFLINE_DEVICE_ID)
    return dict(sentinel_fingerprint())


def native_runtime_version() -> str:
    """Return ``v<crate-version>`` from the compiled Rust Sentinel VM library."""
    try:
        version = native_version(verify_hash=False)
    except (SentinelNativeError, OSError, RuntimeError) as exc:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_native:{type(exc).__name__}") from exc
    cleaned = str(version or "").strip()
    if not cleaned:
        raise SentinelSelfCheckError("sentinel_selfcheck_native_empty")
    return cleaned if cleaned.startswith("v") else f"v{cleaned}"


def mint_offline(*, timeout: int = 60, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the native Rust VM runner once against :data:`OFFLINE_CHALLENGE`."""
    active_profile = profile or _offline_profile()
    try:
        token = run_sentinel_sdk(
            dict(OFFLINE_CHALLENGE),
            flow=OFFLINE_FLOW,
            device_id=OFFLINE_DEVICE_ID,
            profile=active_profile,
            cookie=f"oai-did={OFFLINE_DEVICE_ID}",
            page_url=AUTH_EMAIL_VERIFICATION,
            timeout_seconds=timeout,
        )
    except SentinelRunnerError as exc:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_mint:{str(exc)[:120]}") from exc
    try:
        parsed = json.loads(token)
    except (TypeError, ValueError) as exc:
        raise SentinelSelfCheckError("sentinel_selfcheck_mint:not_json") from exc
    if not isinstance(parsed, dict):
        raise SentinelSelfCheckError("sentinel_selfcheck_mint:not_an_object")
    missing = [key for key in ("id", "flow", "c", "p", "t") if key not in parsed]
    if missing:
        raise SentinelSelfCheckError("sentinel_selfcheck_mint:missing_" + ",".join(missing))
    for key in ("id", "c"):
        if not str(parsed.get(key) or "").strip():
            raise SentinelSelfCheckError(f"sentinel_selfcheck_mint:empty_{key}")
    if str(parsed.get("flow")) != OFFLINE_FLOW:
        raise SentinelSelfCheckError("sentinel_selfcheck_mint:flow_mismatch")
    return parsed


def run_selfcheck(*, mint: bool = True, timeout: int = 60) -> dict[str, Any]:
    """Run every check; return ``{"ok": bool, "bundle": str, "native": str, "mint": str}``."""
    report: dict[str, Any] = {"ok": True, "bundle": "", "native": "", "mint": ""}
    try:
        validate_runtime_bundle()
        report["bundle"] = "ok"
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["bundle"] = f"failed:{type(exc).__name__}"
        report["ok"] = False
    if report["bundle"] == "ok":
        try:
            report["native"] = native_runtime_version()
        except SentinelSelfCheckError as exc:
            report["native"] = str(exc)
            report["ok"] = False
    else:
        report["native"] = "skipped"
    if not mint:
        report["mint"] = "skipped"
        return report
    if report["bundle"] != "ok" or not str(report["native"]).startswith("v"):
        report["mint"] = "skipped"
        return report
    try:
        mint_offline(timeout=timeout)
        report["mint"] = "ok"
    except SentinelSelfCheckError as exc:
        report["mint"] = str(exc)
        report["ok"] = False
    return report


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    report = run_selfcheck(mint="--no-mint" not in args)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("sentinel runtime self-check")
        print(f"  bundle : {report['bundle']}")
        print(f"  native : {report['native']}")
        print(f"  mint   : {report['mint']}")
        print(f"  result : {'ok' if report['ok'] else 'FAILED'}")
    return 0 if report["ok"] else 1


__all__ = [
    "OFFLINE_CHALLENGE",
    "SentinelSelfCheckError",
    "mint_offline",
    "native_runtime_version",
    "run_selfcheck",
]


if __name__ == "__main__":
    raise SystemExit(main())
