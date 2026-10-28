"""Pin the identity-context contract ``_run_stage`` verifies after IDENTITY_READY.

``prepare_identity`` is the only writer of ``runtime.context``; from then on
``_run_stage`` hands that object to every handler. Before the identity stage --
and on a post-create resume, which skips identity entirely -- handlers get the
mutable ``RegistrationRuntimeState`` instead.

Every current stage body closes over ``self`` and ignores the parameter, so a
drift here is invisible today: the first handler that actually *reads* its
context would meet a runtime where its annotation promised a
``RegistrationContext``.  These tests pin the warning that makes the drift
visible, and pin that it is a warning rather than an abort -- a no-op handler
driving the stage idempotently is a legitimate path
(``test_registration_bootstrap_timing.py`` uses it), and failing a live
registration because a stub did not build a context would be a worse trade.
"""

from __future__ import annotations

import logging

import pytest

from sms_tool.registration import orchestrator as registration
from sms_tool.registration.handlers import RegistrationEmailWorkflow
from sms_tool.registration.state import RegistrationContext, RegistrationState, RegistrationStateMachine

#: The check lives with the identity stage's own post-condition
#: (``identity_stages.verify_identity_context``), not in ``handlers`` -- that file's
#: line budget and the "stage body only in its owner module" guard require the
#: delegate to stay two lines.
LOGGER_NAME = "sms_tool.registration.identity_stages"


def _workflow() -> RegistrationEmailWorkflow:
    return RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        operations=registration._email_registration_operations(),
    )


def _context(device_id: str = "did-fixture") -> RegistrationContext:
    """A real ``RegistrationContext``: the guard runs against the production type.

    A stand-in with only ``device_id`` would have passed the guard while proving
    nothing about the object the workflow actually stores (and pyright rejects it,
    because ``runtime.context`` is annotated ``RegistrationContext | None``).
    """
    return RegistrationContext(
        proxy="",
        mailbox=None,
        sentinel_data={},
        auth_base="https://auth.example.test",
        chat_base="https://chat.example.test",
        username="contract@example.test",
        password="fixture-password",
        explicit_password=False,
        password_from_storage=False,
        first_name="Fixture",
        last_name="Account",
        birthdate="1990-01-01",
        registration_mode="passwordless",
        device_id=device_id,
        session_logging_id="logging-fixture",
        reused_device_context=False,
    )


def _markers(caplog) -> list[str]:
    """Warnings the identity module emitted (the marker is the contract)."""
    return [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]


def test_identity_stage_without_a_context_warns(caplog):
    workflow = _workflow()
    workflow.runtime.context = None

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", lambda: None)

    assert any("registration_identity_context_missing" in line for line in _markers(caplog))


def test_the_check_stays_a_delegate_on_the_workflow(caplog):
    """``patch.object(RegistrationEmailWorkflow, ...)`` must still intercept it."""
    workflow = _workflow()

    from unittest.mock import patch

    with patch.object(RegistrationEmailWorkflow, "_verify_identity_context", autospec=True) as check:
        workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", lambda: None)
    check.assert_called_once_with(workflow, RegistrationState.IDENTITY_READY)


def test_identity_stage_with_a_context_does_not_warn(caplog):
    workflow = _workflow()
    workflow.runtime.context = _context()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", lambda: None)

    assert not any("registration_identity_context" in line for line in _markers(caplog))


def test_identity_context_without_device_id_warns(caplog):
    """The oai-did/header/cookie trio is unverifiable without it."""
    workflow = _workflow()
    workflow.runtime.context = _context(device_id="")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", lambda: None)

    assert any("registration_identity_context_without_device_id" in line for line in _markers(caplog))


@pytest.mark.parametrize(
    "state",
    [
        RegistrationState.AUTH_FLOW,
        RegistrationState.USER_REGISTER,
        RegistrationState.CREATE_ACCOUNT,
        RegistrationState.FINALIZE,
    ],
)
def test_other_stages_never_report_a_context_problem(caplog, state):
    """A resume runs these with no context at all; that is normal, not a finding."""
    workflow = _workflow()
    workflow.runtime.context = None

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        workflow._run_stage(state, "stage", lambda: None)

    assert not any("registration_identity_context" in line for line in _markers(caplog))
