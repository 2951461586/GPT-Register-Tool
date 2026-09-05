"""Stable seams shared by protocol and browser registration drivers.

Single source of truth
----------------------
Every place that needs the list of registration drivers used to hardcode it:
the ``RegistrationDriver`` enum values, ``BROWSER_REGISTRATION_DRIVERS``,
the alias table inside ``normalize_registration_driver``, the argparse
``choices`` in ``cli.py``, the ``supported_drivers`` set in ``config.py``
(``validate_registration_driver_config`` *and* the ``registration.driver``
check in ``validate_config``), and the dispatch if-chain in
``external_sessions.create_browser_session`` -- six-to-seven copies that
drifted whenever a driver was added. They now all derive from :data:`DRIVERS`
below. Adding, renaming, or aliasing a driver is a one-line edit here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RegistrationDriver(str, Enum):
    PROTOCOL = "protocol"
    PLAYWRIGHT = "playwright"
    ROXY = "roxy"
    CLOAK = "cloak"
    CAMOUFOX = "camoufox"
    ADSPOWER = "adspower"


@dataclass(frozen=True)
class BrowserDriverSpec:
    """Metadata for one registration driver, the single source of truth."""

    key: str
    # Every spelling (including the canonical ``key``) that resolves to this
    # driver. Consumed by ``normalize_registration_driver`` and the config
    # validators via :data:`KNOWN_DRIVER_ALIASES`.
    aliases: frozenset[str] = field(default_factory=frozenset)
    # ``protocol`` is the HTTP/API path and is not a browser driver; the rest
    # map 1:1 to a browser session class.
    is_browser: bool = True
    supports_headless: bool = True
    supports_context_reuse: bool = False
    supports_proxy_rotation: bool = True
    supports_browser_fetch: bool = True
    supports_profile_persistence: bool = True
    supports_crash_recovery: bool = True


# Canonical driver registry. This is the only place the driver vocabulary and
# its aliases are declared.
DRIVERS: dict[str, BrowserDriverSpec] = {
    RegistrationDriver.PROTOCOL.value: BrowserDriverSpec(
        "protocol",
        frozenset({"protocol", "api", "http"}),
        is_browser=False,
        supports_headless=False,
        supports_proxy_rotation=True,
        supports_browser_fetch=False,
        supports_profile_persistence=False,
        supports_crash_recovery=False,
    ),
    RegistrationDriver.PLAYWRIGHT.value: BrowserDriverSpec(
        "playwright", frozenset({"playwright", "pw"}),
        supports_context_reuse=True,
    ),
    RegistrationDriver.ROXY.value: BrowserDriverSpec(
        "roxy",
        frozenset({
            "roxy", "roxybrowser", "roxy_browser",
            "browser", "browser_registration", "fingerprint", "fingerprint_browser",
        }),
    ),
    RegistrationDriver.CLOAK.value: BrowserDriverSpec(
        "cloak", frozenset({"cloak", "cloakbrowser", "cloak_browser"}),
    ),
    RegistrationDriver.CAMOUFOX.value: BrowserDriverSpec(
        "camoufox", frozenset({"camoufox", "camou", "fox", "cf"}),
    ),
    RegistrationDriver.ADSPOWER.value: BrowserDriverSpec(
        "adspower", frozenset({"adspower", "adsp", "ap", "adspower_browser"}),
    ),
}


# Derived lists -- none of these are hand-maintained anymore.
BROWSER_REGISTRATION_DRIVERS = frozenset(k for k, s in DRIVERS.items() if s.is_browser)

_ALIAS_TO_KEY: dict[str, str] = {a: spec.key for spec in DRIVERS.values() for a in spec.aliases}

# Every accepted driver spelling, for config validators that only need a
# membership test (e.g. ``registration.driver``).
KNOWN_DRIVER_ALIASES = frozenset(_ALIAS_TO_KEY)


def normalize_registration_driver(value: Any = None, config: Mapping[str, Any] | None = None) -> str:
    """Resolve a supported registration driver and reject unknown names."""
    raw_value = value.value if isinstance(value, RegistrationDriver) else value
    raw = str(raw_value or "").strip().lower().replace("-", "_")
    if not raw and isinstance(config, Mapping):
        section = config.get("registration")
        if isinstance(section, Mapping):
            raw = str(section.get("driver") or "").strip().lower().replace("-", "_")
    canon = _ALIAS_TO_KEY.get(raw)
    if canon is not None:
        return canon
    if not raw:
        return RegistrationDriver.PROTOCOL.value
    raise ValueError(f"unsupported registration driver: {raw}")


def driver_choices() -> list[str]:
    """Argparse choices for ``--registration-driver`` (and any CLI mirror)."""
    return sorted(DRIVERS)


def driver_capabilities(value: Any = None, config: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """Return stable capability metadata for orchestration and diagnostics."""
    key = normalize_registration_driver(value, config)
    spec = DRIVERS[key]
    return {
        "is_browser": bool(spec.is_browser),
        "supports_headless": bool(spec.supports_headless),
        "supports_context_reuse": bool(spec.supports_context_reuse),
        "supports_proxy_rotation": bool(spec.supports_proxy_rotation),
        "supports_browser_fetch": bool(spec.supports_browser_fetch),
        "supports_profile_persistence": bool(spec.supports_profile_persistence),
        "supports_crash_recovery": bool(spec.supports_crash_recovery),
    }


class BrowserRegistrationError(RuntimeError):
    """Expected browser-flow failure with a stable, sanitized error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "browser_registration_failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}{': ' + self.detail if self.detail else ''}")
