"""Environment self-check ("doctor") shared by CLI, installer and desktop.

The dependency checks below previously lived only in ``scripts/preflight_env.py``
(a manual README step), so a fresh install had no single command to answer
"why does this machine fail".  ``python -m sms_tool --doctor`` aggregates them
plus config completeness into one offline report; ``--doctor --json`` emits the
same report machine-readably for the WPF first-launch probe and the installer's
post-install verification.

Statuses: ``ok`` / ``warn`` (optional dependency or non-blocking config gap) /
``fail`` (hard requirement missing).  Exit code = number of failed checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any


def _check(name: str, status: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _probe_python() -> dict[str, str]:
    version = sys.version_info
    detail = f"{sys.version.split()[0]} ({sys.executable})"
    if version < (3, 10):
        return _check("python", "fail", detail, "Python 3.10+ is required")
    return _check("python", "ok", detail)


def _probe_node() -> dict[str, str]:
    binary = os.environ.get("OPENAI_SENTINEL_NODE_PATH") or shutil.which("node")
    if not binary:
        return _check(
            "node", "fail", "not found on PATH",
            "install Node.js LTS (Sentinel token extraction shells out to node)",
        )
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15
        )
        version = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 - any launch failure is the finding
        return _check("node", "fail", binary, f"node exists but failed to run: {type(exc).__name__}")
    if proc.returncode != 0:
        return _check("node", "fail", binary, f"node --version exited {proc.returncode}")
    return _check("node", "ok", f"{version} ({binary})")


def _probe_playwright() -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _check(
            "playwright", "fail", "package not installed",
            "python -m pip install playwright",
        )
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
    except Exception as exc:  # noqa: BLE001
        return _check("playwright", "fail", str(exc)[:160], "python -m playwright install chromium")
    if path and os.path.isfile(path):
        return _check("playwright", "ok", path)
    return _check(
        "playwright", "fail", f"chromium executable missing: {path}",
        "python -m playwright install chromium",
    )


def _probe_curl_cffi() -> dict[str, str]:
    try:
        import curl_cffi
    except Exception:
        return _check(
            "curl_cffi", "fail", "package not installed",
            "python -m pip install 'curl_cffi>=0.15.0,<0.17'",
        )
    version = str(getattr(curl_cffi, "__version__", "") or "")
    ok_version = version.startswith(("0.15.", "0.16."))
    if not ok_version:
        return _check(
            "curl_cffi", "fail", f"version {version or 'unknown'}",
            "registration requires curl_cffi 0.15.x or 0.16.x (chrome146 TLS profile)",
        )
    try:
        from curl_cffi.requests.impersonate import BrowserType  # type: ignore

        profile_ok = True
    except Exception:
        try:
            from curl_cffi import BrowserType  # type: ignore

            profile_ok = True
        except Exception:
            profile_ok = False
    if not profile_ok:
        return _check(
            "curl_cffi", "fail", f"version {version} lacks the impersonation profile API",
            "reinstall curl_cffi 0.15.x/0.16.x",
        )
    return _check("curl_cffi", "ok", f"{version} (impersonation profiles available)")


def _probe_import(name: str, display: str, purpose: str, *, required: bool) -> dict[str, str]:
    try:
        __import__(name)
        return _check(display, "ok", "installed")
    except Exception:
        status = "fail" if required else "warn"
        hint = f"python -m pip install {name}" + ("" if required else f"  (only needed for {purpose})")
        return _check(display, status, "not installed", hint)


def _probe_requests() -> dict[str, str]:
    return _probe_import("requests", "requests", "", required=True)


def _probe_config(config: Mapping[str, Any], config_source: str) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    package_dir = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.normcase(os.path.join(package_dir, "config.json"))
    using_fallback = config_source and os.path.normcase(str(config_source)) == bundled
    checks.append(_check(
        "config_source",
        "warn" if using_fallback else "ok",
        str(config_source or "unknown"),
        "" if not using_fallback else
        "project-root config.json is missing; create one from config.example.json for full behavior",
    ))

    proxy_cfg = config.get("proxy") if isinstance(config.get("proxy"), Mapping) else {}
    pool = proxy_cfg.get("pool") or []
    if str(proxy_cfg.get("default") or "").strip() or pool:
        checks.append(_check("config_proxy", "ok", "proxy.default or proxy.pool configured"))
    else:
        checks.append(_check(
            "config_proxy", "warn", "no proxy configured",
            "registration/payment flows need proxy.default or proxy.pool",
        ))

    email_cfg = config.get("email_registration") if isinstance(config.get("email_registration"), Mapping) else {}
    token_file = str(email_cfg.get("token_file") or "").strip()
    mailbox_sources: list[str] = []
    if token_file and os.path.isfile(token_file):
        mailbox_sources.append(f"token_file:{token_file}")
    for provider in ("remail", "smailr"):
        provider_cfg = email_cfg.get(provider) if isinstance(email_cfg.get(provider), Mapping) else {}
        if provider_cfg.get("enabled") and str(provider_cfg.get("api_key") or "").strip():
            mailbox_sources.append(provider)
    cfworker_url = str(email_cfg.get("cfworker_url") or "").strip()
    if cfworker_url:
        mailbox_sources.append("cfworker")
    if mailbox_sources:
        checks.append(_check("config_mailbox", "ok", ", ".join(mailbox_sources)))
    else:
        checks.append(_check(
            "config_mailbox", "warn", "no usable mailbox source",
            "configure email_registration.token_file, remail, smailr or cfworker",
        ))
    return checks


def run_doctor(
    config: Mapping[str, Any] | None = None,
    config_source: str = "",
    *,
    probes: Mapping[str, Callable[[], dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Run all checks and return the report dict (offline; no network calls)."""
    default_probes: list[tuple[str, Callable[[], dict[str, str]]]] = [
        ("python", _probe_python),
        ("node", _probe_node),
        ("playwright", _probe_playwright),
        ("curl_cffi", _probe_curl_cffi),
        ("requests", _probe_requests),
        ("pyotp", lambda: _probe_import("pyotp", "pyotp", "TOTP 2FA enrollment", required=False)),
        ("qrcode", lambda: _probe_import("qrcode", "qrcode", "UPI QR rendering", required=False)),
        ("nacl", lambda: _probe_import("nacl", "pynacl", "Agent Identity signing", required=False)),
    ]
    checks: list[dict[str, str]] = []
    for name, probe in default_probes:
        override = (probes or {}).get(name)
        checks.append(override() if override else probe())
    checks.extend(_probe_config(config or {}, config_source))
    failed = sum(1 for item in checks if item["status"] == "fail")
    warned = sum(1 for item in checks if item["status"] == "warn")
    return {
        "ok": failed == 0,
        "failed": failed,
        "warned": warned,
        "checks": checks,
    }


def print_doctor_report(report: Mapping[str, Any]) -> None:
    labels = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    failed = int(report.get("failed") or 0)
    warned = int(report.get("warned") or 0)
    print(f"doctor: {failed} failed, {warned} warning(s)")
    for item in report["checks"]:
        status = str(item.get("status") or "")
        label = labels.get(status, "[ ?? ]")
        name = str(item.get("name") or "")
        detail = str(item.get("detail") or "")
        line = f"{label} {name}"
        if detail:
            line += f": {detail}"
        print(line)
        if item.get("hint"):
            print(f"         -> {item['hint']}")
    if report.get("ok"):
        print("doctor: environment ready")
    else:
        print("doctor: fix the [FAIL] items above before running registration/payment")
