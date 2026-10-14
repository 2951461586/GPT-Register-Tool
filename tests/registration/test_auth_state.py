import unittest

from sms_tool.auth_state import (
    TRANSACTION_ENUM_VALUES,
    auth_dump_summary,
    compact_auth_dump_text,
)


class AuthStateTests(unittest.TestCase):
    def test_auth_dump_summary_redacts_sensitive_state_values(self):
        summary = auth_dump_summary(
            {
                "client_auth_session": {
                    "session_id": "sess_abcdefghijklmnopqrstuvwxyz",
                    "login_verifier": "verifier_abcdefghijklmnopqrstuvwxyz",
                },
                "state": "state_abcdefghijklmnopqrstuvwxyz",
            }
        )

        signals = summary["signals"]
        self.assertIn("client_auth_session.session_id", signals)
        self.assertIn("client_auth_session.login_verifier", signals)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(signals))
        self.assertIn("(len=", str(signals))

    def test_transaction_arm_enum_values_stay_readable(self):
        """The enum *value* is the arm discriminator; redacting it destroyed

        the stuck-run attribution. Measured 2026-10-06 (batches ed33aaf3 /
        b8937a58 / aef68999, 14 stuck runs): every summary printed
        ``email_verification_mode: [REDACTED](len=10/19)`` — the length was
        indistinguishable between arms, so no run could be attributed to the
        passwordless vs login arm.
        """
        summary = auth_dump_summary(
            {
                "client_auth_session": {
                    "email_verification_mode": "passwordless_signup",
                    "signup_mode": "signup",
                    "original_screen_hint": "login_or_signup",
                    "app_name_enum": "chat",
                    "passwordless_disabled": False,
                    # A secret-looking neighbour stays redacted next to them.
                    "session_id": "sess_abcdefghijklmnopqrstuvwxyz",
                },
            }
        )

        signals = summary["signals"]
        self.assertEqual(signals.get("client_auth_session.email_verification_mode"), "passwordless_signup")
        self.assertEqual(signals.get("client_auth_session.signup_mode"), "signup")
        self.assertEqual(signals.get("client_auth_session.original_screen_hint"), "login_or_signup")
        self.assertEqual(signals.get("client_auth_session.app_name_enum"), "chat")
        # Bools already pass through untouched (non-string values).
        self.assertIs(signals.get("client_auth_session.passwordless_disabled"), False)
        self.assertTrue(signals.get("client_auth_session.session_id", "").startswith("[REDACTED]"))

    def test_unknown_enum_values_stay_redacted(self):
        """The allow-list is exact-match: an arbitrary server string never

        reaches the log. A value outside TRANSACTION_ENUM_VALUES keeps the
        length-only form, so a future server-side enum rename degrades to the
        old (unattributable) behaviour instead of echoing arbitrary strings.
        """
        summary = auth_dump_summary(
            {
                "client_auth_session": {
                    "email_verification_mode": "something_new_and_arbitrary",
                    "signup_mode": "not-a-known-enum-value",
                },
            }
        )

        signals = summary["signals"]
        self.assertEqual(signals.get("client_auth_session.email_verification_mode"), "[REDACTED](len=27)")
        self.assertEqual(signals.get("client_auth_session.signup_mode"), "[REDACTED](len=22)")

    def test_the_compacted_line_carries_the_mode_for_a_stuck_shape(self):
        """End-to-end: the printed (compacted) line for the live stuck shape

        contains the arm enum, so the next stuck run is attributable from the
        log alone — the whole point of the 2026-10-06 observability gap.
        """
        summary = auth_dump_summary(
            {
                "top": 1,
                "client_auth_session": {
                    "email": "user@example.com",
                    "email_verification_mode": "passwordless_signup",
                    "passwordless_email_otp_send_pending": True,
                    "session_id": "sess_abcdefghijklmnopqrstuvwxyz",
                    "username": "user@example.com",
                },
            }
        )
        text = compact_auth_dump_text("offline_stuck_test", summary)
        self.assertIn("passwordless_signup", text)
        self.assertNotIn("user@example.com", text)
        self.assertIn("[REDACTED]", text)

    def test_enum_allowlist_is_exact_match_only(self):
        """Guard the allow-list's safety property: no prefixes, no patterns."""
        for value in TRANSACTION_ENUM_VALUES:
            self.assertEqual(value, value.strip())
            self.assertTrue(value.isalnum() or all(c.isalnum() or c == "_" for c in value))


if __name__ == "__main__":
    unittest.main()
