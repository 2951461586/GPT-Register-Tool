import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import storage
from sms_tool.error_classification import classify_error
from sms_tool.registration_outcome import _registration_outcome, _retain_registration_checkpoint


class RegistrationCheckpointTests(unittest.TestCase):
    def test_post_create_transport_failure_retains_checkpoint_for_probe_retry(self):
        self.assertTrue(
            _retain_registration_checkpoint(
                False,
                "at-present",
                {"status_code": 0, "error": "SSL_connect failed"},
            )
        )
        self.assertFalse(
            _retain_registration_checkpoint(
                False,
                "at-present",
                {"status_code": 401, "error": "token_invalid"},
            )
        )
        self.assertFalse(
            _retain_registration_checkpoint(
                True,
                "at-present",
                {"status_code": 200},
            )
        )

    def test_edge_blocked_403_retains_the_checkpoint(self):
        """P1-B: a CF edge 403 is an egress verdict, not an account verdict.

        The token may be fine and only the exit was blocked; clearing the
        checkpoint re-submits the whole signup and hits ``user_already_exists``.
        """
        self.assertTrue(
            _retain_registration_checkpoint(
                False,
                "at-present",
                {"status_code": 403, "status": "unknown"},
            )
        )
        # No ``status`` at all is still an edge verdict, not a token verdict.
        self.assertTrue(
            _retain_registration_checkpoint(
                False,
                "at-present",
                {"status_code": 403},
            )
        )

    def test_a_token_verdict_still_drops_the_checkpoint(self):
        self.assertFalse(
            _retain_registration_checkpoint(
                False,
                "at-present",
                {"status_code": 403, "status": "token_invalid"},
            )
        )

    def test_the_edge_blocked_suffix_names_the_cause_without_reclassifying(self):
        _success, error, _create_error = _registration_outcome(
            True, {}, "at-present", {"status_code": 403, "status": "unknown"}
        )
        self.assertEqual(error, "access_token_probe_http_403:edge_blocked")
        # The leading token is unchanged, so the failure class is unchanged.
        self.assertEqual(classify_error(error), classify_error("access_token_probe_http_403"))

        _success, plain, _create_error = _registration_outcome(
            True, {}, "at-present", {"status_code": 403, "status": "token_invalid"}
        )
        self.assertEqual(plain, "access_token_probe_http_403")

    def test_checkpoint_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(
                    storage.save_registration_checkpoint(
                        "User@Example.com",
                        "at_probe_pending",
                        {"access_token": "at", "device_id": "did"},
                    )
                )
                checkpoint = storage.get_registration_checkpoint("user@example.com")
                self.assertEqual(checkpoint["state"], "at_probe_pending")
                self.assertEqual(checkpoint["payload"]["device_id"], "did")
                self.assertTrue(storage.clear_registration_checkpoint("user@example.com"))
                self.assertEqual(storage.get_registration_checkpoint("user@example.com"), {})


if __name__ == "__main__":
    unittest.main()
