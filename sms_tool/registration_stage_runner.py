"""Stage execution seam shared by the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-01). ``RegistrationAbort`` is the
sanitized failure signal every exit path funnels through, and
``RegistrationStageRunner`` is the single place a stage is executed against a
runtime context and the state machine -- the workflow keeps stage *ordering*,
this module keeps stage *execution*.

Neither class knows about the workflow, so this module has no cycle back to
``registration_handlers``.
"""

from __future__ import annotations

from typing import Any, Callable

from .registration_state import (
    RegistrationStage,
    RegistrationState,
    RegistrationStateMachine,
)


class RegistrationAbort(RuntimeError):
    """Expected workflow failure that is converted to a sanitized result."""


class RegistrationStageRunner:
    """Run one production stage against a shared runtime and state machine.

    ``context`` is whatever the caller wants handlers to see; the email
    workflow passes its ``RegistrationRuntimeState`` (not a
    ``RegistrationContext``) and handlers mutate that same runtime. ``run_stage``
    is the only execution seam.
    """

    def __init__(
        self,
        context: Any,
        machine: RegistrationStateMachine,
    ) -> None:
        self.context = context
        self.machine = machine

    def run_stage(
        self,
        state: RegistrationState,
        handler: Callable[[], Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        return RegistrationStage(
            state,
            lambda _context: handler(),
            timeout_seconds=timeout_seconds,
        ).run(self.context, self.machine)


__all__ = [
    "RegistrationAbort",
    "RegistrationStageRunner",
]
