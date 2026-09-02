"""Environment overlay for registration driver configuration.

Why this module exists
----------------------
``config.validate_registration_driver_config`` needs the *same*
environment-overlay logic that the runtime session factory uses, otherwise
preflight rejects a driver whose credentials only exist as deployment
environment variables. It used to import that function from
``registration_drivers/external_sessions``, which made ``sms_tool.config`` -
imported by 64 modules, the dependency-inversion hub of the whole package -
depend on a browser driver module. That edge is a cycle: the driver package
imports config indirectly for proxy and runtime settings.

The logic is self-contained (it reads ``os.environ`` and one config mapping), so
it lives here, below both of its consumers. Neither side reaches sideways any
more.

Adding a driver
---------------
Add its table to :data:`DRIVER_ENV_OVERRIDES`. Nothing else needs to change -
that table is the only place the env-var names are declared.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .env_loader import ensure_loaded

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
    "adspower": {
        "api_base": ("ADSPOWER_API_BASE", "str"),
        "user_id": ("ADSPOWER_USER_ID", "str"),
        "headless": ("ADSPOWER_HEADLESS", "bool"),
        "keep_browser_open": ("ADSPOWER_KEEP_BROWSER_OPEN", "bool"),
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
