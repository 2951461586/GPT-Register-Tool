"""Deterministic application configuration loading and validation."""

from __future__ import annotations

import json
import sys
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
    source = Path(path).expanduser().resolve() if path else default_config_path()
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
    if path is None and source == Path(__file__).resolve().parent / "config.json":
        # The bundled package config is a minimal safe fallback (endpoints and
        # paths only). Running on it means the project-root config.json is
        # missing, so say so loudly instead of silently flipping behavior.
        print(
            f"[!] Using the bundled fallback config {source}; "
            f"create a project-root config.json (see config.example.json) for full behavior",
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

    registration = config.get("registration", {})
    if registration is not None and not isinstance(registration, Mapping):
        errors.append("registration must be an object")
    if isinstance(registration, Mapping):
        _validate_positive_numbers(registration, (
            "retry_attempts", "retry_delay_seconds", "at_stability_probe_count",
            "at_stability_probe_delay_seconds", "at_probe_timeout_seconds",
        ), "registration", errors)
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
