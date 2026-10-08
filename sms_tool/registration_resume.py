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

They are module-level functions whose first parameter is the workflow instance,
the same shape ``registration_otp_stages`` uses, so this module cannot form an
import cycle back into ``registration_handlers``.
"""

from __future__ import annotations

import logging
from typing import Any

from . import registration_checkpoint
from .auth_headers import current_auth_fingerprint, set_auth_fingerprint
from .operator_output import emit as _emit
from .registration_protocol_helpers import _new_registration_session
from .registration_state import RegistrationState
from .sanitizer import describe_exception

_LOGGER = logging.getLogger(__name__)


def checkpoint_payload(self: Any) -> dict[str, Any]:
    """Build this run's checkpoint payload (data contract lives in ``registration_checkpoint``)."""
    payload = registration_checkpoint.build_checkpoint_payload(
        self.runtime, lambda: self.r._mailbox_snapshot(self.runtime.mailbox)
    )
    payload["auth_fingerprint_profile"] = str(current_auth_fingerprint().get("impersonate") or "")
    return payload


def persist_checkpoint(self: Any, state: str) -> None:
    """Write the checkpoint at a stage boundary; a failure is a warning, never fatal."""
    s = self.runtime
    if not s.username:
        return
    try:
        registration_checkpoint.persist_checkpoint(
            self.persistence,
            self.config,
            s,
            checkpoint_payload(self),
            state,
        )
    except Exception as exc:
        # ``emit`` rather than a bare ``print``: the line must reach both the
        # operator's stdout and the log file, and it is the documented seam for
        # every operator-visible line (the move out of ``registration_handlers``
        # is exactly when the bare-print ratchet asks for it).
        _emit(_LOGGER, "  [Checkpoint] persist warning: %s", describe_exception(exc))


def resume_post_create(self: Any) -> dict[str, Any] | None:
    """Re-enter the pipeline from a saved post-create checkpoint, or ``None``.

    The account and token already exist at this point, so signup and OTP stay
    disabled: the resume replays auth-session, AT probe, refresh-token and
    finalize only.  A checkpoint whose token is unusable is reported through
    ``_abort`` rather than silently re-registering the address.
    """
    mailbox_email = str(getattr(self.runtime.mailbox, "email", "") or "").strip()
    if not mailbox_email or self.input_mailbox is None:
        return None
    payload = self.runtime.resume_checkpoint or registration_checkpoint.load_resumable_checkpoint(
        self.persistence, mailbox_email, self.config
    )
    if payload is None:
        return None
    if not payload.get("access_token"):
        error = registration_checkpoint.session_recovery_error(payload)
        if error:
            self._abort(error)
    registration_checkpoint.apply_resume_payload(self.runtime, payload)
    self.runtime.username = mailbox_email
    set_auth_fingerprint(str(payload.get("auth_fingerprint_profile") or ""))
    _emit(_LOGGER, "[*] Resuming saved registration checkpoint for %s", mailbox_email)
    if not self.runtime.access_token:
        s = self.runtime
        s.session = _new_registration_session(s.proxy)
        self._install_edge_challenge_hook(s.session)
        registration_checkpoint.restore_session_cookies(s.session, payload)
        s.base_headers = (
            dict(payload["auth_headers"])
            if isinstance(payload.get("auth_headers"), dict)
            else (self.r.openai_auth_headers(s.device_id, accept="application/json", include_trace=True))
        )
        s.session_recovery_attempts += 1
        persist_checkpoint(self, registration_checkpoint.SESSION_PENDING_STATE)
        _LOGGER.info(
            "Resuming created-account session attempt=%s; signup and OTP remain disabled",
            s.session_recovery_attempts,
            extra={"event": "auth_session_recovery"},
        )
        self._run_stage(RegistrationState.AUTH_SESSION, "8-Resume auth session", self.fetch_auth_session)
    self._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Resume AT probe", self.probe_access_token)
    self._set_outcome()
    self.obtain_oauth_refresh_token()
    return self._run_stage(RegistrationState.FINALIZE, "10-Finalize resumed registration", self.finalize)


def has_resume_checkpoint(self: Any) -> bool:
    """True when this mailbox has a resumable checkpoint and a mailbox was supplied."""
    if self.input_mailbox is None:
        return False
    return (
        registration_checkpoint.load_resumable_checkpoint(self.persistence, self.runtime.username, self.config)
        is not None
    )


__all__ = [
    "checkpoint_payload",
    "has_resume_checkpoint",
    "persist_checkpoint",
    "resume_post_create",
]
