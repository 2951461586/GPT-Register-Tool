import unittest

from sms_tool.auth_state import auth_dump_summary


class AuthStateTests(unittest.TestCase):
    def test_auth_dump_summary_redacts_sensitive_state_values(self):
        summary = auth_dump_summary({
            "client_auth_session": {
                "session_id": "sess_abcdefghijklmnopqrstuvwxyz",
                "login_verifier": "verifier_abcdefghijklmnopqrstuvwxyz",
            },
            "state": "state_abcdefghijklmnopqrstuvwxyz",
        })

        signals = summary["signals"]
        self.assertIn("client_auth_session.session_id", signals)
        self.assertIn("client_auth_session.login_verifier", signals)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(signals))
        self.assertIn("(len=", str(signals))


if __name__ == "__main__":
    unittest.main()
