"""Ports a mailbox provider may use without importing the application layer.

Rule 3 forbids provider adapters from importing the workflow. The 2026-10-21
architecture audit widened the concern: two providers imported
``mailbox_pool_writer.apply_rotation`` (persistence) and one imported
``desktop_ipc.progress_dots_enabled`` (the stdout/IPC channel). Both are things
a provider should **receive**, not fetch -- the routing owner knows whether a
rotation must be persisted and whether stdout is a terminal or the desktop IPC
pipe.

``sms_tool/mailbox/router.py`` installs the real implementations at import time. A
provider imported without that wiring still behaves predictably:

* ``progress_dots_enabled`` answers for a terminal (the pre-IPC default);
* ``apply_rotation`` raises a named error instead of silently skipping the
  persistence half of a rotation, which would leave the pool file and the
  in-memory account disagreeing.

The names are module-level callables rather than a class instance so a test can
replace one with ``monkeypatch.setattr(ports, "apply_rotation", fake)`` -- the
same patch-by-attribute seam ``auth_flow/deps.py`` uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol


class ApplyRotation(Protocol):
    """Persist a rotated refresh token on the mailbox and its pool line."""

    def __call__(
        self,
        mailbox: Any,
        *,
        previous: str,
        current: str,
        pool_path: str | Path | None = None,
    ) -> str: ...


def _unwired_apply_rotation(
    mailbox: Any,
    *,
    previous: str,
    current: str,
    pool_path: str | Path | None = None,
) -> str:
    raise RuntimeError(
        "providers.ports.apply_rotation is not wired: import sms_tool.mailbox.router (the routing owner) "
        "or call providers.ports.install(...) before driving a provider directly"
    )


_apply_rotation: ApplyRotation = _unwired_apply_rotation
_progress_dots: Callable[[], bool] = lambda: True
_wired = False


def _ensure_wired() -> None:
    """Install the application implementations on first use.

    The imports are function-local because the module-level edge is what this
    module exists to remove: ``providers -> sms_tool`` must not grow. This is
    the remedy ``import_layer_ratchet`` names for a documented DI seam, and the
    edge is counted by ``delayed_import_ratchet`` instead.
    """
    global _wired
    if _wired:
        return
    from ..desktop_ipc import progress_dots_enabled as dots
    from ..mailbox.pool_writer import apply_rotation as rotate

    install(apply_rotation_impl=rotate, progress_dots_impl=dots)
    _wired = True


def apply_rotation(
    mailbox: Any,
    *,
    previous: str,
    current: str,
    pool_path: str | Path | None = None,
) -> str:
    """Persist a rotation through the application-side implementation."""
    _ensure_wired()
    return _apply_rotation(mailbox, previous=previous, current=current, pool_path=pool_path)


def progress_dots_enabled() -> bool:
    """Whether the provider may write progress dots to stdout."""
    _ensure_wired()
    return _progress_dots()


def install(
    *,
    apply_rotation_impl: ApplyRotation | None = None,
    progress_dots_impl: Callable[[], bool] | None = None,
) -> None:
    """Install the application-side implementations.

    Called once from ``sms_tool/mailbox/router.py``. Keyword-only and partial on
    purpose: a test that needs only one port can install that one.
    """
    global _apply_rotation, _progress_dots, _wired
    if apply_rotation_impl is not None:
        _apply_rotation = apply_rotation_impl
    if progress_dots_impl is not None:
        _progress_dots = progress_dots_impl
    _wired = True
