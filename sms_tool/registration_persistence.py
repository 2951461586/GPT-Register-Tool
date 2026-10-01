"""Persistence seam for the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-01) so the workflow module owns
stage ordering while the checkpoint/account persistence contract lives on its
own. The seam is deliberate: the workflow depends on the ``Protocol`` shape, not
on ``storage``, so a test can inject a fake without a database.

``StorageRegistrationPersistence`` is the default adapter kept at the
application seam -- it is the only place that reaches into ``storage``, and it
does so with delayed imports so importing this module never drags the storage
stack in.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class RegistrationPersistence(Protocol):
    """Persistence seam for registration checkpoints and device identity."""

    def save_checkpoint(
        self, email: str, state: str, payload: Mapping[str, Any], *, runtime_config: Mapping[str, Any] | None
    ) -> Any: ...
    def upsert_account(self, payload: Mapping[str, Any], *, runtime_config: Mapping[str, Any] | None) -> Any: ...
    def get_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None) -> Mapping[str, Any]: ...
    def get_device_context(self, email: str) -> Mapping[str, Any]: ...
    def clear_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None) -> Any: ...


class StorageRegistrationPersistence:
    """Default adapter kept at the application seam, not inside stage logic."""

    def save_checkpoint(self, email, state, payload, *, runtime_config=None):
        from .storage import save_registration_checkpoint

        return save_registration_checkpoint(email, state, payload, runtime_config=runtime_config)

    def upsert_account(self, payload, *, runtime_config=None):
        from .storage import upsert_account

        return upsert_account(payload, runtime_config=runtime_config)

    def get_checkpoint(self, email, *, runtime_config=None):
        from .storage import get_registration_checkpoint

        return get_registration_checkpoint(email, runtime_config=runtime_config)

    def get_device_context(self, email):
        from .storage import get_device_context

        return get_device_context(email)

    def clear_checkpoint(self, email, *, runtime_config=None):
        from .storage import clear_registration_checkpoint

        return clear_registration_checkpoint(email, runtime_config=runtime_config)


__all__ = [
    "RegistrationPersistence",
    "StorageRegistrationPersistence",
]
