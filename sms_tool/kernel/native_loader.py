"""Shared loader for optional ``rust-anything`` PyO3 acceleration modules.

Every Python module with a Rust hot-path port under ``rust/<crate>/python``
previously duplicated a 15-line ``_load_native_*`` helper.  Centralizing it
in the kernel keeps ``sys.path`` hygiene and ``RUST_ANYTHING_MODE`` /
``RUST_ANYTHING_UNIT_*`` fallback semantics in one place without introducing
any application-layer imports.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OLD_MODES = frozenset({"old", "original"})
_NEW_MODES = frozenset({"new", "rust"})

#: Scalar types safely accepted by generated PyO3 string/number wrappers.
NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)


def load_native_module(crate_dir: str, module_name: str, unit_env: str) -> Any | None:
    """Import ``module_name`` from ``rust/<crate_dir>/python`` when enabled.

    Returns ``None`` when ``RUST_ANYTHING_MODE`` or ``unit_env`` requests the
    pure-Python path (``old``/``original``), or when the compiled extension is
    unavailable (for example in CI without prebuilt ``.pyd`` binaries).
    Re-raises ``ImportError`` / ``OSError`` when ``new``/``rust`` mode is
    explicitly forced.
    """
    mode = str(os.environ.get("RUST_ANYTHING_MODE") or "").strip().lower()
    unit_mode = str(os.environ.get(unit_env) or "").strip().lower()
    if mode in _OLD_MODES or unit_mode in _OLD_MODES:
        return None
    strict = mode in _NEW_MODES or unit_mode in _NEW_MODES
    pkg_dir = _REPO_ROOT / "rust" / crate_dir / "python"
    if not pkg_dir.is_dir():
        if strict:
            raise ImportError(f"{module_name} not found at {pkg_dir}")
        return None
    pkg_str = str(pkg_dir)
    inserted = False
    if pkg_str not in sys.path:
        sys.path.insert(0, pkg_str)
        inserted = True
    try:
        return importlib.import_module(module_name)
    except (ImportError, OSError):
        if strict:
            raise
        return None
    finally:
        if inserted and pkg_str in sys.path:
            sys.path.remove(pkg_str)
