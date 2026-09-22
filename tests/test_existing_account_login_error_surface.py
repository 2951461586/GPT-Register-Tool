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
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool import registration, registration_handlers
from sms_tool.accounts.account_creation import _is_user_already_exists
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


class ExistingLoginProbeTests(unittest.TestCase):
    """``fetch_auth_session`` asks the probe before any email code is spent.

    The passwordless lane cannot turn a ``user_already_exists`` state into a
    session: it verifies an OTP and lands on ``/about-you``, a *signup* step the
    server never follows with a NextAuth session (measured 2026-09-14: 5/5
    landings, 0 sessions).  So the lane is asked with the account password (when
    we hold one) and with ``allow_passwordless=False``, and only a verdict the
    probe could not reach at all is allowed to fall back to email.
    """

    def _workflow(self, *, login_result, password_known=True, existing_account=True):
        operations = Mock()
        operations._fetch_auth_session.return_value = {"body": {}}
        operations._auth_session_access_token.return_value = ""
        operations._sanitize_text.side_effect = lambda value: str(value)
        operations._login_existing_account_with_email_otp.return_value = login_result
        operations._stored_registration_totp.return_value = "BASE32SECRET"
        workflow = RegistrationEmailWorkflow(
            RegistrationStateMachine(lambda *args: None),
            operations=operations,
            persistence=Mock(),
        )
        workflow.runtime.existing_account = existing_account
        workflow.runtime.existing_account_password_known = password_known
        workflow.runtime.password = "StoredPass123"
        workflow.runtime.username = "dead@example.com"
        return workflow

    def _guard(self):
        # ``registration_handlers`` binds the class at import time, so the seam
        # is that module's own symbol -- patching the defining module would not
        # reach the call site.
        return patch("sms_tool.registration_handlers.RegistrationRetryGuard")

    def test_the_lane_is_asked_with_the_password_and_email_forbidden(self):
        workflow = self._workflow(login_result={"ok": True})

        workflow.fetch_auth_session()

        kwargs = workflow._operations._login_existing_account_with_email_otp.call_args.kwargs
        self.assertEqual(kwargs["password"], "StoredPass123")
        # ``existing_account`` is only ever set by ``user_already_exists``, and
        # for that address the email lane is a measured dead end.
        self.assertFalse(kwargs["allow_passwordless"])

    def test_an_unknown_password_is_not_submitted(self):
        """The inverse guard: submitting a generated password would be a guess."""
        workflow = self._workflow(login_result={"ok": True}, password_known=False)

        workflow.fetch_auth_session()

        kwargs = workflow._operations._login_existing_account_with_email_otp.call_args.kwargs
        self.assertEqual(kwargs["password"], "")

    def test_the_stored_totp_travels_with_the_password(self):
        """Nothing is enrolled yet in this run, so the secret must come from storage.

        Without it the MFA challenge that follows a password login can only be
        answered with ``existing_login_totp_secret_missing``.
        """
        workflow = self._workflow(login_result={"ok": True})

        workflow.fetch_auth_session()

        kwargs = workflow._operations._login_existing_account_with_email_otp.call_args.kwargs
        self.assertEqual(kwargs["totp_secret"], "BASE32SECRET")

    def test_a_definitive_negative_is_written_to_the_retry_guard(self):
        """The next batch must skip this address without spending an OTP to relearn it."""
        workflow = self._workflow(
            login_result={
                "ok": False,
                "error": "existing_login_no_password_step:password_form_absent",
            }
        )

        with self._guard() as guard_cls:
            workflow.fetch_auth_session()

        guard_cls.return_value.mark_dead_end.assert_called_once()
        self.assertEqual(guard_cls.return_value.mark_dead_end.call_args.args[0], "dead@example.com")

    def test_the_inconclusive_verdict_is_also_written_to_the_retry_guard(self):
        """``unknown`` is not a licence to spend a code on a known-registered address."""
        workflow = self._workflow(
            login_result={"ok": False, "error": "existing_login_password_step_unknown"}
        )

        with self._guard() as guard_cls:
            workflow.fetch_auth_session()

        guard_cls.return_value.mark_dead_end.assert_called_once()

    def test_password_required_is_not_blacklisted(self):
        """A later run supplying ``--password`` could still log this account in.

        Blacklisting "the account has a password we do not hold" would skip an
        address we can actually recover, so this one failure must stay out of
        the permanent ledger.
        """
        workflow = self._workflow(login_result={"ok": False, "error": "existing_login_password_required"})

        with self._guard() as guard_cls:
            workflow.fetch_auth_session()

        guard_cls.return_value.mark_dead_end.assert_not_called()

    def test_a_successful_probe_reads_the_new_session(self):
        workflow = self._workflow(login_result={"ok": True})

        workflow.fetch_auth_session()

        self.assertEqual(workflow._operations._fetch_auth_session.call_count, 2)

    def test_an_unregistered_address_never_enters_the_existing_login_lane(self):
        """The inverse guard: this lane is only for addresses the server called registered.

        The recovery lane (``account_recovery.relogin_chatgpt_email_account``)
        calls the same function directly and relies on its default
        ``allow_passwordless=True``; that side is pinned in
        ``tests/test_existing_login_password_probe.py``.
        """
        workflow = self._workflow(login_result={"ok": True}, existing_account=False)

        workflow.fetch_auth_session()

        workflow._operations._login_existing_account_with_email_otp.assert_not_called()


class ExistingLoginDeadEndErrorTests(unittest.TestCase):
    """Only verdicts that cannot be recovered from may enter the permanent ledger."""

    def test_the_two_unrecoverable_verdicts_are_recognised(self):
        for error in (
            "existing_login_no_password_step:password_form_absent",
            "existing_login_password_step_unknown",
        ):
            with self.subTest(error=error):
                self.assertTrue(registration_handlers._is_existing_login_dead_end_error(error))

    def test_a_recoverable_verdict_is_not(self):
        for error in (
            "existing_login_password_required",
            "existing_login_otp_poll_timeout",
            "existing_login_transport:boom",
            "",
            None,
        ):
            with self.subTest(error=error):
                self.assertFalse(registration_handlers._is_existing_login_dead_end_error(error))


class LoginProbePasswordGateTests(unittest.TestCase):
    """``_login_probe_password`` -- one answer, shared by every probe caller.

    The probe can only *offer* the password step; the caller still has to supply
    a password.  Answering "is this password the account's own?" in each caller
    would invite three different answers, so it is answered once.

    ``password_unknown`` alone is the wrong gate: ``create_account`` sets it for
    *every* ``user_already_exists`` answer -- including the ones where the
    caller supplied ``--password`` -- so keying on it would withhold the password
    in exactly the case where we do own it.
    """

    def _state(self, **overrides):
        # ``password_unknown`` defaults to True because that is what
        # ``create_account`` sets for an ``user_already_exists`` answer; the
        # known/unknown split for those is carried by
        # ``existing_account_password_known`` instead.
        state = SimpleNamespace(
            password="StoredPass123",
            existing_account=True,
            existing_account_password_known=True,
            password_unknown=True,
        )
        for key, value in overrides.items():
            setattr(state, key, value)
        return state

    def test_a_known_existing_account_password_is_submitted(self):
        self.assertEqual(
            registration_handlers._login_probe_password(self._state()), "StoredPass123"
        )

    def test_an_unknown_existing_account_password_is_withheld(self):
        """The inverse guard: submitting the generated password would be a guess."""
        state = self._state(existing_account_password_known=False)

        self.assertEqual(registration_handlers._login_probe_password(state), "")

    def test_a_fresh_registration_submits_the_password_we_just_set(self):
        state = self._state(
            existing_account=False, existing_account_password_known=False, password_unknown=False
        )

        self.assertEqual(registration_handlers._login_probe_password(state), "StoredPass123")

    def test_a_resumed_verification_does_not_submit_a_password_we_do_not_own(self):
        """``--resume-email-verification`` without a password cannot vouch for it."""
        state = self._state(
            existing_account=False, existing_account_password_known=False, password_unknown=True
        )

        self.assertEqual(registration_handlers._login_probe_password(state), "")

    def test_no_password_at_all_is_withheld(self):
        self.assertEqual(registration_handlers._login_probe_password(self._state(password="")), "")


class ReauthHandsTheProbeItsInputsTests(unittest.TestCase):
    """``enroll_totp``'s re-auth runs the *same* lane, so it needs the same inputs.

    ``reauth_existing_account`` calls ``_login_existing_account_with_email_otp``
    directly rather than going through ``fetch_auth_session``, so it is a second
    call site that has to be kept in step.  Without the password the probe's
    positive verdict has nothing to submit and the re-auth can only answer
    ``password_required``; without the TOTP secret a password login cannot clear
    the account's own MFA challenge.
    """

    def _workflow(self, *, totp_secret="", stored_totp="BASE32SECRET", **overrides):
        operations = Mock()
        operations._fetch_auth_session.return_value = {"body": {}}
        operations._auth_session_access_token.return_value = "fresh-at"
        operations._sanitize_text.side_effect = lambda value: str(value)
        operations._stored_registration_totp.return_value = stored_totp
        operations._login_existing_account_with_email_otp.return_value = {"ok": True}
        workflow = RegistrationEmailWorkflow(
            RegistrationStateMachine(lambda *args: None),
            operations=operations,
            persistence=Mock(),
        )
        workflow.runtime.success = True
        workflow.runtime.access_token = "stale-at"
        workflow.runtime.mailbox = "box@example.com"
        workflow.runtime.username = "dead@example.com"
        workflow.runtime.password = "StoredPass123"
        workflow.runtime.totp_secret = totp_secret
        for key, value in overrides.items():
            setattr(workflow.runtime, key, value)
        return workflow

    def _reauth(self, workflow):
        """Drive ``enroll_totp`` with a 2FA stub that actually calls the re-auth."""

        def fake_setup_totp_2fa(**kwargs):
            kwargs["reauth_login_fn"]()
            return {"ok": True, "totp_secret": "NEWSECRET", "access_token": "fresh-at"}

        with patch(
            "sms_tool.accounts.account_2fa.setup_totp_2fa", side_effect=fake_setup_totp_2fa
        ):
            workflow.enroll_totp()
        return workflow._operations._login_existing_account_with_email_otp.call_args.kwargs

    def test_the_reauth_submits_the_password_and_the_stored_secret(self):
        workflow = self._workflow(existing_account=True, existing_account_password_known=True)

        kwargs = self._reauth(workflow)

        self.assertEqual(kwargs["password"], "StoredPass123")
        # Nothing is enrolled in this run yet, so the secret has to come from storage.
        self.assertEqual(kwargs["totp_secret"], "BASE32SECRET")
        self.assertFalse(kwargs["allow_passwordless"])

    def test_an_unknown_existing_account_password_is_withheld_here_too(self):
        """The gate is shared, so this call site cannot bypass it."""
        workflow = self._workflow(existing_account=True, existing_account_password_known=False)

        kwargs = self._reauth(workflow)

        self.assertEqual(kwargs["password"], "")

    def test_a_secret_enrolled_in_this_run_wins_over_storage(self):
        workflow = self._workflow(
            totp_secret="RUNSECRET",
            existing_account=True,
            existing_account_password_known=True,
        )

        kwargs = self._reauth(workflow)

        self.assertEqual(kwargs["totp_secret"], "RUNSECRET")


class _CreateResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = "{}"

    def json(self):
        return self._body


class CreateAccountPasswordKnownTests(unittest.TestCase):
    """``create_account`` itself must record whether a password login is possible.

    The probe can only *offer* the password step -- a login still needs a
    password to submit -- so the flag it feeds has to be raised at the one place
    the ``user_already_exists`` answer is parsed.  A test that sets it by hand
    cannot notice that assignment being deleted.
    """

    _CREATE_DATA = {
        "error": {
            "message": "An account already exists for this email address, please login instead.",
            "type": "invalid_request_error",
            "code": "user_already_exists",
            "redirect_uri": "https://chatgpt.com/auth/login_with?callback_path=/",
            "userAlreadyExistsRecovery": {"action": "continue_to_login"},
        }
    }

    def _workflow(self, *, status_code, body, explicit_password=False, password_from_storage=False):
        operations = Mock()
        operations._sanitize_text.side_effect = lambda value: str(value)
        operations._is_user_already_exists.side_effect = _is_user_already_exists
        operations._auth_request_headers.return_value = {}
        operations.auth_impersonate.return_value = None
        operations._create_account_continue_url.return_value = ""
        operations.request_with_retry.return_value = _CreateResponse(status_code, body)
        workflow = RegistrationEmailWorkflow(
            RegistrationStateMachine(lambda *args: None),
            operations=operations,
            persistence=Mock(),
        )
        workflow.runtime.auth_base = "https://auth.openai.com"
        workflow.runtime.base_headers = {}
        workflow.runtime.device_id = "did-1"
        workflow.runtime.context = Mock(
            explicit_password=explicit_password,
            password_from_storage=password_from_storage,
        )
        return workflow

    def _sentinel(self):
        return patch.object(
            RegistrationEmailWorkflow, "_issue_sentinel", return_value=Mock(token="t", so_token="s")
        )

    def test_a_stored_password_makes_a_password_login_possible(self):
        workflow = self._workflow(status_code=400, body=self._CREATE_DATA, password_from_storage=True)

        with self._sentinel():
            workflow.create_account()

        self.assertTrue(workflow.runtime.existing_account_password_known)
        # The existing wording must survive: the outcome reporter names this
        # cause, and ``create_ok`` is what keeps it out of the generic fallback.
        self.assertTrue(workflow.runtime.existing_account)
        self.assertTrue(workflow.runtime.create_ok)

    def test_an_explicit_password_also_makes_it_possible(self):
        workflow = self._workflow(status_code=400, body=self._CREATE_DATA, explicit_password=True)

        with self._sentinel():
            workflow.create_account()

        self.assertTrue(workflow.runtime.existing_account_password_known)

    def test_a_generated_password_does_not(self):
        """A freshly generated password is a guess, not this account's password."""
        workflow = self._workflow(status_code=400, body=self._CREATE_DATA)

        with self._sentinel():
            workflow.create_account()

        self.assertFalse(workflow.runtime.existing_account_password_known)
        self.assertTrue(workflow.runtime.existing_account)

    def test_a_successful_create_does_not_set_the_flag(self):
        """The inverse guard: a flag that always fires would submit a wrong password."""
        workflow = self._workflow(status_code=200, body={}, password_from_storage=True)

        with self._sentinel():
            workflow.create_account()

        self.assertFalse(workflow.runtime.existing_account_password_known)
        self.assertTrue(workflow.runtime.create_ok)

    def test_another_400_code_does_not_set_the_flag(self):
        workflow = self._workflow(
            status_code=400,
            body={"error": {"code": "invalid_auth_step", "message": "no"}},
            password_from_storage=True,
        )

        with self._sentinel():
            workflow.create_account()

        self.assertFalse(workflow.runtime.existing_account_password_known)
        self.assertFalse(workflow.runtime.create_ok)


if __name__ == "__main__":
    unittest.main()
