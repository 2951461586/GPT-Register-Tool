from __future__ import annotations

from typing import Any
import os
import sys


def _emit(step: str, msg: str, **kw: Any) -> None:
    """Top-level progress/error sink (sunk copy; see ``gen_pp_link._emit``)."""
    print(f"[{step}] {msg}", file=sys.stderr)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """解析浮点环境变量。非法值 / 空值回落到默认值，并夹到下界。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, float(raw))
    except ValueError:
        return max(minimum, default)
