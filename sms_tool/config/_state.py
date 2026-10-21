"""Mutable module-level state for :mod:`sms_tool.config`.

Why this is its own module
--------------------------
``CONFIG_DIR`` is resolved **once**, at import, from ``kernel.paths.config_dir``.
That is what makes it a seam: tests and portable installs point it elsewhere
with ``patch.object(config_state, "CONFIG_DIR", tmp_path)``.  It lives in its own
module and every submodule reads it through the module object
(``_state.CONFIG_DIR``) rather than importing the value, because a
``from ._state import CONFIG_DIR`` would freeze the value at import time and
silently make the seam inert.

``PACKAGE_DIR`` is the ``sms_tool`` package directory -- the parent of *this*
directory, not ``Path(__file__).parent``.  The bundled fallback ``config.json``
ships beside it, and both :func:`..loader.default_config_path` and
:func:`..runtime.load_runtime_config` compare against it; taking ``.parent`` here
would silently point them at ``sms_tool/config/`` instead.

The accessors below exist so the submodules can reach the state **without**
writing ``from . import _state``.  That form resolves to an edge on the package
itself, and ``sms_tool.config.__init__`` imports every submodule, so the two
together are a package facade cycle -- the same shape the 2026-10-21 cycle work
removed from ``auth_flow`` and ``upi_link``.  ``from ._state import config_dir``
names the submodule directly, and the function still reads the module global, so
``patch.object(_state, "CONFIG_DIR", ...)`` keeps working.
"""

from __future__ import annotations

from pathlib import Path

from ..kernel.paths import config_dir as _resolve_config_dir

# Credential-bearing shards live outside the git checkout (see sms_tool/paths.py).
CONFIG_DIR: Path = _resolve_config_dir()

#: The ``sms_tool`` package directory: the bundled fallback config lives here.
PACKAGE_DIR: Path = Path(__file__).resolve().parents[1]


def config_dir() -> Path:
    """The active config directory, read fresh on every call (see the module docstring)."""
    return CONFIG_DIR


def package_dir() -> Path:
    """The ``sms_tool`` package directory, read fresh on every call."""
    return PACKAGE_DIR
