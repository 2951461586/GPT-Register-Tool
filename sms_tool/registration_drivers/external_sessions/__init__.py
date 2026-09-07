"""Stable browser-session factory; implementation lives behind this import path."""

from typing import Any, Mapping

from ...driver_env import driver_config as _driver_config
from ..base import BROWSER_REGISTRATION_DRIVERS, BrowserRegistrationError, normalize_registration_driver
from ..browser_session import PlaywrightBrowserSession, _playwright_proxy
from .managed import (
    AdsPowerBrowserSession, CamoufoxBrowserSession, CloakBrowserSession,
    ConnectedPlaywrightSession, RoxyBrowserSession,
    MOZ_DISABLE_CONTENT_SANDBOX, _first, _normalize_debugger_address, _require, _roxy_retryable,
    apply_playwright_stealth, curl_requests, time,
)
from .profiles import _browser_profile_dir, _inject_browser_profile
from .probe import verify_browser_proxy_country

_BROWSER_SESSION_FACTORIES: dict[str, type[PlaywrightBrowserSession]] = {
    "cloak": CloakBrowserSession,
    "camoufox": CamoufoxBrowserSession,
    "roxy": RoxyBrowserSession,
    "adspower": AdsPowerBrowserSession,
}
assert set(_BROWSER_SESSION_FACTORIES) == BROWSER_REGISTRATION_DRIVERS - {"playwright"}


def create_browser_session(
    driver: str, *, config: Mapping[str, Any], proxy: str | None, headless: bool,
    timeout_ms: int, locale: str, timezone_id: str,
    browser_identity: Mapping[str, Any] | None = None,
    viewport: tuple[int, int] | None = None,
) -> PlaywrightBrowserSession:
    try:
        driver = normalize_registration_driver(driver)
    except ValueError as exc:
        raise BrowserRegistrationError("unsupported_registration_driver") from exc
    if driver == "protocol":
        raise BrowserRegistrationError("unsupported_registration_driver", "protocol")
    config = _inject_browser_profile(config, driver, browser_identity)
    kwargs = {
        "proxy": proxy, "headless": headless, "timeout_ms": timeout_ms,
        "locale": locale, "timezone_id": timezone_id,
    }
    if driver == "playwright":
        kwargs["viewport"] = viewport
        profile = str(_driver_config(config, "playwright").get("user_data_dir") or "").strip()
        if profile:
            kwargs["user_data_dir"] = profile
        return PlaywrightBrowserSession(**kwargs)
    factory = _BROWSER_SESSION_FACTORIES.get(driver)
    if factory is None:
        raise BrowserRegistrationError("unsupported_registration_driver", driver)
    return factory(config=config, **kwargs)


__all__ = [
    "CamoufoxBrowserSession", "CloakBrowserSession", "RoxyBrowserSession",
    "AdsPowerBrowserSession", "create_browser_session", "verify_browser_proxy_country",
]
