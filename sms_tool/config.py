"""Deterministic application configuration loading and validation."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


# ---- Config sharding (proxy.json / runtime.json / payment.json) ----
# The historical single config.json is split into three focused shard files.
# Both the desktop shell and the Python backend merge these shards at load time,
# and a legacy config.json is migrated into shards on first load.
SHARD_FILES: dict[str, str] = {
    "proxy": "proxy.json",
    "runtime": "runtime.json",
    "payment": "payment.json",
}
# Top-level config key -> owning shard name. Every key present in config.json
# must be listed here so writes can be routed to the correct shard.
SHARD_OWNERSHIP: dict[str, str] = {
    # runtime.json
    "runtime": "runtime",
    "timeouts": "runtime",
    "storage": "runtime",
    "output": "runtime",
    "account_health": "runtime",
    "registration": "runtime",
    "chatgpt": "runtime",
    "email_registration": "runtime",
    "codex_oauth": "runtime",
    # proxy.json
    "proxy": "proxy",
    "mailbox_proxy": "proxy",
    "phone_reuse": "proxy",
    "paypal_browser": "proxy",
    # payment.json
    "paypal": "payment",
    "paypal_nocard": "payment",
    "upi": "payment",
    "omakse": "payment",
    "protocol_payments": "payment",
    "kakao": "payment",
    "momo": "payment",
    "cpa_mode": "payment",
    "sub2api": "payment",
}
_CONFIG_DIR = Path(__file__).resolve().parent.parent  # project root


def _deep_merge(target: dict, source: Mapping) -> None:
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _split_into_shards(data: Mapping[str, Any]) -> dict[str, dict]:
    shards: dict[str, dict] = {name: {} for name in SHARD_FILES}
    for key, value in data.items():
        owner = SHARD_OWNERSHIP.get(key, "runtime")
        shards[owner][key] = value
    return shards


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write JSON atomically: a temp file in the *same* directory + ``os.replace``.

    The temp file lives beside the target so ``os.replace`` is a same-volume
    rename (atomic on Windows, no truncated-file window). A ``.bak`` copy of
    the previous content is kept first, because the three shard files *are*
    the whole application configuration — a crash mid-write used to leave a
    truncated JSON and take the app down with it.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError:
            # Best-effort only; a missing/locked backup must not fail the write.
            pass
    prefix = "." + path.stem + "."
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(data), indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_shards(shards: Mapping[str, Mapping], config_dir: Path) -> None:
    for name, filename in SHARD_FILES.items():
        _atomic_write_json(config_dir / filename, dict(shards[name]))


def load_merged_config() -> dict[str, Any]:
    """Merge the proxy/runtime/payment shards into a single config dict.

    Honors a legacy single config.json by migrating it into shards on first
    load. Returns {} when no configuration exists.
    """
    shard_paths = [(name, _CONFIG_DIR / filename) for name, filename in SHARD_FILES.items()]
    if any(path.exists() for _, path in shard_paths):
        merged: dict[str, Any] = {}
        for _, path in shard_paths:
            if not path.exists():
                continue
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                _deep_merge(merged, data)
        return merged
    legacy = _CONFIG_DIR / "config.json"
    if legacy.exists():
        with open(legacy, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _write_shards(_split_into_shards(data), _CONFIG_DIR)
            return dict(data)
    return {}




def validate_registration_driver_config(
    config: Mapping[str, Any],
    driver: Any = None,
    *,
    proxy: Any = None,
) -> str:
    """Validate credentials required by the selected browser driver.

    Static ``validate_config`` checks types and URL shapes, but cannot require
    credentials for every optional driver.  This focused preflight is called at
    the registration boundary so a missing cloud/API setting is reported before
    a disposable mailbox is claimed or a browser session is created.
    """
    from .registration_drivers.base import normalize_registration_driver

    selected = normalize_registration_driver(driver, config)
    if selected == "protocol":
        return selected

    registration = config.get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    drivers = registration.get("drivers")
    drivers = drivers if isinstance(drivers, Mapping) else {}
    selected_config = drivers.get(selected)
    selected_config = selected_config if isinstance(selected_config, Mapping) else {}
    # Use the same environment-overlay logic as the runtime session factory so
    # preflight does not reject a driver whose credentials live in deployment
    # environment variables.
    try:
        from .registration_drivers.external_sessions import _driver_config

        selected_config = _driver_config(config, selected)
    except Exception:
        selected_config = dict(selected_config)

    # Cloud browser secrets may be injected by deployment environments.  Keep
    # environment precedence at the validation boundary without mutating the
    # persisted JSON configuration.
    requirements = {
        "roxy": (("workspace_id", "roxy_workspace_id_missing"),),
    }
    for key, error_code in requirements.get(selected, ()):
        configured = selected_config.get(key)
        if not str(configured or "").strip():
            raise ConfigError(error_code)
    # ``proxy`` stays in the signature for call-site stability; every remaining
    # driver consumes the registration proxy locally, so there is no
    # "provider-native proxy setting required" case left to validate.
    return selected


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    data: Mapping[str, Any]
    source: Path

    def workflow(self, name: str) -> Mapping[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, Mapping) else MappingProxyType({})

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self.data)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source: str | Path = "<injected>",
        validate: bool = True,
    ) -> "RuntimeConfig":
        if validate:
            validate_config(value)
        return cls(data=_freeze(_thaw(value)), source=Path(source))


ConfigInput = RuntimeConfig | Mapping[str, Any] | None
_CURRENT_CONFIG: ContextVar[RuntimeConfig | None] = ContextVar("sms_tool_runtime_config", default=None)


def resolve_runtime_config(value: ConfigInput = None, *, workflow: str | None = None) -> RuntimeConfig:
    config = value if isinstance(value, RuntimeConfig) else (
        RuntimeConfig.from_mapping(value, validate=False)
        if isinstance(value, Mapping)
        else (_CURRENT_CONFIG.get() or default_runtime_config())
    )
    validate_config(config.data, workflow=workflow)
    return config


def current_runtime_config() -> RuntimeConfig:
    return _CURRENT_CONFIG.get() or default_runtime_config()


def current_config_data() -> Mapping[str, Any]:
    return current_runtime_config().data


@contextmanager
def runtime_config_scope(value: ConfigInput, *, workflow: str | None = None):
    config = resolve_runtime_config(value, workflow=workflow)
    token = _CURRENT_CONFIG.set(config)
    try:
        yield config
    finally:
        _CURRENT_CONFIG.reset(token)


class LegacyConfigView(MutableMapping[str, Any]):
    """Compatibility view that reads from the injected RuntimeConfig.

    Local overrides exist only for legacy tests/integrations that mutate CFG;
    production reads always follow the ContextVar-backed application scope.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return _thaw(current_config_data()[key])

    def __setitem__(self, key: str, value: Any) -> None:
        self._overrides[str(key)] = value

    def __delitem__(self, key: str) -> None:
        if key in self._overrides:
            del self._overrides[key]
            return
        raise KeyError(key)

    def __iter__(self):
        return iter(dict.fromkeys((*current_config_data().keys(), *self._overrides.keys())))

    def __len__(self) -> int:
        return len(set(current_config_data()) | set(self._overrides))

    def copy(self) -> dict[str, Any]:
        # unittest.mock.patch.dict must restore only explicit overrides.
        return dict(self._overrides)

    def clear(self) -> None:
        self._overrides.clear()


def default_config_path() -> Path:
    """Resolve config independently of the process current directory."""
    package_dir = Path(__file__).resolve().parent
    project_file = package_dir.parent / "config.json"
    package_file = package_dir / "config.json"
    return project_file if project_file.is_file() else package_file


def load_runtime_config(path: str | Path | None = None, *, validate: bool = True) -> RuntimeConfig:
    if path is None:
        # Merge the proxy/runtime/payment shards (migrating a legacy single
        # config.json on first load). This is the canonical desktop + backend path.
        raw = load_merged_config()
        if not isinstance(raw, dict):
            raise ConfigError("merged config root must be a JSON object")
        if validate:
            validate_config(raw)
        return RuntimeConfig(data=_freeze(raw), source=_CONFIG_DIR)
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"config file not found: {source}")
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid config file {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    if validate:
        validate_config(raw)
    if source == Path(__file__).resolve().parent / "config.json":
        # The bundled package config is a minimal safe fallback (endpoints and
        # paths only). Running on it means the project-root config.json is
        # missing, so say so loudly instead of silently flipping behavior.
        print(
            f"[!] Using the bundled fallback config {source}; "
            f"create a project-root config (see config.example.json) for full behavior",
            file=sys.stderr,
        )
    return RuntimeConfig(data=_freeze(raw), source=source)


@lru_cache(maxsize=1)
def default_runtime_config() -> RuntimeConfig:
    """Load the deterministic default only when an application asks for it."""
    return load_runtime_config()


def initialize_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    """Parse, validate, and activate configuration at an application boundary."""
    config = load_runtime_config(path)
    _CURRENT_CONFIG.set(config)
    return config


def validate_config(config: Mapping[str, Any], *, workflow: str | None = None) -> None:
    """Validate static workflow inputs before network or subprocess execution."""
    errors: list[str] = []
    chatgpt = config.get("chatgpt")
    if not isinstance(chatgpt, Mapping):
        errors.append("chatgpt must be an object")
    else:
        for key in ("auth_base_url", "chat_base_url"):
            value = str(chatgpt.get(key) or "").strip()
            if value and urlsplit(value).scheme not in {"http", "https"}:
                errors.append(f"chatgpt.{key} must be an http(s) URL")

    proxy = config.get("proxy", {})
    if proxy is not None and not isinstance(proxy, Mapping):
        errors.append("proxy must be an object")
    if isinstance(proxy, Mapping):
        pool = proxy.get("pool", [])
        if pool is not None and not isinstance(pool, (list, tuple)):
            errors.append("proxy.pool must be an array")
        for key in (
            "browser_registration_pool",
            "browser_pool",
            "protocol_registration_pool",
            "protocol_pool",
        ):
            if key in proxy and not isinstance(proxy.get(key), (str, list, tuple)):
                errors.append(f"proxy.{key} must be a proxy list")
        if "health" in proxy and not isinstance(proxy.get("health"), (str, list, tuple)):
            errors.append("proxy.health must be a proxy list")

    account_health = config.get("account_health", {})
    if account_health is not None and not isinstance(account_health, Mapping):
        errors.append("account_health must be an object")
    if isinstance(account_health, Mapping):
        if "proxy_pool" in account_health and not isinstance(
            account_health.get("proxy_pool"), (str, list, tuple)
        ):
            errors.append("account_health.proxy_pool must be a proxy list")
        health_proxies = account_health.get("proxies", {})
        if health_proxies is not None and not isinstance(health_proxies, Mapping):
            errors.append("account_health.proxies must be an object")
        elif isinstance(health_proxies, Mapping):
            supported_health_lanes = {
                "liveness", "liveness_pool", "quota_pool",
                "promotion", "promotion_pool",
                "browser", "browser_pool", "browser_verification_pool",
            }
            unknown_health_lanes = sorted(set(health_proxies) - supported_health_lanes)
            if unknown_health_lanes:
                errors.append(
                    "unsupported account_health proxy lane: "
                    + ", ".join(unknown_health_lanes)
                )
            for key, value in health_proxies.items():
                if not isinstance(value, (str, list, tuple)):
                    errors.append(f"account_health.proxies.{key} must be a proxy list")

    registration = config.get("registration", {})
    if registration is not None and not isinstance(registration, Mapping):
        errors.append("registration must be an object")
    if isinstance(registration, Mapping):
        driver = str(registration.get("driver") or "protocol").strip().lower().replace("-", "_")
        if driver not in {
            "protocol", "api", "http", "playwright", "pw",
            "browser", "browser_registration", "fingerprint", "fingerprint_browser",
            "roxy", "roxybrowser", "roxy_browser", "cloak", "cloakbrowser", "cloak_browser",
            "camoufox", "camou", "fox", "cf",
        }:
            errors.append("registration.driver is unsupported")
        _validate_positive_numbers(registration, (
            "retry_attempts", "retry_delay_seconds", "at_stability_probe_count",
            "at_stability_probe_delay_seconds", "at_probe_timeout_seconds", "browser_timeout_seconds",
        ), "registration", errors)
        if "browser_headless" in registration and not isinstance(registration.get("browser_headless"), bool):
            errors.append("registration.browser_headless must be a boolean")
        for key in ("browser_locale", "browser_timezone"):
            if key in registration and not str(registration.get(key) or "").strip():
                errors.append(f"registration.{key} must not be blank")
        drivers = registration.get("drivers", {})
        if drivers is not None and not isinstance(drivers, Mapping):
            errors.append("registration.drivers must be an object")
        elif isinstance(drivers, Mapping):
            supported_drivers = {"roxy", "cloak", "playwright", "camoufox", "adspower"}
            unknown_drivers = sorted(set(drivers) - supported_drivers)
            if unknown_drivers:
                errors.append(f"unsupported registration driver config: {', '.join(unknown_drivers)}")
            for name, raw in drivers.items():
                if not isinstance(raw, Mapping):
                    errors.append(f"registration.drivers.{name} must be an object")
                    continue
                for key in ("api_base", "cdp_base", "start_url"):
                    value = str(raw.get(key) or "").strip()
                    if value and urlsplit(value).scheme not in {"http", "https", "ws", "wss"}:
                        errors.append(f"registration.drivers.{name}.{key} must be a URL")
                _validate_positive_numbers(
                    raw,
                    ("session_timeout_minutes",),
                    f"registration.drivers.{name}",
                    errors,
                )
                for key in (
                    "use_proxy", "humanize", "geoip", "keep_browser_open",
                    "delete_profile_after_run", "generate_browser_profile", "ad_blocker",
                ):
                    if key in raw and not isinstance(raw.get(key), bool):
                        errors.append(f"registration.drivers.{name}.{key} must be a boolean")
        stage_timeouts = registration.get("stage_timeouts", {})
        if stage_timeouts is not None and not isinstance(stage_timeouts, Mapping):
            errors.append("registration.stage_timeouts must be an object")
        elif isinstance(stage_timeouts, Mapping):
            valid_stages = {
                "sentinel", "identity_ready", "auth_flow", "user_register",
                "email_otp_send", "email_otp_wait", "email_otp_validate",
                "create_account", "auth_session", "codex_oauth",
                "access_token_probe", "totp_enroll", "finalize",
            }
            unknown_stages = sorted(set(stage_timeouts) - valid_stages)
            if unknown_stages:
                errors.append(f"unsupported registration stage timeout: {', '.join(unknown_stages)}")
            _validate_positive_numbers(
                stage_timeouts,
                tuple(str(key) for key in stage_timeouts),
                "registration.stage_timeouts",
                errors,
            )
        pulse = registration.get("pulse", {})
        if pulse is not None and not isinstance(pulse, Mapping):
            errors.append("registration.pulse must be an object")
        elif isinstance(pulse, Mapping):
            if "enabled" in pulse and not isinstance(pulse.get("enabled"), bool):
                errors.append("registration.pulse.enabled must be a boolean")
            _validate_positive_numbers(
                pulse,
                ("wave_size", "wave_delay_seconds", "ban_threshold",
                 "ban_pause_seconds", "max_waves"),
                "registration.pulse",
                errors,
            )
        process_pool = registration.get("browser_process_pool", {})
        if process_pool is not None and not isinstance(process_pool, Mapping):
            errors.append("registration.browser_process_pool must be an object")
        elif isinstance(process_pool, Mapping):
            # NOTE: deliberately NOT named ``browser_pool`` -- that key is a
            # proxy-pool alias (see proxy_routing.PROXY_LANE_ALIASES); reusing
            # it here would make every future grep ambiguous.
            for key in ("enabled", "recycle_on_error"):
                if key in process_pool and not isinstance(process_pool.get(key), bool):
                    errors.append(f"registration.browser_process_pool.{key} must be a boolean")
            _validate_positive_numbers(
                process_pool,
                ("max_concurrent", "max_uses_per_process"),
                "registration.browser_process_pool",
                errors,
            )

    email = config.get("email_registration", {})
    if email is not None and not isinstance(email, Mapping):
        errors.append("email_registration must be an object")
    if isinstance(email, Mapping):
        _validate_positive_numbers(email, ("otp_timeout", "otp_poll_interval"), "email_registration", errors)

    payments = config.get("protocol_payments", {})
    if payments is not None and not isinstance(payments, Mapping):
        errors.append("protocol_payments must be an object")
    if isinstance(payments, Mapping):
        _validate_payment_config(payments, errors)

    if workflow and workflow not in {"registration", "protocol_payments", "payment", "mailbox", "storage"}:
        errors.append(f"unknown workflow: {workflow}")
    if errors:
        raise ConfigError("; ".join(errors))


def _validate_positive_numbers(section: Mapping[str, Any], keys: tuple[str, ...], prefix: str, errors: list[str]) -> None:
    for key in keys:
        if key not in section:
            continue
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f"{prefix}.{key} must be a non-negative number")


def _validate_payment_config(section: Mapping[str, Any], errors: list[str]) -> None:
    from .payment_catalog import PAYMENT_CATALOG, normalize_payment_method
    from .payment_flow import normalize_payment_stage
    supported = set(PAYMENT_CATALOG.methods)
    enabled = section.get("enabled_methods", [])
    if enabled is not None and not isinstance(enabled, (list, tuple)):
        errors.append("protocol_payments.enabled_methods must be an array")
    elif enabled:
        unknown = sorted({str(item) for item in enabled} - supported)
        if unknown:
            errors.append(f"unsupported protocol payment methods: {', '.join(unknown)}")
    methods = section.get("methods", {})
    proxy_pools = section.get("proxy_pools", {})
    if proxy_pools is not None and not isinstance(proxy_pools, Mapping):
        errors.append("protocol_payments.proxy_pools must be an object")
        proxy_pools = {}
    elif isinstance(proxy_pools, Mapping):
        for name, value in proxy_pools.items():
            if not str(name or "").strip():
                errors.append("protocol_payments.proxy_pools names must not be blank")
            if not isinstance(value, (str, list, tuple, Mapping)):
                errors.append(f"protocol_payments.proxy_pools.{name} must be a proxy list")
    if methods is not None and not isinstance(methods, Mapping):
        errors.append("protocol_payments.methods must be an object")
    elif isinstance(methods, Mapping):
        unknown = sorted(set(methods) - supported)
        if unknown:
            errors.append(f"unsupported protocol payment method config: {', '.join(unknown)}")
        known_pools = set(proxy_pools) if isinstance(proxy_pools, Mapping) else set()
        for method, raw in methods.items():
            if not isinstance(raw, Mapping):
                errors.append(f"protocol_payments.methods.{method} must be an object")
                continue
            flow_profile = raw.get("flow_profile")
            if flow_profile is not None and not str(flow_profile or "").strip():
                errors.append(f"protocol_payments.methods.{method}.flow_profile must not be blank")
            stages = raw.get("stages")
            if stages is not None and not isinstance(stages, (list, tuple)):
                errors.append(f"protocol_payments.methods.{method}.stages must be an array")
            elif isinstance(stages, (list, tuple)):
                invalid = [str(stage) for stage in stages if not normalize_payment_stage(stage)]
                if invalid:
                    errors.append(f"protocol_payments.methods.{method}.stages contains unsupported stages: {', '.join(invalid)}")
            routes = raw.get("stage_routes")
            if routes is not None and not isinstance(routes, Mapping):
                errors.append(f"protocol_payments.methods.{method}.stage_routes must be an object")
            elif isinstance(routes, Mapping):
                for stage, route in routes.items():
                    prefix = f"protocol_payments.methods.{method}.stage_routes.{stage}"
                    if not normalize_payment_stage(stage):
                        errors.append(f"{prefix} uses an unsupported stage")
                        continue
                    route_value = route if isinstance(route, Mapping) else {"pool": route}
                    pool = str(route_value.get("pool") or "").strip()
                    if pool and pool not in known_pools and pool not in {"checkout", "approve", "default"}:
                        errors.append(f"{prefix}.pool references unknown proxy pool: {pool}")
                    country = str(route_value.get("country") or "").strip()
                    if country and (len(country) != 2 or not country.isalpha()):
                        errors.append(f"{prefix}.country must be ISO alpha-2")
    _validate_positive_numbers(section, ("timeout_seconds",), "protocol_payments", errors)
    matrix = section.get("matrix", {})
    if matrix is not None and not isinstance(matrix, Mapping):
        errors.append("protocol_payments.matrix must be an object")
    elif isinstance(matrix, Mapping):
        cells = matrix.get("cells", [])
        if cells is not None and not isinstance(cells, (list, tuple)):
            errors.append("protocol_payments.matrix.cells must be an array")
        names: set[str] = set()
        for index, cell in enumerate(cells or []):
            if not isinstance(cell, Mapping):
                errors.append(f"protocol_payments.matrix.cells[{index}] must be an object")
                continue
            name = str(cell.get("name") or "").strip()
            if not name:
                errors.append(f"protocol_payments.matrix.cells[{index}].name is required")
            elif name in names:
                errors.append(f"duplicate protocol payment matrix cell name: {name}")
            names.add(name)
            method = normalize_payment_method(cell.get("payment_method"), default_for_blank=False)
            if not method:
                errors.append(f"protocol_payments.matrix.cells[{index}].payment_method is unsupported")
            sample_size = cell.get("sample_size", 1)
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 1:
                errors.append(f"protocol_payments.matrix.cells[{index}].sample_size must be a positive integer")
            for key in sorted(key for key in cell if str(key).endswith("_country")):
                value = str(cell.get(key) or "").strip()
                if value and (len(value) != 2 or not value.isalpha()):
                    errors.append(f"protocol_payments.matrix.cells[{index}].{key} must be ISO alpha-2")
            if method:
                checkout_country = str(cell.get("checkout_country") or "").strip().upper()
                expected_country = PAYMENT_CATALOG.methods[method].country
                if checkout_country and checkout_country != expected_country:
                    errors.append(
                        f"protocol_payments.matrix.cells[{index}].checkout_country must be {expected_country} for {method}"
                    )


# CFG is a mutable-shape compatibility view for existing modules and tests.
# It performs no import-time I/O and follows the RuntimeConfig active in the
# current workflow context.
CFG: MutableMapping[str, Any] = LegacyConfigView()


def _load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Compatibility loader returning a detached dictionary."""
    return load_runtime_config(path).as_dict()


load_config = load_runtime_config
