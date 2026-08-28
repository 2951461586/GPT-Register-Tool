"""Optional Playwright init-script hardening for browser registration sessions.

The provider browser normally owns the user-agent and client-hint profile.  The
stealth scripts therefore only cover JavaScript automation signals and leave
those provider-selected values unchanged.  The dependency is optional at
runtime so a missing package remains a normal, testable browser configuration
state rather than preventing the driver from starting.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def build_playwright_stealth(*, label: str = "browser", provider_prefix: str = "playwright") -> Any | None:
    """Build the reference project's conservative ``playwright-stealth`` profile."""
    del provider_prefix
    try:
        from playwright_stealth import Stealth
    except ImportError:
        logger.debug("[%s] playwright-stealth is unavailable; continuing without the optional init script", label)
        return None
    try:
        return Stealth(
            # Keep the provider's native UA, client hints, platform, and WebGL.
            navigator_user_agent=False,
            navigator_user_agent_data=False,
            navigator_platform=False,
            sec_ch_ua=False,
            webgl_vendor=False,
            # Patch browser automation signals only.
            chrome_runtime=True,
            chrome_app=True,
            chrome_csi=True,
            chrome_load_times=True,
            hairline=True,
            iframe_content_window=True,
            media_codecs=True,
            navigator_hardware_concurrency=True,
            navigator_languages=True,
            navigator_permissions=True,
            navigator_plugins=True,
            navigator_vendor=True,
            navigator_webdriver=True,
            error_prototype=True,
            init_scripts_only=True,
        )
    except Exception as exc:
        logger.debug("[%s] could not construct playwright-stealth: %s", label, type(exc).__name__)
        return None


def apply_playwright_stealth(
    context: Any,
    page: Any | None = None,
    *,
    label: str = "browser",
    provider_prefix: str = "playwright",
) -> dict[str, Any]:
    """Apply optional init scripts to a context and its current page.

    Existing connected CDP contexts are supported.  Every failure is captured
    in status metadata because stealth must never hide the registration error
    that actually matters to the caller.
    """
    result: dict[str, Any] = {"playwright_stealth": False}
    stealth = build_playwright_stealth(label=label, provider_prefix=provider_prefix)
    if stealth is None:
        result["reason"] = "dependency_missing_or_unavailable"
        return result

    applied = False
    for target_name, target in (("context", context), ("page", page)):
        if target is None:
            continue
        try:
            stealth.apply_stealth_sync(target)
            applied = True
        except Exception as exc:
            result[f"{target_name}_error"] = type(exc).__name__
    if applied:
        result["playwright_stealth"] = True
    elif "context_error" in result or "page_error" in result:
        result["reason"] = "apply_failed"
    return result


__all__ = ["apply_playwright_stealth", "build_playwright_stealth"]
