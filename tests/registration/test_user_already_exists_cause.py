"""``user_already_exists`` must survive to the outcome, and must stop the retry.

Observed 2026-09-14 on three mailboxes whose ``+oai01`` twins had been registered
back on 09-06. Every attempt logged::

    [7-Create account]
      Status: 400
      Response: {... "code": "user_already_exists",
                    "userAlreadyExistsRecovery": {"action": "continue_to_...}}
      Account already exists, password may differ from generated one; ...
    [8-Fetch auth session]
      Auth session: 200 (x4, no access token)
      Existing account has no ChatGPT session yet; retrying with passwordless...
      Existing account OTP send: /api/accounts/email-otp/resend 429 rate_limit_exceeded
      Existing account login failed: existing_login_otp_send_failed:429

``registration_audit`` then recorded ``existing_login_otp_send_failed:429`` and,
in earlier rounds, ``existing_login_otp_validate: ... 401`` -- both *downstream*
symptoms of the re-login fallback -- and never ``user_already_exists``. The audit
trail blamed the OTP layer for a mailbox that was simply used, which is what sent
a whole diagnosis down the OTP path.

Two consequences are pinned here:

1. ``_registration_outcome`` reports ``user_already_exists`` rather than the
   fallback's symptom. ``create_ok`` is deliberately flipped to True for this
   case (see ``registration_handlers.create_account``), so
   ``_create_account_error`` returns ``""`` and the cause has to come from
   ``_existing_account_error``.
2. The classification is ``account``: not retryable, batch-dropped. Each retry of
   a used address spends another OTP (``resend`` answered 429 by the second
   attempt), so the class is what makes this save quota instead of merely
   reading better.
"""

import unittest

from sms_tool import registration
from sms_tool.error_classification import ACCOUNT_ERROR_MARKERS, classify_error
from sms_tool.failure_registry import BATCH_DROPPED_CLASSES, BATCH_RETRY_CLASSES
from sms_tool.registration_outcome import _existing_account_error
from sms_tool.registration_policy import registration_retry_decision


# Verbatim shape from the live 2026-09-14 response body.
_CREATE_DATA = {
    "error": {
        "message": "An account already exists for this email address, please login instead.",
        "type": "invalid_request_error",
        "param": None,
        "code": "user_already_exists",
        "redirect_uri": "https://chatgpt.com/auth/login_with?callback_path=/",
        "userAlreadyExistsRecovery": {"action": "continue_to_login"},
    }
}

# The fallback symptom that used to be reported instead of the real cause.
_FALLBACK_SYMPTOM = "existing_login_otp_send_failed:429"


class ExistingAccountErrorHelperTests(unittest.TestCase):
    """``_existing_account_error`` extracts the cause, and only that cause."""

    def test_user_already_exists_is_reported_with_the_recovery_action(self):
        self.assertEqual(
            _existing_account_error(_CREATE_DATA),
            "existing_account_user_already_exists:continue_to_login",
        )

    def test_missing_recovery_object_still_reports_the_cause(self):
        self.assertEqual(
            _existing_account_error({"error": {"code": "user_already_exists"}}),
            "existing_account_user_already_exists",
        )

    def test_non_dict_recovery_object_adds_no_suffix(self):
        data = {"error": {"code": "user_already_exists", "userAlreadyExistsRecovery": "nope"}}

        self.assertEqual(
            _existing_account_error(data), "existing_account_user_already_exists"
        )

    def test_blank_recovery_action_adds_no_suffix(self):
        data = {"error": {"code": "user_already_exists", "userAlreadyExistsRecovery": {"action": "  "}}}

        self.assertEqual(
            _existing_account_error(data), "existing_account_user_already_exists"
        )

    def test_another_create_error_is_not_claimed(self):
        self.assertEqual(_existing_account_error({"error": {"code": "invalid_state"}}), "")

    def test_non_dict_and_empty_payloads_are_safe(self):
        for value in (None, [], "user_already_exists", {}, {"error": None}):
            with self.subTest(value=value):
                self.assertEqual(_existing_account_error(value), "")


class RegistrationOutcomePrefersTheRealCauseTests(unittest.TestCase):
    """The outcome must not collapse to the fallback's OTP symptom."""

    def test_used_address_beats_the_fallback_symptom(self):
        success, error, _ = registration._registration_outcome(
            True, _CREATE_DATA, "", {}, _FALLBACK_SYMPTOM
        )

        self.assertFalse(success)
        self.assertEqual(error, "existing_account_user_already_exists:continue_to_login")
        self.assertNotIn("429", error)

    def test_used_address_is_classified_account_and_never_retried(self):
        """The quota-saving assertion: a used address must not be re-attempted."""

        _, error, _ = registration._registration_outcome(
            True, _CREATE_DATA, "", {}, _FALLBACK_SYMPTOM
        )
        decision = registration_retry_decision(error)

        self.assertEqual(decision.failure_class, "account")
        self.assertFalse(decision.retryable)
        self.assertNotEqual(decision.failure_class, "auth_state")
        self.assertNotIn("account", BATCH_RETRY_CLASSES)

    def test_used_address_marks_the_account_dropped(self):
        self.assertIn("account", BATCH_DROPPED_CLASSES)

    def test_marker_is_registered_on_the_account_class(self):
        """A future deletion of the registry marker must fail here, not in prod."""

        self.assertIn("user_already_exists", ACCOUNT_ERROR_MARKERS)
        self.assertEqual(classify_error("user_already_exists"), "account")
        self.assertEqual(
            classify_error("existing_account_user_already_exists:continue_to_login"),
            "account",
        )

    def test_genuine_create_failure_still_outranks_a_used_address(self):
        """``create_ok=False`` means the create really failed -- keep that cause."""

        data = {
            "error": {
                "code": "user_already_exists",
                "message": "An account already exists for this email address, please login instead.",
            }
        }

        _, error, _ = registration._registration_outcome(
            False, data, "", {}, _FALLBACK_SYMPTOM
        )

        self.assertIn("create_account_failed", error)
        self.assertIn("user_already_exists", error)
        self.assertNotIn("429", error)

    def test_fallback_symptom_still_wins_when_the_address_is_not_flagged(self):
        """Regression: the pre-existing precedence is untouched."""

        _, error, _ = registration._registration_outcome(
            True, {}, "", {}, _FALLBACK_SYMPTOM
        )

        self.assertEqual(error, _FALLBACK_SYMPTOM)
        self.assertEqual(classify_error(error), "unknown")


if __name__ == "__main__":
    unittest.main()
