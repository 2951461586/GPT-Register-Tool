"""Contract tests for ``registration/finalize.py`` -- the single assembly point.

``RegistrationEmailWorkflow.finalize`` (a one-line delegate to this module) is the
only place a completed or failed protocol run becomes the result dict that is
persisted, emitted to the desktop and fed to the retry guard.  Until this file
existed the whole body was covered only indirectly from ``tests/accounts``; the
protocol domain had no test of its own assembly, its checkpoint-retention
decision, or the payload keys the storage layer reads.

The four branches pinned here:

1. **success** -- ``registration_state == "active"``, the AT reaches the payload,
   the machine reaches ``completed``, and the checkpoint is cleared;
2. **partial_registered** -- an already-registered address that we could not log
   back into is not reported as a failure, and it carries
   ``needs_manual_session_recovery``;
3. **password known** -- the same partial state, but recoverable through the
   password lane, so the "needs a human" bucket must stay empty;
4. **checkpoint retention** -- a created account with no AT keeps the
   ``auth_session_pending`` checkpoint (never re-runs signup), while a probe
   verdict that judged the token invalid clears it.

The persistence seam is a small in-memory fake, and
``_retain_registration_checkpoint`` is the *real* function in every branch, so the
retention decision under test is the production one rather than a stub.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import Mock, patch

from sms_tool.registration import finalize as registration_finalize
from sms_tool.registration.checkpoint import SESSION_PENDING_STATE
from sms_tool.registration.handlers import RegistrationEmailWorkflow
from sms_tool.registration.outcome import _retain_registration_checkpoint
from sms_tool.registration.state import RegistrationState, RegistrationStateMachine

_PROXY = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"


class _FakePersistence:
    """In-memory stand-in for ``RegistrationPersistence`` (records, never writes)."""

    def __init__(self) -> None:
        self.save_calls: list[tuple[str, str]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.clear_calls: list[str] = []

    def save_checkpoint(
        self,
        email: str,
        state: str,
        payload: Mapping[str, Any],
        *,
        runtime_config: Mapping[str, Any] | None = None,
    ) -> bool:
        self.save_calls.append((email, state))
        return True

    def upsert_account(self, payload: Mapping[str, Any], *, runtime_config: Mapping[str, Any] | None = None) -> bool:
        self.upsert_calls.append(dict(payload))
        return True

    def get_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        return {}

    def get_device_context(self, email: str) -> Mapping[str, Any]:
        return {}

    def clear_checkpoint(self, email: str, *, runtime_config: Mapping[str, Any] | None = None) -> bool:
        self.clear_calls.append(email)
        return True


def _workflow(
    *,
    success: bool = False,
    access_token: str = "",
    existing_account: bool = False,
    password_known: bool = False,
    create_ok: bool = False,
    at_probe: dict | None = None,
    oauth_refresh_token: str = "",
) -> tuple[RegistrationEmailWorkflow, _FakePersistence]:
    """A workflow whose ``finalize`` can actually run (no database, no network)."""
    config = {"proxy": {"registration": _PROXY, "pool": [_PROXY]}, "registration": {}}
    operations = Mock()
    operations._sanitize_text.side_effect = lambda value: str(value or "")
    operations._oauth_result_summary.return_value = {}
    operations._timing_summary.return_value = {}
    operations._mailbox_snapshot.return_value = {}
    # The production retention rule, not a stub: the decision is what branch 4 pins.
    operations._retain_registration_checkpoint = _retain_registration_checkpoint
    persistence = _FakePersistence()

    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        config=config,
        operations=operations,
        persistence=persistence,
    )
    runtime = workflow.runtime
    runtime.proxy = _PROXY
    runtime.username = "finalize@example.test"
    runtime.registration_mode = "passwordless"
    runtime.session_logging_id = "log-1"
    runtime.device_id = "did-0123456789"
    runtime.success = success
    runtime.access_token = access_token
    runtime.existing_account = existing_account
    runtime.existing_account_password_known = password_known
    runtime.create_ok = create_ok
    runtime.at_probe = dict(at_probe or {})
    runtime.oauth_refresh_token = oauth_refresh_token
    return workflow, persistence


def test_success_is_active_carries_the_token_and_clears_the_checkpoint():
    workflow, persistence = _workflow(
        success=True,
        access_token="at-live",
        create_ok=True,
        at_probe={"status_code": 200, "status": "active"},
    )

    result = workflow.finalize()

    assert result["success"] is True
    assert result["registration_state"] == "active"
    assert result["access_token"] == "at-live"
    assert result["registration_success_basis"] == "at_http_200"
    assert result["registration_machine"]["state"] == RegistrationState.COMPLETED.value
    assert persistence.clear_calls == ["finalize@example.test"]
    assert persistence.save_calls == [], "a successful run must not retain a checkpoint"


def test_finalize_publishes_the_oauth_refresh_token_into_the_payload():
    """Behavioral replacement for the old ``inspect.getsource(finalize)`` scan.

    ``obtain_oauth_refresh_token`` only sets a runtime field; the storage column
    the recovery chain reads is filled from this payload.  A token that stops at
    the field is the measured 0/1245 failure this plumbing exists to fix.
    """
    workflow, _ = _workflow(
        success=True,
        access_token="at-live",
        at_probe={"status_code": 200},
        oauth_refresh_token="rt_1234567890",
    )

    result = workflow.finalize()

    assert result["oauth_refresh_token"] == "rt_1234567890"
    assert result["refresh_token_status"] == "oauth_present"


def test_an_account_without_a_refresh_token_reports_no_rt():
    workflow, _ = _workflow(success=True, access_token="at-live", at_probe={"status_code": 200})

    result = workflow.finalize()

    assert result["oauth_refresh_token"] == ""
    assert result["refresh_token_status"] == "no_rt"


def test_a_partial_registration_is_not_a_failure_and_needs_a_human_when_no_password():
    workflow, _ = _workflow(
        success=False,
        access_token="",
        existing_account=True,
        password_known=False,
        create_ok=True,
    )
    with patch.object(registration_finalize, "RegistrationRetryGuard") as guard:
        result = workflow.finalize()

    assert result["success"] is False
    assert result["registration_state"] == "partial_registered"
    assert result["needs_manual_session_recovery"] is True
    # ``record(success=True)`` is reserved for the *successful* re-login of an
    # existing account; a partial registration must not be counted as one.
    guard.return_value.record.assert_not_called()


def test_a_partial_registration_with_a_known_password_is_not_the_manual_bucket():
    workflow, _ = _workflow(
        success=False,
        access_token="",
        existing_account=True,
        password_known=True,
        create_ok=True,
    )
    with patch.object(registration_finalize, "RegistrationRetryGuard"):
        result = workflow.finalize()

    assert result["registration_state"] == "partial_registered"
    assert result["needs_manual_session_recovery"] is False


def test_a_created_account_without_a_token_retains_the_session_checkpoint():
    """Never re-run signup/OTP for an account that already exists server-side."""
    workflow, persistence = _workflow(success=False, access_token="", create_ok=True)

    workflow.finalize()

    assert persistence.save_calls == [("finalize@example.test", SESSION_PENDING_STATE)]
    assert persistence.clear_calls == []


def test_a_transport_probe_verdict_retains_the_checkpoint_and_an_invalid_token_clears_it():
    blocked, blocked_persistence = _workflow(success=False, access_token="at-live", at_probe={"status_code": 0})
    blocked.finalize()
    assert blocked_persistence.save_calls == []
    assert blocked_persistence.clear_calls == [], "a transport failure is not a verdict about the account"

    invalid, invalid_persistence = _workflow(
        success=False,
        access_token="at-live",
        at_probe={"status_code": 401, "status": "token_invalid"},
    )
    invalid.finalize()
    assert invalid_persistence.save_calls == []
    assert invalid_persistence.clear_calls == ["finalize@example.test"], "a judged token drops the checkpoint"
