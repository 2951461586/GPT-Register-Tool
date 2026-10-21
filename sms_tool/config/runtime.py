"""The resolved runtime view of the configuration.

:class:`RuntimeConfig` is the frozen, validated snapshot the application reads;
the ``ContextVar`` makes "the config for *this* workflow" explicit instead of a
module global.  Loading lives here rather than in the loader because a
``RuntimeConfig`` is what a load produces -- the loader owns the files, this
module owns the value.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ._state import config_dir as _config_dir
from ._state import package_dir as _package_dir
from .loader import load_merged_config
from .schema import ConfigError, _log_config_warnings, validate_config


def _freeze(value: Any) -> Any:
    """Deep-freeze a config tree: ``dict`` -> ``MappingProxyType``, ``list`` -> ``tuple``.

    🔴 **Consumers must test a config section with ``isinstance(x, Mapping)``,
    never ``isinstance(x, dict)``.** Every mapping produced here is a
    ``MappingProxyType`` -- a ``Mapping`` that is **not** a ``dict``. A ``dict``
    guard therefore reads as "section missing" in production while plain-``dict``
    test fixtures keep it green (the 2026-10-07 P1-D class: three registration
    toggles silently disabled, and the smailr section silently empty).

    Scope of the rule: the **frozen surfaces** -- ``current_config_data()``,
    ``resolve_runtime_config(...).data`` and ``RuntimeConfig.data``.
    ``CFG`` (``LegacyConfigView``) is the exception: its ``__getitem__``/``get``
    call ``_thaw`` and hand back plain dicts, so ``CFG.get(...)`` *may* be
    dict-tested (that is why ``scripts/batch_enable_2fa.py``'s
    ``isinstance(CFG.get("chatgpt"), dict)`` is safe, not a P1-D bug).
    """
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
    config = (
        value
        if isinstance(value, RuntimeConfig)
        else (
            RuntimeConfig.from_mapping(value, validate=False)
            if isinstance(value, Mapping)
            else (_CURRENT_CONFIG.get() or default_runtime_config())
        )
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


def load_runtime_config(path: str | Path | None = None, *, validate: bool = True) -> RuntimeConfig:
    if path is None:
        # Merge the proxy/runtime/payment shards (migrating a legacy single
        # config.json on first load). This is the canonical desktop + backend path.
        raw = load_merged_config()
        if not isinstance(raw, dict):
            raise ConfigError("merged config root must be a JSON object")
        if validate:
            validate_config(raw)
        _log_config_warnings(raw)
        return RuntimeConfig(data=_freeze(raw), source=_config_dir())
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
    _log_config_warnings(raw)
    if source == _package_dir() / "config.json":
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


def _load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Compatibility loader returning a detached dictionary."""
    return load_runtime_config(path).as_dict()


load_config = load_runtime_config
