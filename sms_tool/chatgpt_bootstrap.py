"""ChatGPT first-screen warm-up ("bootstrap") for browser registrations.

Mirrors ``turb-gpt-free-register``'s ``core/chatgpt_bootstrap.py``: replay the
request sequence a real browser issues when it first lands on ChatGPT, so a
freshly registered account looks like a real user session instead of a
"register and vanish" API call.  The three accounts lost on 2026-08-29 had
exactly that signature — registered fine, then batch-deactivated hours later
with no activity in between.

Two deliberate deviations from the reference project, both for safety:

1. **Traffic goes through the browser, not ``curl_cffi``.**  turb warms up its
   *protocol* session.  This module warms up a *browser* registration, so every
   request is issued with ``page.evaluate(fetch(...))`` — the same pattern
   ``_bind_totp_in_browser`` uses — and therefore carries the browser's real
   cookies, TLS fingerprint and Cloudflare clearance.  A server-side warm-up
   would present a different fingerprint than the one that registered, which
   defeats the entire purpose.

2. **Read-only ``GET`` requests only.**  turb also POSTs
   ``sentinel/chat-requirements`` prepare/finalize and ``conversation/init``.
   Those need a generated ``p`` token and risk creating half-constructed
   challenges (turb's own docstring warns about exactly this), so they are
   intentionally omitted here.

Non-fatal contract (inherited from turb): **bootstrapping is decoration, never a
gate.**  Every request is wrapped so a failure is logged and counted, never
raised.  ``strict=True`` exists for diagnostics only and is never used by the
registration flow.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ANON_BASE = "https://chatgpt.com/backend-anon"
API_BASE = "https://chatgpt.com/backend-api"
REFERER = "https://chatgpt.com/"

# The feature is OFF until explicitly enabled in
# ``registration.chatgpt_bootstrap.enabled``.
DEFAULT_BOOTSTRAP_CONFIG: dict[str, Any] = {
    "enabled": False,
    "anonymous": True,
    "authenticated": True,
}

_GET_SCRIPT = """
async ([url, headers]) => {
    try {
        const r = await fetch(url, {
            method: "GET",
            headers: headers || {},
            credentials: "include",
        });
        const text = await r.text().catch(() => "");
        return {status: r.status, ok: r.ok, body: text.slice(0, 200)};
    } catch (e) {
        return {status: 0, ok: false, error: String(e)};
    }
}
"""

_TZ_OFFSET_SCRIPT = "() => -new Date().getTimezoneOffset()"

_ANON_PATHS = (
    "/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
    "/me",
    "/system_hints?mode=custom_agents",
    "/system_hints?mode=connectors",
    "/system_hints?mode=basic",
    "/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
)

_AUTH_PATHS = (
    "/accounts/optimized/check",
    "/me",
    "/settings/user",
    "/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
    "/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
    "/conversations?offset=0&limit=28&order=updated",
    "/client/strings",
)


def bootstrap_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read ``registration.chatgpt_bootstrap``; unknown keys keep the default.

    Always returns a fully populated dict so callers never have to guard for
    missing keys.  Defaults keep the feature disabled.
    """
    merged = dict(DEFAULT_BOOTSTRAP_CONFIG)
    section = (config or {}).get("registration")
    if isinstance(section, Mapping):
        raw = section.get("chatgpt_bootstrap")
        if isinstance(raw, Mapping):
            for key in DEFAULT_BOOTSTRAP_CONFIG:
                if key in raw:
                    merged[key] = raw[key]
    merged["enabled"] = bool(merged["enabled"])
    merged["anonymous"] = bool(merged["anonymous"])
    merged["authenticated"] = bool(merged["authenticated"])
    return merged


def _timezone_offset_minutes(page: Any) -> int:
    """JS-side UTC offset in minutes; ``0`` when the probe fails."""
    try:
        return int(page.evaluate(_TZ_OFFSET_SCRIPT))
    except Exception:
        return 0


def _get(
    page: Any,
    url: str,
    headers: dict[str, str],
    *,
    label: str,
    strict: bool,
    stats: dict[str, int],
) -> dict[str, Any] | None:
    """Issue one warm-up GET. Never raises unless ``strict`` is set."""
    stats["attempted"] += 1
    try:
        result = page.evaluate(_GET_SCRIPT, [url, headers])
    except Exception as exc:
        stats["failed"] += 1
        message = f"[bootstrap] {label} raised {type(exc).__name__}: {str(exc)[:180]}"
        if strict:
            raise RuntimeError(message) from exc
        logger.debug(message)
        return None

    status = 0
    if isinstance(result, dict):
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
    if 200 <= status < 400:
        stats["ok"] += 1
        return result

    stats["failed"] += 1
    message = f"[bootstrap] {label} -> HTTP {status} (ignored)"
    if strict:
        raise RuntimeError(message)
    logger.debug(message)
    return None


def _auth_headers(access_token: str = "", device_id: str = "") -> dict[str, str]:
    headers = {"Referer": REFERER, "oai-language": "en-US"}
    token = str(access_token or "").strip()
    if token:
        headers["Authorization"] = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
    if device_id:
        headers["oai-device-id"] = str(device_id)
    return headers


def _run_paths(page: Any, base: str, paths: tuple[str, ...], headers: dict[str, str], tz: int, *, strict: bool, stats: dict[str, int]) -> None:
    for path in paths:
        url = f"{base}{path.format(tz=tz)}"
        _get(page, url, headers, label=path.split("?")[0], strict=strict, stats=stats)


def anonymous_bootstrap(
    page: Any,
    *,
    strict: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Warm up the anonymous ChatGPT session (called before registration)."""
    stats = {"attempted": 0, "ok": 0, "failed": 0}
    del config
    if page is None:
        return {"ok": False, "skipped": True, "reason": "no_page", "stats": stats}

    tz = _timezone_offset_minutes(page)
    logger.info("[bootstrap] anonymous ChatGPT warm-up start")
    try:
        _run_paths(
            page, ANON_BASE, _ANON_PATHS, _auth_headers(), tz,
            strict=strict, stats=stats,
        )
    except Exception as exc:
        # strict is a diagnostic escape hatch: surface the real error so a
        # developer can see it.  Production always goes through the run_*
        # entry points, which pass strict=False and swallow everything.
        if strict:
            raise
        logger.debug("[bootstrap] anonymous warm-up aborted: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stats": stats}
    logger.info(
        "[bootstrap] anonymous ChatGPT warm-up done ok=%s failed=%s",
        stats["ok"], stats["failed"],
    )
    return {"ok": stats["failed"] == 0, "stats": stats}


def authenticated_bootstrap(
    page: Any,
    access_token: str = "",
    *,
    device_id: str = "",
    strict: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Warm up the logged-in ChatGPT session (called after registration)."""
    stats = {"attempted": 0, "ok": 0, "failed": 0}
    del config
    if page is None:
        return {"ok": False, "skipped": True, "reason": "no_page", "stats": stats}

    tz = _timezone_offset_minutes(page)
    logger.info("[bootstrap] authenticated ChatGPT warm-up start")
    try:
        _run_paths(
            page, API_BASE, _AUTH_PATHS,
            _auth_headers(access_token, device_id), tz,
            strict=strict, stats=stats,
        )
    except Exception as exc:
        # strict is a diagnostic escape hatch: surface the real error so a
        # developer can see it.  Production always goes through the run_*
        # entry points, which pass strict=False and swallow everything.
        if strict:
            raise
        logger.debug("[bootstrap] authenticated warm-up aborted: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stats": stats}
    logger.info(
        "[bootstrap] authenticated ChatGPT warm-up done ok=%s failed=%s",
        stats["ok"], stats["failed"],
    )
    return {"ok": stats["failed"] == 0, "stats": stats}


def run_anonymous_bootstrap(
    page: Any,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Config-gated entry point used by the registration flow. Never raises."""
    cfg = bootstrap_config(config)
    if not cfg["enabled"] or not cfg["anonymous"]:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    try:
        return anonymous_bootstrap(page, strict=False)
    except Exception as exc:  # defensive: warm-up must never break registration
        logger.debug("[bootstrap] anonymous warm-up crashed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_authenticated_bootstrap(
    page: Any,
    access_token: str = "",
    *,
    device_id: str = "",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Config-gated entry point used by the registration flow. Never raises."""
    cfg = bootstrap_config(config)
    if not cfg["enabled"] or not cfg["authenticated"]:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    try:
        return authenticated_bootstrap(
            page, access_token, device_id=device_id, strict=False
        )
    except Exception as exc:  # defensive: warm-up must never break registration
        logger.debug("[bootstrap] authenticated warm-up crashed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "ANON_BASE",
    "API_BASE",
    "DEFAULT_BOOTSTRAP_CONFIG",
    "anonymous_bootstrap",
    "authenticated_bootstrap",
    "bootstrap_config",
    "run_anonymous_bootstrap",
    "run_authenticated_bootstrap",
]
