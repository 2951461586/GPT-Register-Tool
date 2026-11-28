"""Registration driver vocabulary and environment overlay.

Why this module exists
----------------------
``sms_tool.config`` (imported by ~70 modules, the dependency-inversion hub of
the package) needs both the canonical registration-driver vocabulary
(:class:`RegistrationDriver`, :data:`DRIVERS`, :data:`KNOWN_DRIVER_ALIASES`,
:func:`normalize_registration_driver`) and the deployment environment-overlay
logic (:func:`driver_config`). Previously ``driver_config`` lived here while
the driver registry lived in ``registration_drivers/base.py``, forcing
``config/schema.py`` to keep three delayed upward imports into
``registration_drivers`` and ``registration/orchestrator.py`` to import
``registration_drivers/base.py`` at module scope just for driver-name
normalization.

Both datasets are pure stdlib tables with zero browser or transport dependencies,
so they live together in the kernel below ``config/``, ``registration/``, and
``registration_drivers/``. ``registration_drivers/base.py`` re-exports every
symbol for call-site compatibility.

Adding a driver
---------------
1. Add its :class:`RegistrationDriver` member and :data:`DRIVERS` entry below.
2. Add its environment-variable map to :data:`DRIVER_ENV_OVERRIDES` if needed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .env_loader import ensure_loaded


class RegistrationDriver(str, Enum):
    PROTOCOL = "protocol"
    PLAYWRIGHT = "playwright"
    ROXY = "roxy"
    CLOAK = "cloak"
    CAMOUFOX = "camoufox"


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
        "playwright",
        frozenset({"playwright", "pw"}),
        supports_context_reuse=True,
    ),
    RegistrationDriver.ROXY.value: BrowserDriverSpec(
        "roxy",
        frozenset(
            {
                "roxy",
                "roxybrowser",
                "roxy_browser",
                "browser",
                "browser_registration",
                "fingerprint",
                "fingerprint_browser",
            }
        ),
    ),
    RegistrationDriver.CLOAK.value: BrowserDriverSpec(
        "cloak",
        frozenset({"cloak", "cloakbrowser", "cloak_browser"}),
    ),
    RegistrationDriver.CAMOUFOX.value: BrowserDriverSpec(
        "camoufox",
        frozenset({"camoufox", "camou", "fox", "cf"}),
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


# driver name -> {config key: (environment variable, value type)}
# value type is one of: str, bool, int, float, json
DRIVER_ENV_OVERRIDES: dict[str, dict[str, tuple[str, str]]] = {
    "roxy": {
        "api_token": ("ROXY_API_TOKEN", "str"),
        "api_base": ("ROXY_API_BASE", "str"),
        "profile_id": ("ROXY_PROFILE_ID", "str"),
        "workspace_id": ("ROXY_WORKSPACE_ID", "str"),
        "project_id": ("ROXY_PROJECT_ID", "str"),
        "workspace_list_path": ("ROXY_WORKSPACE_LIST_PATH", "str"),
        "open_path": ("ROXY_OPEN_PATH", "str"),
        "open_method": ("ROXY_OPEN_METHOD", "str"),
        "open_headless": ("ROXY_OPEN_HEADLESS", "bool"),
        "close_path": ("ROXY_CLOSE_PATH", "str"),
        "close_method": ("ROXY_CLOSE_METHOD", "str"),
        "delete_path": ("ROXY_DELETE_PATH", "str"),
        "delete_method": ("ROXY_DELETE_METHOD", "str"),
        "keep_browser_open": ("ROXY_KEEP_BROWSER_OPEN", "bool"),
        "delete_profile_after_run": ("ROXY_DELETE_PROFILE_AFTER_RUN", "bool"),
        "api_retries": ("ROXY_API_RETRIES", "int"),
        "api_retry_delay_seconds": ("ROXY_API_RETRY_DELAY", "float"),
        "backend": ("ROXY_BACKEND", "str"),
        "start_url": ("ROXY_START_URL", "str"),
        "headless": ("ROXY_HEADLESS", "bool"),
    },
    "cloak": {
        "license_key": ("CLOAK_LICENSE_KEY", "str"),
        "headless": ("CLOAK_HEADLESS", "bool"),
        "humanize": ("CLOAK_HUMANIZE", "bool"),
        "geoip": ("CLOAK_GEOIP", "bool"),
        "locale": ("CLOAK_LOCALE", "str"),
        "timezone": ("CLOAK_TIMEZONE", "str"),
        "use_proxy": ("CLOAK_USE_PROXY", "bool"),
        "fingerprint_seed": ("CLOAK_FINGERPRINT_SEED", "str"),
        "user_data_dir": ("CLOAK_USER_DATA_DIR", "str"),
        "keep_browser_open": ("CLOAK_KEEP_BROWSER_OPEN", "bool"),
        "start_url": ("CLOAK_START_URL", "str"),
    },
    "camoufox": {
        "headless": ("CAMOUFOX_HEADLESS", "bool"),
        "humanize": ("CAMOUFOX_HUMANIZE", "bool"),
        "geoip": ("CAMOUFOX_GEOIP", "bool"),
        "locale": ("CAMOUFOX_LOCALE", "str"),
        "timezone": ("CAMOUFOX_TIMEZONE", "str"),
        "use_proxy": ("CAMOUFOX_USE_PROXY", "bool"),
        "user_data_dir": ("CAMOUFOX_USER_DATA_DIR", "str"),
        "keep_browser_open": ("CAMOUFOX_KEEP_BROWSER_OPEN", "bool"),
        "start_url": ("CAMOUFOX_START_URL", "str"),
        "max_width": ("CAMOUFOX_MAX_WIDTH", "int"),
        "max_height": ("CAMOUFOX_MAX_HEIGHT", "int"),
    },
}


def driver_config(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return one driver's config with deployment env vars layered on top.

    Secrets stay out of the persisted JSON and out of diagnostic payloads: the
    JSON value is the base, the environment wins when it is set and parseable.
    An unparseable environment value is ignored rather than fatal - the module
    must stay importable and a bad optional override must not break registration.
    """
    ensure_loaded()
    registration = config.get("registration")
    drivers = registration.get("drivers") if isinstance(registration, Mapping) else {}
    value = drivers.get(name) if isinstance(drivers, Mapping) else {}
    result = dict(value) if isinstance(value, Mapping) else {}

    for key, (env_name, value_type) in DRIVER_ENV_OVERRIDES.get(name, {}).items():
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        try:
            if value_type == "bool":
                normalized = text.lower()
                if normalized in {"1", "true", "yes", "on", "y"}:
                    result[key] = True
                elif normalized in {"0", "false", "no", "off", "n"}:
                    result[key] = False
                else:
                    continue
            elif value_type == "int":
                result[key] = int(text)
            elif value_type == "float":
                result[key] = float(text)
            elif value_type == "json":
                parsed = json.loads(text)
                if isinstance(parsed, Mapping):
                    result[key] = dict(parsed)
            else:
                result[key] = text
        except (TypeError, ValueError, json.JSONDecodeError):
            # Invalid optional environment values must not break importing the
            # module; the JSON/config value remains authoritative instead.
            continue
    return result
