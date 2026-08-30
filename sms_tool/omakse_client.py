#!/usr/bin/env python3
"""Omakse API client — link extraction + US PayPal protocol payment.

Integrates with the omakse server (http://oai.omakse.xyz) for:
  1. Link extraction: POST /api/link-extract/jobs → poll until terminal
  2. US protocol payment: POST /api/paypal-us-protocol/run → poll status

Usage from CLI:
  python -m sms_tool --omakse-extract --at <TOKEN> --us-proxy <PROXY>
  python -m sms_tool --omakse-extract --email <EMAIL> --us-proxy <PROXY>
  python -m sms_tool --omakse-us-pay --ba-token <BA-TOKEN> --proxy <PROXY>

Usage from Python:
  from sms_tool.omakse_client import extract_links, run_us_payment
  result = extract_links(credentials="...", us_proxies="...", promotion_proxies="...")
  result = run_us_payment(ba_token="BA-...", proxy="http://...")
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULT_BASE_URL = "http://oai.omakse.xyz"
DEFAULT_TIMEOUT = 30
POLL_INTERVAL = 1.5          # seconds between status polls
MAX_POLL_DURATION = 300      # 5 minutes default
TERMINAL_STATES = {"completed", "failed", "stopped"}


def _load_json(path: str) -> dict:
    if os.path.abspath(path) == os.path.abspath(DEFAULT_CONFIG_PATH):
        from .config import load_merged_config
        return load_merged_config()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _omakse_cfg() -> dict:
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    return cfg.get("omakse") if isinstance(cfg.get("omakse"), dict) else {}


def _resolve_base_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    return _omakse_cfg().get("base_url", DEFAULT_BASE_URL).rstrip("/")


def _resolve_proxy(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""
    return _omakse_cfg().get("proxy", proxy_default)


def _make_session(proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


# ─── Link Extraction ──────────────────────────────────────────────────────────


def create_extract_job(
    credentials: str,
    us_proxies: str = "",
    promotion_proxies: str = "",
    provider_country: str = "US",
    promotion_country: str = "VN",
    concurrency: int = 5,
    max_attempts: int = 3,
    base_url: str | None = None,
    proxy: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create a link extraction job on the omakse server.

    Args:
        credentials:  One or more Session/AT (newline-separated).
        us_proxies:   US proxy list (newline-separated, one per line).
        promotion_proxies: Promotion-region proxy list (newline-separated).
        provider_country:  PayPal provider country (default US).
        promotion_country: Promotion region country (default VN).
        concurrency:  Number of concurrent extractions.
        max_attempts: Max attempts per credential.
        base_url:     Omakse server URL (default from config).
        proxy:        Local proxy to reach the omakse server.
        timeout:      HTTP timeout.

    Returns:
        Job object: {id, status, counters, logs, links, ...}
    """
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(proxy))

    body = {
        "credentials": credentials,
        "promotionCountry": promotion_country.upper(),
        "providerCountry": provider_country.upper(),
        "usProxies": us_proxies,
        "promotionProxies": promotion_proxies,
        "concurrency": int(concurrency),
        "maxAttempts": int(max_attempts),
    }

    resp = session.post(
        f"{base}/api/link-extract/jobs",
        json=body,
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"omakse create job failed: {resp.status_code} "
            f"{data.get('detail', resp.text[:300])}"
        )
    return data


def get_extract_job(
    job_id: str,
    base_url: str | None = None,
    proxy: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Get the current status of a link extraction job."""
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(proxy))

    resp = session.get(
        f"{base}/api/link-extract/jobs/{job_id}",
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"omakse get job failed: {resp.status_code} "
            f"{data.get('detail', resp.text[:300])}"
        )
    return data


def extract_links(
    credentials: str,
    us_proxies: str = "",
    promotion_proxies: str = "",
    provider_country: str = "US",
    promotion_country: str = "VN",
    concurrency: int = 5,
    max_attempts: int = 3,
    poll_interval: float = POLL_INTERVAL,
    max_poll_seconds: int = MAX_POLL_DURATION,
    base_url: str | None = None,
    proxy: str = "",
    on_log: Any = None,
) -> dict[str, Any]:
    """Create a link extraction job and poll until completion.

    Args:
        on_log: Optional callback(step, msg) for progress logging.

    Returns:
        Final job object with status, counters, links, logs.
    """
    def _log(step: str, msg: str) -> None:
        if on_log:
            on_log(step, msg)
        else:
            print(f"[{step}] {msg}", file=sys.stderr)

    _log("create", "Creating omakse link extraction job...")
    job = create_extract_job(
        credentials=credentials,
        us_proxies=us_proxies,
        promotion_proxies=promotion_proxies,
        provider_country=provider_country,
        promotion_country=promotion_country,
        concurrency=concurrency,
        max_attempts=max_attempts,
        base_url=base_url,
        proxy=proxy,
    )
    job_id = job.get("id", "")
    if not job_id:
        raise RuntimeError(f"omakse job creation returned no id: {job}")
    _log("create", f"Job created: {job_id}, status={job.get('status', 'unknown')}")

    # If already terminal, return immediately
    if job.get("status") in TERMINAL_STATES:
        _log("done", f"Job already terminal: {job['status']}")
        links = job.get("links") or []
        return {
            "ok": job.get("status") == "completed" and len(links) > 0,
            "job_id": job_id,
            "status": job.get("status"),
            "counters": job.get("counters", {}),
            "links": links,
            "logs": job.get("logs", []),
        }

    # Poll until terminal
    deadline = time.time() + max_poll_seconds
    poll_count = 0
    last_log_len = 0

    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_count += 1
        try:
            job = get_extract_job(job_id, base_url=base_url, proxy=proxy)
        except Exception as e:
            _log("poll", f"Poll #{poll_count} error: {e}")
            continue

        status = job.get("status", "")
        counters = job.get("counters") or {}
        total = counters.get("total", 0)
        success = counters.get("success", 0)
        failed = counters.get("failed", 0)
        running = counters.get("running", 0)

        # Print new log lines
        logs = job.get("logs") or []
        if len(logs) > last_log_len:
            for line in logs[last_log_len:]:
                _log("job", str(line))
            last_log_len = len(logs)

        _log("poll", f"#{poll_count} status={status} total={total} "
             f"success={success} failed={failed} running={running}")

        if status in TERMINAL_STATES:
            break

    final_status = job.get("status", "unknown")
    links = job.get("links") or []
    _log("done", f"Job {job_id} finished: {final_status}, {len(links)} link(s) extracted")

    return {
        "ok": final_status == "completed" and len(links) > 0,
        "job_id": job_id,
        "status": final_status,
        "counters": job.get("counters", {}),
        "links": links,
        "logs": job.get("logs", []),
    }


# ─── US Protocol Payment ──────────────────────────────────────────────────────


def run_us_payment(
    ba_token: str,
    proxy: str = "",
    phone_country: str = "US",
    phone_country_code: str = "1",
    proxy_region: str = "US",
    client_id: str | None = None,
    randomize_device: bool = False,
    preconfirm_phone: bool = False,
    send_phone_otp: bool = False,
    load_return_url: bool = False,
    base_url: str | None = None,
    local_proxy: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Start a US PayPal protocol payment job on the omakse server.

    Args:
        ba_token:     PayPal BA token (BA-...) or full approve URL.
        proxy:        Proxy for the payment flow (e.g. http://...).
        client_id:    Optional client ID (auto-generated if omitted).
        base_url:     Omakse server URL.
        local_proxy:  Local proxy to reach the omakse server itself.

    Returns:
        {ok, job_id, status, ...}
    """
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(local_proxy))

    if not client_id:
        client_id = f"us-{int(time.time()):x}-{uuid.uuid4().hex[:8]}"

    body = {
        "baToken": ba_token,
        "proxy": proxy,
        "phoneCountry": phone_country.upper(),
        "phoneCountryCode": phone_country_code,
        "proxyRegion": proxy_region.upper(),
        "clientId": client_id,
        "randomizeDevice": randomize_device,
        "preconfirmPhone": preconfirm_phone,
        "sendPhoneOtp": send_phone_otp,
        "loadReturnUrl": load_return_url,
    }

    resp = session.post(
        f"{base}/api/paypal-us-protocol/run",
        json=body,
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"omakse US payment run failed: {resp.status_code} "
            f"{data.get('detail', resp.text[:300])}"
        )

    job_id = data.get("job_id", "")
    return {
        "ok": bool(job_id),
        "job_id": job_id,
        "client_id": client_id,
        "status": data.get("status", "started"),
        "raw": data,
    }


def get_us_payment_status(
    job_id: str,
    base_url: str | None = None,
    local_proxy: str = "",
    tail: int = 1000,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Get the current status of a US protocol payment job."""
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(local_proxy))

    params = {"tail": tail}
    if job_id:
        params["jobId"] = job_id

    resp = session.get(
        f"{base}/api/paypal-us-protocol/status",
        params=params,
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"omakse US status failed: {resp.status_code} "
            f"{data.get('detail', resp.text[:300])}"
        )
    return data


def get_us_payment_logs(
    job_id: str,
    base_url: str | None = None,
    local_proxy: str = "",
    tail: int = 1000,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[str]:
    """Get the logs of a US protocol payment job."""
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(local_proxy))

    params = {"tail": tail}
    if job_id:
        params["jobId"] = job_id

    resp = session.get(
        f"{base}/api/paypal-us-protocol/logs",
        params=params,
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        return []
    return data.get("lines") or data.get("logs") or []


def cancel_us_payment(
    job_id: str,
    base_url: str | None = None,
    local_proxy: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Cancel a running US protocol payment job."""
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(local_proxy))

    url = f"{base}/api/paypal-us-protocol/cancel"
    if job_id:
        url += f"?jobId={job_id}"

    resp = session.post(url, timeout=timeout)
    data = resp.json() if resp.text else {}
    return data


def run_us_payment_and_wait(
    ba_token: str,
    proxy: str = "",
    poll_interval: float = POLL_INTERVAL,
    max_poll_seconds: int = MAX_POLL_DURATION,
    base_url: str | None = None,
    local_proxy: str = "",
    on_log: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Start a US payment job and poll until terminal.

    Args:
        on_log: Optional callback(step, msg) for progress logging.
        **kwargs: Passed through to run_us_payment().

    Returns:
        {ok, job_id, status, result, logs}
    """
    def _log(step: str, msg: str) -> None:
        if on_log:
            on_log(step, msg)
        else:
            print(f"[{step}] {msg}", file=sys.stderr)

    _log("start", "Starting omakse US protocol payment...")
    start_result = run_us_payment(
        ba_token=ba_token,
        proxy=proxy,
        base_url=base_url,
        local_proxy=local_proxy,
        **kwargs,
    )
    job_id = start_result.get("job_id", "")
    if not job_id:
        raise RuntimeError(f"omakse US payment returned no job_id: {start_result}")
    _log("start", f"Job started: {job_id}")

    deadline = time.time() + max_poll_seconds
    poll_count = 0
    last_log_len = 0
    status_data: dict[str, Any] = {}

    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_count += 1
        try:
            status_data = get_us_payment_status(
                job_id, base_url=base_url, local_proxy=local_proxy,
            )
        except Exception as e:
            _log("poll", f"Poll #{poll_count} error: {e}")
            continue

        status = status_data.get("status", "")
        result = status_data.get("result") or {}
        queue = status_data.get("queue") or {}

        _log("poll", f"#{poll_count} status={status} "
             f"result_ok={result.get('ok')} queue={queue.get('active', 0)}/{queue.get('total', 0)}")

        # Print new logs
        logs = get_us_payment_logs(job_id, base_url=base_url, local_proxy=local_proxy)
        if len(logs) > last_log_len:
            for line in logs[last_log_len:]:
                _log("job", str(line))
            last_log_len = len(logs)

        # Terminal states for US payment
        if status in {"completed", "failed", "stopped", "idle"} and poll_count > 1:
            if result.get("ok") is not None or status in {"failed", "stopped"}:
                break

    final_status = status_data.get("status", "unknown")
    result = status_data.get("result") or {}
    _log("done", f"US payment {job_id} finished: {final_status}, result.ok={result.get('ok')}")

    return {
        "ok": result.get("ok") is True,
        "job_id": job_id,
        "status": final_status,
        "result": result,
        "logs": get_us_payment_logs(job_id, base_url=base_url, local_proxy=local_proxy),
    }


# ─── Proxy Test ───────────────────────────────────────────────────────────────


def test_proxy(
    proxy: str,
    base_url: str | None = None,
    local_proxy: str = "",
    timeout: int = 15,
) -> dict[str, Any]:
    """Test a proxy via the omakse server."""
    base = _resolve_base_url(base_url)
    session = _make_session(_resolve_proxy(local_proxy))

    resp = session.post(
        f"{base}/api/proxy/test",
        json={"proxy": proxy},
        timeout=timeout,
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        return {"ok": False, "error": data.get("detail", resp.text[:300])}
    return data


# ─── Convenience: extract from account ────────────────────────────────────────


def extract_links_for_account(
    email: str = "",
    access_token: str = "",
    session_file: str = "",
    us_proxies: str = "",
    promotion_proxies: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Extract PayPal links for a local account via the omakse server.

    Reads the access token from SQLite/session JSON if not provided directly.
    """
    credentials = access_token
    if not credentials and email:
        # Try loading from SQLite
        try:
            import sqlite3
            db_path = os.path.join(PROJECT_ROOT, "runtime", "accounts.sqlite3")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                "SELECT access_token FROM accounts WHERE email=? ORDER BY updated_at DESC LIMIT 1",
                (email,),
            )
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                credentials = row[0]
        except Exception:
            pass

    if not credentials and session_file:
        try:
            data = _load_json(session_file)
            credentials = data.get("access_token", "")
        except Exception:
            pass

    if not credentials:
        return {"ok": False, "error": "No access token found for the given account"}

    return extract_links(
        credentials=credentials,
        us_proxies=us_proxies,
        promotion_proxies=promotion_proxies,
        **kwargs,
    )


# ─── Authorization-result normalization ──────────────────────────────────────
#
# The omakse US-payment job returns a free-form ``result`` dict.  This helper
# normalizes it through the read-only ``paypal_authorization`` parser so callers
# get a stable, PaymentResult-compatible view (ok/status/error_code/retryable)
# without changing any existing return structure.


def normalize_us_payment_result(
    job_result: dict[str, Any] | None,
    *,
    payment_method: str = "paypal",
    operation: str = "execute_payment",
    ba_token: str = "",
) -> dict[str, Any]:
    """Normalize an omakse US-payment job result into a PaymentResult-compatible dict.

    ``job_result`` may be the ``result`` field of ``run_us_payment_and_wait`` /
    ``get_us_payment_status``, or a full run/status dict (its ``result`` / ``raw``
    sub-dict is then used).  The returned dict feeds directly into
    ``PaymentResult.from_mapping`` / ``payment_retry_allowed``.
    """
    from .paypal_authorization import parse_authorization_context, to_payment_result

    payload: Any = job_result or {}
    if isinstance(payload, dict):
        # unwrap common wrappers
        for key in ("result", "raw", "data", "authorization"):
            nested = payload.get(key)
            if isinstance(nested, dict) and nested:
                payload = nested
                break

    ctx = parse_authorization_context(payload, ba_token=ba_token)
    normalized = to_payment_result(
        ctx,
        payment_method=payment_method,
        operation=operation,
    )
    # If the job already carried a URL, keep it.
    url = ""
    if isinstance(job_result, dict):
        url = str(job_result.get("url") or job_result.get("return_url") or "").strip()
    if url:
        normalized["url"] = url
    return normalized
