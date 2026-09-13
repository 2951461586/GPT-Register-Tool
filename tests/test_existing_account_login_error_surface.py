"""The "no access token" fallback must not hide why an existing account failed.

Observed 2026-09-12 in ``runtime/_verify_wave2_run.log`` (line 602)::

    [!] Registration failed for bu***@icloud.com: missing_auth_session_access_token

Read alone, that name says "the protocol code lost a session it should have
had". The log says otherwise -- the account already existed, so there was never
a ChatGPT session to fetch:

    523  Status: 400  ... "code": "user_already_exists"
    526  Create account continue: 200 https://chatgpt.com/auth/login_with?...
    528-531  Auth session: 200 (x4, no access token -- expected for existing)
    532  Existing account has no ChatGPT session yet; retrying with passwordless...
    566  Existing account continue: 409
    579  Existing account login failed: existing_login_otp_validate: ... 409 ... invalid_state

The real cause is a 409 ``invalid_state`` on the re-login, and it was printed
but never reached the outcome. That matters beyond cosmetics:
``classify_error`` maps ``invalid_state`` to ``auth_state`` (retryable), while
``missing_auth_session_access_token`` used to map to ``unknown`` (not
retryable), so the generic name was also turning a transient failure into a
permanent drop.

2026-09-13 update (L3): the fallback itself is now ``auth_state`` too. Surfacing
the cause was the right first fix, but the no-cause fallback is not rare -- 18 of
179 failures over 09-08..09-13 ended there, every one of them a *protocol* run
whose pipeline reached ``finalize`` and whose auth session returned no access
token (upstream answered 200 with only ``WARNING_BANNER``). Leaving it
``unknown`` meant ``RegistrationRetryGuard`` never accumulated a cooldown and the
same mailbox was re-attempted immediately. See
``test_generic_fallback_is_a_retryable_auth_state`` below.

Three things this file pins down:

1. ``_registration_outcome`` prefers the real cause, but never over a genuine
   ``create_account`` error -- the cause is a fallback, not an override.
2. The classification consequence (retryable vs terminal) is asserted, because
   it is the reason the wording was worth changing at all.
3. The handler actually stores the cause on runtime state. A test that only
   covers the pure function survives deleting the assignment in
   ``fetch_auth_session``, which is the line that matters.
"""

import unittest
from unittest.mock import Mock

from sms_tool import registration
from sms_tool.error_classification import classify_error
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_policy import registration_retry_decision
from sms_tool.registration_runtime import RegistrationRuntimeState
from sms_tool.registration_state import RegistrationStateMachine


# Shape taken verbatim from auth_flow._login_existing_account_with_email_otp.
_LOGIN_ERROR = (
    'existing_login_otp_validate:{"endpoint": "/api/accounts/email-otp/validate", '
    '"status": 409, "body": {"error": {"code": "invalid_state", '
    '"message": "Your sign-in session is no longer valid."}}}'
)


class RegistrationOutcomeCauseTests(unittest.TestCase):
    """``_registration_outcome`` when there is no access token to return."""

    def test_existing_login_error_replaces_the_generic_fallback(self):
        success, error, _ = registration._registration_outcome(
            True, {}, "", {}, _LOGIN_ERROR
        )

        self.assertFalse(success)
        self.assertEqual(error, _LOGIN_ERROR)

    def test_create_account_error_still_wins_over_the_login_cause(self):
        create_data = {"error": {"code": "invalid_auth_step", "message": "Invalid authorization step."}}

        success, error, _ = registration._registration_outcome(
            False, create_data, "", {}, _LOGIN_ERROR
        )

        self.assertFalse(success)
        self.assertIn("invalid_auth_step", error)
        self.assertNotIn("existing_login_otp_validate", error)
        self.assertNotIn("invalid_state", error)

    def test_generic_fallback_remains_without_a_login_cause(self):
        success, error, _ = registration._registration_outcome(True, {}, "", {}, "")

        self.assertFalse(success)
        self.assertEqual(error, "missing_auth_session_access_token")

    def test_blank_login_cause_is_not_preferred_over_the_generic_fallback(self):
        success, error, _ = registration._registration_outcome(True, {}, "", {}, "   ")

        self.assertFalse(success)
        self.assertEqual(error, "missing_auth_session_access_token")

    def test_surfaced_cause_classifies_as_retryable_auth_state(self):
        _, error, _ = registration._registration_outcome(True, {}, "", {}, _LOGIN_ERROR)

        self.assertEqual(classify_error(error), "auth_state")
        self.assertTrue(registration_retry_decision(error).retryable)

    def test_generic_fallback_is_a_retryable_auth_state(self):
        """L3 (2026-09-13): the no-cause fallback is an auth-session state.

        This used to assert ``unknown`` / not-retryable. The intent behind that
        was conservative -- "we do not know why, so do not burn another attempt"
        -- with the real fix being to surface the cause (asserted above). But the
        fallback is not rare: 18 of 179 failures over 09-08..09-13 ended here,
        all of them protocol runs whose pipeline reached ``finalize`` while the
        auth session returned no access token. ``unknown`` is not in
        ``RETRYABLE_CLASSES``, so the retry guard never accumulated a cooldown.
        """
        _, error, _ = registration._registration_outcome(True, {}, "", {}, "")

        self.assertEqual(error, "missing_auth_session_access_token")
        self.assertEqual(classify_error(error), "auth_state")
        self.assertTrue(registration_retry_decision(error).retryable)


class ExistingAccountLoginSurfaceTests(unittest.TestCase):
    """``fetch_auth_session`` must carry the cause onto runtime state."""

    def _workflow(self, login_result):
        operations = Mock()
        operations._fetch_auth_session.return_value = {"body": {}}
        operations._auth_session_access_token.return_value = ""
        operations._sanitize_text.side_effect = lambda value: str(value)
        operations._login_existing_account_with_email_otp.return_value = login_result
        operations._registration_outcome = Mock(
            side_effect=lambda create_ok, create_data, access_token, at_probe,
            existing_login_error="": (
                False,
                existing_login_error or "missing_auth_session_access_token",
                "",
            )
        )
        workflow = RegistrationEmailWorkflow(
            RegistrationStateMachine(lambda *args: None),
            operations=operations,
            persistence=Mock(),
        )
        workflow.runtime.existing_account = True
        return workflow

    def test_failed_existing_login_is_stored_on_runtime_state(self):
        workflow = self._workflow({"ok": False, "error": _LOGIN_ERROR})

        workflow.fetch_auth_session()

        self.assertEqual(workflow.runtime.existing_login_error, _LOGIN_ERROR)

    def test_stored_error_is_sanitized_before_it_is_stored(self):
        workflow = self._workflow({"ok": False, "error": _LOGIN_ERROR})
        workflow._operations._sanitize_text.side_effect = lambda value: f"[SANITIZED]{value}"

        workflow.fetch_auth_session()

        self.assertTrue(workflow.runtime.existing_login_error.startswith("[SANITIZED]"))

    def test_successful_existing_login_stores_no_error(self):
        workflow = self._workflow({"ok": True})

        workflow.fetch_auth_session()

        self.assertEqual(workflow.runtime.existing_login_error, "")

    def test_stored_error_reaches_the_registration_outcome(self):
        workflow = self._workflow({"ok": False, "error": _LOGIN_ERROR})
        workflow.runtime.create_ok = True
        workflow.runtime.create_data = {}
        workflow.runtime.access_token = ""

        workflow.fetch_auth_session()
        workflow._set_outcome()

        self.assertIn("invalid_state", workflow.runtime.error)
        self.assertNotEqual(workflow.runtime.error, "missing_auth_session_access_token")

    def test_runtime_state_exposes_the_field_to_flat_and_grouped_callers(self):
        state = RegistrationRuntimeState(existing_login_error=_LOGIN_ERROR)

        self.assertEqual(state.existing_login_error, _LOGIN_ERROR)
        self.assertEqual(state.account.existing_login_error, _LOGIN_ERROR)

        state.existing_login_error = "replaced"
        self.assertEqual(state.account.existing_login_error, "replaced")


if __name__ == "__main__":
    unittest.main()
