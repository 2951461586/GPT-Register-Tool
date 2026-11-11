"""Offline self-check for the Sentinel runtime (node + vendored bundle + one mint).

Why this exists
---------------
``check_node_runner_readiness()`` verifies that ``node`` exists and that the
vendored ``sdk.js`` / ``sentinel-runner.js`` still hash to their pinned digests.
That catches a corrupt checkout, which is what the 2026-09-17 incident was — but
it does not catch a runner that loads and then fails to mint, and it cannot
notice a Node release that broke a syntax the runner relies on.  Both failures
land at ``create_account``, i.e. after a mailbox and its OTP have been spent, so
the cheapest possible defence is one **offline** mint before the batch starts.

This module performs three checks, in the order that makes a failure
attributable:

1. **bundle** — the pinned SHA256 of the vendored assets (line-ending normalised).
2. **node** — the binary exists and reports a supported major version.
3. **mint** — a real ``sentinel-runner.js`` execution against a synthetic
   challenge, asserted to produce ``{id, flow, c, p, t}``.  No network: the
   challenge is fabricated locally, exactly like the offline unit test.

Usage::

    python -m sms_tool.sentinel.selfcheck          # human-readable
    python -m sms_tool.sentinel.selfcheck --json   # machine-readable

It is also wired into ``sms_tool.doctor`` as the ``sentinel_runtime`` probe, so
``--doctor`` covers it (the startup surface the desktop app already calls).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

from ..auth_headers import set_auth_fingerprint, set_fingerprint_device, set_fingerprint_geo, sentinel_fingerprint
from ..endpoints import AUTH_EMAIL_VERIFICATION
from .bundle import validate_runtime_bundle
from .runner import SentinelRunnerError, run_sentinel_sdk

#: Synthetic challenge: no PoW, no Turnstile, no SO requirement.  The runner
#: only needs ``token`` to bind the proof, so this exercises the real code path
#: (SDK load, VM execution, JSON assembly) without a server round-trip.
OFFLINE_CHALLENGE: dict[str, Any] = {
    "token": "selfcheck-challenge",
    "proofofwork": {"required": False},
    "turnstile": {"required": False},
    "so": {"required": False},
}

OFFLINE_FLOW = "authorize_continue"
OFFLINE_DEVICE_ID = "00000000-0000-4000-8000-000000000000"

#: The oldest LTS line the vendored runner is exercised against.  Node 18 is
#: end-of-life upstream; the runner uses ``node:vm``, ``crypto.randomUUID`` and
#: optional chaining, all present since 16, so 18 is a safe floor.
MIN_NODE_MAJOR = 18


class SentinelSelfCheckError(RuntimeError):
    """Stable failure raised by the self-check (never carries a secret)."""


def _offline_profile() -> dict[str, Any]:
    set_auth_fingerprint("chrome146")
    set_fingerprint_geo("US")
    set_fingerprint_device(OFFLINE_DEVICE_ID)
    return dict(sentinel_fingerprint())


def node_version(binary: str = "") -> str:
    """``node --version`` output, or raise :class:`SentinelSelfCheckError`."""
    executable = binary or shutil.which("node") or ""
    if not executable:
        raise SentinelSelfCheckError("sentinel_selfcheck_node_missing")
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_node_launch:{type(exc).__name__}") from exc
    version = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not version:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_node_version:{completed.returncode}")
    return version


def check_node_major(version: str) -> int:
    """Parse ``v22.20.0`` -> ``22`` and reject an unsupported major."""
    digits = version.lstrip("v").split(".")[0]
    try:
        major = int(digits)
    except ValueError as exc:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_node_unparsable:{version[:16]}") from exc
    if major < MIN_NODE_MAJOR:
        raise SentinelSelfCheckError(f"sentinel_selfcheck_node_too_old:{major}")
    return major


def mint_offline(*, timeout: int = 60, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the real runner once against :data:`OFFLINE_CHALLENGE`.

    Returns the decoded token.  Raises :class:`SentinelSelfCheckError` with a
    bounded, secret-free reason on any failure.
    """
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
    # ``p`` / ``t`` are legitimately ``null`` when the challenge says the work is
    # not required (this offline challenge does), so presence — not truthiness —
    # is what proves the runner assembled the token instead of short-circuiting.
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
    """Run every check; return a report instead of raising.

    ``{"ok": bool, "bundle": str, "node": str, "mint": str}`` — each value is
    ``"ok"`` or a stable reason token.  ``mint=False`` keeps the check cheap for
    callers that only want the local integrity half.
    """
    report: dict[str, Any] = {"ok": True, "bundle": "", "node": "", "mint": ""}
    try:
        validate_runtime_bundle()
        report["bundle"] = "ok"
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["bundle"] = f"failed:{type(exc).__name__}"
        report["ok"] = False
    try:
        version = node_version()
        check_node_major(version)
        report["node"] = version
    except SentinelSelfCheckError as exc:
        report["node"] = str(exc)
        report["ok"] = False
    if not mint:
        report["mint"] = "skipped"
        return report
    if report["bundle"] != "ok" or not str(report["node"]).startswith("v"):
        # A mint without a trustworthy bundle proves nothing; say so instead of
        # reporting a second, less specific failure.
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
        print(f"  node   : {report['node']}")
        print(f"  mint   : {report['mint']}")
        print(f"  result : {'ok' if report['ok'] else 'FAILED'}")
    return 0 if report["ok"] else 1


__all__ = [
    "MIN_NODE_MAJOR",
    "OFFLINE_CHALLENGE",
    "SentinelSelfCheckError",
    "check_node_major",
    "mint_offline",
    "node_version",
    "run_selfcheck",
]


if __name__ == "__main__":
    raise SystemExit(main())
