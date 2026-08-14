import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import storage
from sms_tool.registration_outcome import _retain_registration_checkpoint


class RegistrationCheckpointTests(unittest.TestCase):
    def test_post_create_transport_failure_retains_checkpoint_for_probe_retry(self):
        self.assertTrue(_retain_registration_checkpoint(
            False,
            "at-present",
            {"status_code": 0, "error": "SSL_connect failed"},
        ))
        self.assertFalse(_retain_registration_checkpoint(
            False,
            "at-present",
            {"status_code": 401, "error": "token_invalid"},
        ))
        self.assertFalse(_retain_registration_checkpoint(
            True,
            "at-present",
            {"status_code": 200},
        ))

    def test_checkpoint_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.save_registration_checkpoint(
                    "User@Example.com",
                    "at_probe_pending",
                    {"access_token": "at", "device_id": "did"},
                ))
                checkpoint = storage.get_registration_checkpoint("user@example.com")
                self.assertEqual(checkpoint["state"], "at_probe_pending")
                self.assertEqual(checkpoint["payload"]["device_id"], "did")
                self.assertTrue(storage.clear_registration_checkpoint("user@example.com"))
                self.assertEqual(storage.get_registration_checkpoint("user@example.com"), {})


if __name__ == "__main__":
    unittest.main()
