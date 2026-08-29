"""Canonical ChatGPT access-token liveness and Codex quota contract.

This module owns the direct ``/backend-api/wham/usage`` probe. Registration,
payments, account scans, operator scripts, and provider adapters must depend on
this seam instead of defining their own endpoint, headers, or classification.
"""

from __future__ import annotations

import base64
import json
import re
from contextlib import contextmanager
from typing import Any, Generator
from curl_cffi import requests as curl_requests

from .account_identity import account_identity, bind_account_identity
from .auth_headers import auth_impersonate, chatgpt_headers
from .config import CFG
from .phone_proxy import normalize_proxy_url, redact_proxy_url as _redact_proxy_url
from .proxy_routing import select_operation_proxy


CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_QUOTA_HEADERS = {
    "Authorization": "Bearer $TOKEN$",
    "Content-Type": "application/json",
    "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
}


def redact_proxy_url(proxy: str | None) -> str:
    """Return a log-safe proxy URL without exposing credentials (delegates to phone_proxy)."""
    return _redact_proxy_url(proxy, empty_placeholder="")


@contextmanager
def browser_fetch_for_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 30,
    config: Any = None,
) -> Generator[Any, None, None]:
    """Open a browser session and yield a ``browser_fetch`` callable.

    When the account has a saved ``browser_identity`` (set during browser-driver
    registration), this opens a browser session with the same driver and profile,
    navigates to chatgpt.com, waits for Cloudflare to clear, and yields the
    browser's ``fetch_json`` method so liveness/promotion probes carry the real
    fingerprint and cookies instead of falling back to curl_cffi.

    Yields ``None`` when no ``browser_identity`` is present or when the browser
    session cannot be opened, so callers can fall back to curl_cffi.
    """
    identity = account_identity(account)
    browser_identity = identity.get("browser_identity") or {}
    driver = str(browser_identity.get("driver") or "").strip().lower()

    if not driver:
        yield None
        return

    cfg = config or CFG
    health_proxy = select_operation_proxy(
        account,
        operation="health_browser",
        explicit=proxy,
        config=cfg,
    )
    chat_cfg = cfg.get("chatgpt", {}) if isinstance(cfg, dict) else {}
    chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
    auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    device_id = str(identity.get("device_id") or account.get("device_id") or "").strip()

    session = None
    browser = None
    fetch_fn: Any = None
    try:
        from .registration_drivers.external_sessions import create_browser_session
        from .registration_drivers.playwright import _wait_for_challenge_clear

        session = create_browser_session(
            driver,
            config=cfg,
            proxy=health_proxy,
            headless=True,
            timeout_ms=max(10_000, int(timeout) * 1000),
            locale="en-US",
            timezone_id="America/New_York",
            browser_identity=dict(browser_identity) if browser_identity else None,
        )
        browser = session.__enter__()
        if device_id:
            try:
                browser.add_device_cookie(device_id, chat_base, auth_base)
            except Exception:
                pass
        page = browser.page
        page.goto(chat_base, wait_until="domcontentloaded", timeout=max(5_000, int(timeout) * 1000))
        _wait_for_challenge_clear(page, max_wait_seconds=min(30, int(timeout)))
        fetch_fn = browser.fetch_json
    except Exception:
        if browser is not None and session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass
        browser = None
        session = None
        fetch_fn = None

    try:
        yield fetch_fn
    finally:
        if browser is not None and session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


def probe_account_liveness(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 30,
    *,
    browser_fetch: Any = None,
) -> dict[str, Any]:
    """Probe a saved account without relogin or persistence side effects.

    When ``browser_fetch`` is provided, the quota probe is routed through
    the browser context's ``fetch_json`` method instead of ``curl_cffi``,
    carrying the real browser fingerprint and cookies to bypass
    Cloudflare-based 401 blocks on protocol-only requests.
    """
    if not isinstance(account, dict):
        return {
            "ok": False,
            "mode": "local",
            "status": "unknown",
            "quota_status": "缺少账号",
            "error": "invalid_account",
        }
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return {
            "ok": False,
            "mode": "local",
            "status": "unknown",
            "quota_status": "缺少AT",
            "error": "missing_access_token",
        }

    had_identity_context = bool(account.get("identity_context"))
    identity = bind_account_identity(account)
    # Health probes deliberately do not restore registration proxy affinity.
    resolved_proxy = select_operation_proxy(
        account if had_identity_context else {key: value for key, value in account.items() if key != "identity_context"},
        operation="liveness",
        explicit=proxy,
        config=CFG,
    )

    device_id = str(identity.get("device_id") or "")
    headers = chatgpt_headers(device_id, accept="application/json")
    headers["Authorization"] = f"Bearer {access_token}"
    headers["Content-Type"] = "application/json"
    account_id = account_chatgpt_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    normalized_proxy = normalize_proxy_url(resolved_proxy)
    proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None

    # When a browser fetch callable is provided, route the probe through the
    # browser context to carry the real fingerprint and cookies.  This bypasses
    # Cloudflare 401 blocks that affect protocol-only requests.
    if browser_fetch is not None:
        try:
            result = browser_fetch(CODEX_USAGE_URL, headers=headers, timeout_ms=timeout * 1000)
            # The browser fetch returns the HTTP status under the ``status`` key
            # (see the anti-detect drivers' fetch_json contract), not
            # ``status_code``. Normalize so the classifier sees the real code
            # instead of discarding a genuine 401 as a transport failure.
            if isinstance(result, dict) and "status_code" not in result and "status" in result:
                result = {**result, "status_code": result.get("status")}
            if isinstance(result, dict) and "status_code" in result:
                return quota_result_from_payload(
                    result,
                    status_code=result.get("status_code"),
                    mode="browser",
                    account_id=account_id,
                )
            return quota_result_from_payload(
                {"status_code": 0, "body": result},
                status_code=0,
                mode="browser",
                account_id=account_id,
                transport_ok=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "mode": "browser",
                "status": "unknown",
                "quota_status": "检测失败",
                "error": str(exc)[:500],
            }

    try:
        response = curl_requests.get(
            CODEX_USAGE_URL,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            impersonate=auth_impersonate(),
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": str(response.text or "")[:500]}
        return quota_result_from_payload(
            {"status_code": response.status_code, "body": body},
            status_code=response.status_code,
            mode="local",
            account_id=account_id,
        )
    except Exception as exc:
        error = str(exc)
        for candidate in (str(proxy or "").strip(), str(resolved_proxy or "").strip(), normalized_proxy):
            if candidate:
                error = error.replace(candidate, redact_proxy_url(candidate))
        return {
            "ok": False,
            "mode": "local",
            "status": "unknown",
            "quota_status": "检测失败",
            "error": error,
        }


def quota_result_from_payload(
    payload: Any,
    status_code: int | None = None,
    *,
    mode: str,
    account_id: str = "",
    transport_ok: bool | None = None,
) -> dict[str, Any]:
    """Normalize direct and proxied quota responses into one result contract."""
    wrapped = payload if isinstance(payload, dict) else {"body": payload}
    resolved_status = _extract_status_code(wrapped) if status_code is None else _as_int(status_code)
    error_text = _extract_error_text(wrapped)
    if _is_token_invalid(resolved_status, error_text):
        status = "token_invalid"
    elif 200 <= resolved_status < 300:
        status = "active"
    else:
        status = "unknown"
    body = wrapped.get("body") if isinstance(wrapped, dict) else {}
    usage = parse_wham_usage(body)
    usage_label = format_wham_usage_label(usage)
    result = {
        "ok": bool(transport_ok) if transport_ok is not None else 200 <= resolved_status < 300,
        "mode": mode,
        "status": status,
        "quota_status": usage_label or _quota_status_label(wrapped, resolved_status, error_text),
        "wham_usage": usage,
        "status_code": resolved_status,
        "error": error_text,
    }
    if account_id:
        result["account_id"] = account_id
    return result


def parse_wham_usage(body: Any) -> dict[str, Any] | None:
    """Parse structured five-hour and seven-day usage windows."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None

    result: dict[str, Any] = {}
    for window_key in ("5h", "7d"):
        parsed = _parse_usage_window(body, window_key)
        if parsed:
            result[window_key] = parsed

    for window_key in ("5h", "7d"):
        if window_key not in result:
            continue
        for container_key in ("usage", "rate_limits", "limits"):
            container = body.get(container_key) if isinstance(body.get(container_key), dict) else body
            if not isinstance(container, dict):
                continue
            window = container.get(window_key)
            if not isinstance(window, dict):
                continue
            for reset_key in ("resets_at", "reset_at", "reset_time", "expires_at"):
                reset_value = window.get(reset_key)
                if reset_value is not None:
                    result[window_key]["reset_at"] = str(reset_value)
                    break
    return result or None


def format_wham_usage_label(usage: dict[str, Any] | None) -> str:
    """Format parsed quota data for CLI and desktop display."""
    if not usage:
        return ""
    parts = []
    for window_key in ("5h", "7d"):
        window = usage.get(window_key)
        if not isinstance(window, dict):
            continue
        used = window.get("used", 0)
        limit = window.get("limit", 0)
        percent = float(window.get("percent", 0) or 0)
        parts.append(f"{window_key}: {_format_token_count(used)}/{_format_token_count(limit)} ({percent:.0f}%)")
    return " | ".join(parts)


def account_chatgpt_id(account: dict[str, Any]) -> str:
    candidates = [
        account.get("chatgpt_account_id"),
        account.get("account_id"),
        account.get("workspace_id"),
        account.get("k12_workspace_id"),
        _nested_value(account, "account", "id"),
        _nested_value(account, "auth_session", "account", "id"),
    ]
    for token_key in ("id_token", "access_token"):
        token_account = chatgpt_id_from_token(account.get(token_key))
        if token_account:
            candidates.append(token_account)
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def chatgpt_id_from_token(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("chatgpt_account_id") or value.get("chatgptAccountId") or "").strip()
    token = str(value or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or auth.get("chatgptAccountId") or "").strip()
        if account_id:
            return account_id
    return str(data.get("chatgpt_account_id") or data.get("chatgptAccountId") or "").strip()


def _parse_usage_window(body: dict[str, Any], window_key: str) -> dict[str, Any] | None:
    containers = [
        section
        for key in ("usage", "rate_limits", "limits", "rate_limits_info")
        if isinstance((section := body.get(key)), dict)
    ]
    containers.append(body)
    alternatives = {
        "5h": ("5h", "300min", "five_hours", "short"),
        "7d": ("7d", "10080min", "seven_days", "weekly", "long"),
    }
    for container in containers:
        window = next(
            (container.get(key) for key in alternatives.get(window_key, (window_key,)) if isinstance(container.get(key), dict)),
            None,
        )
        if not isinstance(window, dict):
            continue

        def pick(keys: tuple[str, ...]) -> int | None:
            for key in keys:
                value = window.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
            return None

        used = pick(("used", "num_tokens_used", "tokens_used", "consumed"))
        limit = pick(("limit", "num_tokens_limit", "tokens_limit", "max", "cap"))
        remaining = pick(("remaining", "num_tokens_remaining", "tokens_remaining", "available"))
        if remaining is None and used is not None and limit is not None:
            remaining = max(0, limit - used)
        if used is None and remaining is not None and limit is not None:
            used = max(0, limit - remaining)
        if used is not None or limit is not None or remaining is not None:
            return {
                "used": used or 0,
                "limit": limit or 0,
                "remaining": remaining or 0,
                "percent": round((used or 0) * 100.0 / limit, 1) if limit else 0.0,
            }
    return None


def _extract_status_code(payload: dict[str, Any]) -> int:
    for key in ("status_code", "statusCode"):
        value = _as_int(payload.get(key))
        if value:
            return value
    return 0


def _extract_error_text(payload: dict[str, Any]) -> str:
    values = []
    body = payload.get("body")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            values.extend((error.get("message"), error.get("code")))
        else:
            values.append(error)
        values.append(body.get("message"))
    else:
        values.append(body)
    values.extend((payload.get("bodyText"), payload.get("error"), payload.get("message")))
    return " ".join(str(value or "") for value in values if str(value or "").strip()).strip()[:500]


def _quota_status_label(payload: dict[str, Any], status_code: int, error_text: str = "") -> str:
    if _is_token_invalid(status_code, error_text):
        return "401失效"
    if status_code in (402, 429) or re.search(r"insufficient|exceeded|rate.?limit|too many", error_text, re.I):
        return "额度不足"
    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = {}
    if isinstance(body, dict):
        text_candidates = [str(body[key]) for key in ("status", "message", "quota_status", "usage_status") if body.get(key)]
        for section_key in ("quota", "usage", "limits", "codex"):
            section = body.get(section_key)
            if not isinstance(section, dict):
                continue
            remaining = section.get("remaining") or section.get("remaining_tokens") or section.get("available")
            limit = section.get("limit") or section.get("total") or section.get("max")
            if remaining is not None or limit is not None:
                return f"{remaining or 0}/{limit}" if limit is not None else str(remaining)
            if section.get("status") or section.get("message"):
                text_candidates.append(str(section.get("status") or section.get("message")))
        if text_candidates:
            return " / ".join(text_candidates)[:80]
    if 200 <= status_code < 300:
        return "可用"
    return f"HTTP {status_code}" if status_code else "未知"


def _is_token_invalid(status_code: int, error_text: str) -> bool:
    return _as_int(status_code) == 401 or re.search(
        r"\b401\b|unauthorized|authentication token has been invalidated|token has been invalidated|invalid_grant|refresh_token",
        str(error_text or "").lower(),
    ) is not None


def _nested_value(data: dict[str, Any], *keys: str) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node


def _format_token_count(value: Any) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return str(value)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
