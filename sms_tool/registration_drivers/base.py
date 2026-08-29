"""Stable seams shared by protocol and browser registration drivers."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class RegistrationDriver(str, Enum):
    PROTOCOL = "protocol"
    PLAYWRIGHT = "playwright"
    ROXY = "roxy"
    CLOAK = "cloak"
    CAMOUFOX = "camoufox"
    ADSPOWER = "adspower"


BROWSER_REGISTRATION_DRIVERS = frozenset({
    RegistrationDriver.PLAYWRIGHT.value,
    RegistrationDriver.ROXY.value,
    RegistrationDriver.CLOAK.value,
    RegistrationDriver.CAMOUFOX.value,
    RegistrationDriver.ADSPOWER.value,
})


def normalize_registration_driver(value: Any = None, config: Mapping[str, Any] | None = None) -> str:
    """Resolve a supported registration driver and reject unknown names."""
    raw_value = value.value if isinstance(value, RegistrationDriver) else value
    raw = str(raw_value or "").strip().lower().replace("-", "_")
    if not raw and isinstance(config, Mapping):
        section = config.get("registration")
        if isinstance(section, Mapping):
            raw = str(section.get("driver") or "").strip().lower().replace("-", "_")
    if not raw or raw in {"protocol", "api", "http"}:
        return RegistrationDriver.PROTOCOL.value
    if raw in {"playwright", "pw"}:
        return RegistrationDriver.PLAYWRIGHT.value
    if raw in {
        "browser", "browser_registration", "fingerprint", "fingerprint_browser",
        "roxy", "roxybrowser", "roxy_browser",
    }:
        return RegistrationDriver.ROXY.value
    if raw in {"cloak", "cloakbrowser", "cloak_browser"}:
        return RegistrationDriver.CLOAK.value
    if raw in {"camoufox", "camou", "fox", "cf"}:
        return RegistrationDriver.CAMOUFOX.value
    if raw in {"adspower", "adsp", "ap", "adspower_browser"}:
        return RegistrationDriver.ADSPOWER.value
    raise ValueError(f"unsupported registration driver: {raw}")


class BrowserRegistrationError(RuntimeError):
    """Expected browser-flow failure with a stable, sanitized error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "browser_registration_failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}{': ' + self.detail if self.detail else ''}")
