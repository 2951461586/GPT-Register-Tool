"""Checkpoint persistence and post-create resume for the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-08).  The four functions here
are the workflow's *resume* stage: they build the checkpoint payload, persist
it at stage boundaries, detect a resumable checkpoint and re-enter the pipeline
after ``create_account`` without repeating signup or OTP.

Boundary with the neighbouring modules:

* ``registration_checkpoint`` owns the payload **contract** (build / persist /
  load / apply / restore cookies).  It is pure data plumbing and knows nothing
  about stages.
* This module owns the **stage wiring**: which workflow state the payload is
  built from, when a checkpoint is written, and what a resume replays.
* ``registration_handlers`` keeps thin delegates so every historical call site
  (``self._persist_checkpoint(...)`` -- including ``registration_finalize`` and
  the tests that mock it) keeps working unchanged.

They are module-level functions that take the workflow instance as their first
parameter, named ``workflow`` and annotated with ``RegistrationWorkflow``
under ``TYPE_CHECKING``, so the type checker resolves the workflow's attributes
without a runtime import cycle back into ``registration_handlers``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import checkpoint as registration_checkpoint
from ..auth_headers import current_auth_fingerprint, set_auth_fingerprint
from ..operator_output import emit as _emit
from .protocol_helpers import _new_registration_session
from .state import RegistrationState
from ..kernel.sanitizer import describe_exception

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .state import RegistrationWorkflow

_LOGGER = logging.getLogger(__name__)


def checkpoint_payload(workflow: RegistrationWorkflow) -> dict[str, Any]:
    """Build this run's checkpoint payload (data contract lives in ``registration_checkpoint``)."""
    payload = registration_checkpoint.build_checkpoint_payload(
        workflow.runtime, lambda: workflow.r._mailbox_snapshot(workflow.runtime.mailbox)
    )
    payload["auth_fingerprint_profile"] = str(current_auth_fingerprint().get("impersonate") or "")
    return payload


def persist_checkpoint(workflow: RegistrationWorkflow, state: str) -> None:
    """Write the checkpoint at a stage boundary; a failure is a warning, never fatal."""
    s = workflow.runtime
    if not s.username:
        return
    try:
        registration_checkpoint.persist_checkpoint(
            workflow.persistence,
            workflow.config,
            s,
            checkpoint_payload(workflow),
            state,
        )
    except Exception as exc:
        # ``emit`` rather than a bare ``print``: the line must reach both the
        # operator's stdout and the log file, and it is the documented seam for
        # every operator-visible line (the move out of ``registration_handlers``
        # is exactly when the bare-print ratchet asks for it).
        _emit(_LOGGER, "  [Checkpoint] persist warning: %s", describe_exception(exc))


def resume_post_create(workflow: RegistrationWorkflow) -> dict[str, Any] | None:
    """Re-enter the pipeline from a saved post-create checkpoint, or ``None``.

    The account and token already exist at this point, so signup and OTP stay
    disabled: the resume replays auth-session, AT probe, refresh-token and
    finalize only.  A checkpoint whose token is unusable is reported through
    ``_abort`` rather than silently re-registering the address.
    """
    mailbox_email = str(getattr(workflow.runtime.mailbox, "email", "") or "").strip()
    if not mailbox_email or workflow.input_mailbox is None:
        return None
    payload = workflow.runtime.resume_checkpoint or registration_checkpoint.load_resumable_checkpoint(
        workflow.persistence, mailbox_email, workflow.config
    )
    if payload is None:
        return None
    if not payload.get("access_token"):
        error = registration_checkpoint.session_recovery_error(payload)
        if error:
            workflow._abort(error)
    registration_checkpoint.apply_resume_payload(workflow.runtime, payload)
    workflow.runtime.username = mailbox_email
    set_auth_fingerprint(str(payload.get("auth_fingerprint_profile") or ""))
    _emit(_LOGGER, "[*] Resuming saved registration checkpoint for %s", mailbox_email)
    if not workflow.runtime.access_token:
        s = workflow.runtime
        s.session = _new_registration_session(s.proxy)
        workflow._install_edge_challenge_hook(s.session)
        registration_checkpoint.restore_session_cookies(s.session, payload)
        s.base_headers = (
            dict(payload["auth_headers"])
            if isinstance(payload.get("auth_headers"), dict)
            else (workflow.r.openai_auth_headers(s.device_id, accept="application/json", include_trace=True))
        )
        s.session_recovery_attempts += 1
        persist_checkpoint(workflow, registration_checkpoint.SESSION_PENDING_STATE)
        _LOGGER.info(
            "Resuming created-account session attempt=%s; signup and OTP remain disabled",
            s.session_recovery_attempts,
            extra={"event": "auth_session_recovery"},
        )
        workflow._run_stage(RegistrationState.AUTH_SESSION, "8-Resume auth session", workflow.fetch_auth_session)
    workflow._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Resume AT probe", workflow.probe_access_token)
    workflow._set_outcome()
    workflow.post_signup_warmup()
    workflow.obtain_oauth_refresh_token()
    return workflow._run_stage(RegistrationState.FINALIZE, "10-Finalize resumed registration", workflow.finalize)


def has_resume_checkpoint(workflow: RegistrationWorkflow) -> bool:
    """True when this mailbox has a resumable checkpoint and a mailbox was supplied."""
    if workflow.input_mailbox is None:
        return False
    return (
        registration_checkpoint.load_resumable_checkpoint(
            workflow.persistence, workflow.runtime.username, workflow.config
        )
        is not None
    )


__all__ = [
    "checkpoint_payload",
    "has_resume_checkpoint",
    "persist_checkpoint",
    "resume_post_create",
]
