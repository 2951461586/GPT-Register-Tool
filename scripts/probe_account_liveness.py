#!/usr/bin/env python3
"""Batch account liveness probe using the desktop app's canonical endpoint.

This script intentionally shares ``probe_account_liveness`` with the WPF
account-liveness action. Both paths call ``/backend-api/wham/usage`` and use
the same headers, account-id handling, proxy, and HTTP result semantics.
No mailbox OTP relogin is attempted.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sms_tool.account_liveness import (
    CODEX_USAGE_URL,
    probe_account_liveness,
    browser_fetch_for_account,
)
from sms_tool.storage import get_account_record


LIVENESS_ENDPOINT = CODEX_USAGE_URL


def load_config_proxy() -> str:
    try:
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    proxy = cfg.get("proxy", {}) if isinstance(cfg.get("proxy"), dict) else {}
    for key in ("registration", "default"):
        value = str(proxy.get(key) or "").strip()
        if value:
            return value
    pool = proxy.get("pool")
    if isinstance(pool, list) and pool:
        return str(pool[0] or "").strip()
    return ""


def latest_session(sessions_dir: str, email: str) -> Path | None:
    matches = glob.glob(os.path.join(sessions_dir, f"session_{email}_*.json"))
    if not matches:
        return None
    return Path(max(matches, key=os.path.getmtime))


def read_account(path: Path) -> tuple[dict[str, Any], float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, 0.0
    if not isinstance(data, dict):
        return {}, 0.0
    try:
        created_at = float(data.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0.0
    return data, created_at


def load_account(email: str, sessions_dir: str) -> tuple[dict[str, Any], float, str | None]:
    account = get_account_record(email)
    if isinstance(account, dict) and str(account.get("access_token") or "").strip():
        try:
            created_at = float(account.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        return account, created_at, None

    session_file = latest_session(sessions_dir, email)
    if session_file is None:
        return {}, 0.0, "no_account"
    account, created_at = read_account(session_file)
    if not str(account.get("access_token") or "").strip():
        return account, created_at, "no_token"
    return account, created_at, None


def probe_account(account: dict[str, Any], proxy: str, timeout: int, browser_fetch=None) -> dict[str, Any]:
    return probe_account_liveness(
        account, proxy=proxy or None, timeout=timeout, browser_fetch=browser_fetch
    )


def classify_probe(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    error = str(result.get("error") or "").strip().lower()
    try:
        status_code = int(result.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0

    if bool(result.get("ok")) and 200 <= status_code < 300:
        return "alive"
    mode = str(result.get("mode") or "")
    if mode == "browser" and not bool(result.get("ok")) and not status_code:
        return "browser_error"
    if status == "account_deactivated" or "account_deactivated" in error or "deactivat" in error:
        return "deactivated"
    if status_code == 401 or status == "token_invalid":
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 429:
        return "rate_limited"
    if status_code:
        return f"http_{status_code}"
    return "error"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch AT liveness probe via /backend-api/wham/usage")
    parser.add_argument("--email-file", default="runtime/at200_emails.txt")
    parser.add_argument("--sessions-dir", default="sessions")
    parser.add_argument("--proxy", default=None, help="Override proxy; defaults to config registration/default proxy")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--browser", action="store_true",
                        help="Route quota probes through a headless browser (camoufox/playwright) "
                             "via browser_fetch_for_account, carrying the account's real fingerprint "
                             "and cookies to bypass Cloudflare TLS-stage resets on protocol-only probes.")
    parser.add_argument("--json-out", default="", help="Write per-account JSON results")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    proxy = args.proxy if args.proxy is not None else load_config_proxy()
    if args.browser:
        safe_workers = max(1, min(args.workers, 2))
        if safe_workers != args.workers:
            print(f"[browser] capping workers {args.workers} -> {safe_workers} to limit concurrent browsers")
        args.workers = safe_workers
    emails = [line.strip() for line in Path(args.email_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not emails:
        print("Account list is empty")
        return 2

    now = time.time()
    tasks: list[tuple[str, dict[str, Any], float, str | None]] = []
    for email in emails:
        account, created_at, preclassified = load_account(email, args.sessions_dir)
        tasks.append((email, account, created_at, preclassified))

    def work(task: tuple[str, dict[str, Any], float, str | None]) -> dict[str, Any]:
        email, account, created_at, preclassified = task
        age = round((now - created_at) / 60, 1) if created_at else None
        if preclassified:
            return {"email": email, "category": preclassified, "status": None, "age_min": age}
        if args.browser:
            # Let the browser manager pick the health_browser lane when no
            # explicit proxy is forced, so the probe carries the account's real
            # fingerprint + cookies (bypasses Cloudflare TLS-stage resets).
            explicit_proxy = proxy if args.proxy is not None else None
            try:
                with browser_fetch_for_account(account, proxy=explicit_proxy, timeout=args.timeout) as browser_fetch:
                    probe = probe_account(account, proxy, args.timeout, browser_fetch=browser_fetch)
            except Exception as exc:
                probe = {
                    "ok": False,
                    "mode": "browser",
                    "status": "unknown",
                    "quota_status": "检测失败",
                    "error": str(exc)[:500],
                    "status_code": 0,
                }
        else:
            probe = probe_account(account, proxy, args.timeout)
        return {
            "email": email,
            "category": classify_probe(probe),
            "status": probe.get("status_code"),
            "age_min": age,
        }

    results: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results.extend(pool.map(work, tasks))

    counts = Counter(row["category"] for row in results)
    total = len(results)
    labels = {
        "alive": "Alive (HTTP 2xx)",
        "unauthorized": "AT invalid (401)",
        "deactivated": "Account deactivated",
        "forbidden": "Forbidden (403)",
        "rate_limited": "Rate limited (429)",
        "error": "Network/probe error",
        "browser_error": "Browser launch/SSL error",
        "no_account": "No SQLite/session account",
        "no_token": "No access token",
    }
    order = ["alive", "unauthorized", "deactivated", "forbidden", "rate_limited", "error", "browser_error", "no_account", "no_token"]

    mode_label = "browser_fetch" if args.browser else "protocol(curl_cffi)"
    print("=" * 56)
    print(f"Account liveness: {total}  endpoint={LIVENESS_ENDPOINT}  mode={mode_label}  proxy={'configured' if proxy else 'direct'}")
    print("=" * 56)
    for key in order + [category for category in counts if category not in order]:
        count = counts.get(key)
        if count:
            print(f"  {labels.get(key, key):30} {count:4}  ({count * 100 // total}%)")
    alive = counts.get("alive", 0)
    print(f"\nAlive: {alive}/{total} = {alive * 100 // total if total else 0}%")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Details: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
