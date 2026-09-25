"""Codex quota (``wham/usage``) parsing and display formatting.

Split out of :mod:`sms_tool.accounts.account_liveness` so the liveness module
owns only the probe and its result contract. Everything here is pure: it turns
an already-fetched usage body into the structured ``wham_usage`` mapping and
the compact ``5h: 1.2K/10K (12%) | 7d: ...`` label. No network, no config, no
account state.

``account_liveness`` re-exports these names so existing importers keep working;
new code should import from this module directly.
"""

from __future__ import annotations

import json
from typing import Any

WINDOW_KEYS = ("5h", "7d")

#: Accepted upstream spellings for each usage window, most specific first.
_WINDOW_ALTERNATIVES = {
    "5h": ("5h", "300min", "five_hours", "short"),
    "7d": ("7d", "10080min", "seven_days", "weekly", "long"),
}

_USED_KEYS = ("used", "num_tokens_used", "tokens_used", "consumed")
_LIMIT_KEYS = ("limit", "num_tokens_limit", "tokens_limit", "max", "cap")
_REMAINING_KEYS = ("remaining", "num_tokens_remaining", "tokens_remaining", "available")
_RESET_KEYS = ("resets_at", "reset_at", "reset_time", "expires_at")


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
    for window_key in WINDOW_KEYS:
        parsed = _parse_usage_window(body, window_key)
        if parsed:
            result[window_key] = parsed

    for window_key in WINDOW_KEYS:
        if window_key not in result:
            continue
        for container_key in ("usage", "rate_limits", "limits"):
            container = body.get(container_key) if isinstance(body.get(container_key), dict) else body
            if not isinstance(container, dict):
                continue
            window = container.get(window_key)
            if not isinstance(window, dict):
                continue
            for reset_key in _RESET_KEYS:
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
    for window_key in WINDOW_KEYS:
        window = usage.get(window_key)
        if not isinstance(window, dict):
            continue
        used = window.get("used", 0)
        limit = window.get("limit", 0)
        percent = float(window.get("percent", 0) or 0)
        parts.append(f"{window_key}: {_format_token_count(used)}/{_format_token_count(limit)} ({percent:.0f}%)")
    return " | ".join(parts)


def _parse_usage_window(body: dict[str, Any], window_key: str) -> dict[str, Any] | None:
    containers = [
        section
        for key in ("usage", "rate_limits", "limits", "rate_limits_info")
        if isinstance((section := body.get(key)), dict)
    ]
    containers.append(body)
    for container in containers:
        window = next(
            (
                container.get(key)
                for key in _WINDOW_ALTERNATIVES.get(window_key, (window_key,))
                if isinstance(container.get(key), dict)
            ),
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

        used = pick(_USED_KEYS)
        limit = pick(_LIMIT_KEYS)
        remaining = pick(_REMAINING_KEYS)
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


__all__ = ["format_wham_usage_label", "parse_wham_usage"]
