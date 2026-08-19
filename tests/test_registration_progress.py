import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import registration_progress


class RegistrationProgressTests(unittest.TestCase):
    def test_decorator_attaches_and_persists_stage_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"

            @registration_progress.track_registration
            def run(**kwargs):
                registration_progress.registration_stage("auth_flow")
                registration_progress.registration_stage("access_token_probe")
                return {"success": True, "email": "user@example.com"}

            with patch.object(registration_progress, "runtime_file", return_value=path):
                result = run()

            self.assertEqual(result["registration_progress"]["last_stage"], "completed")
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertTrue(stored["success"])
            self.assertEqual([item["stage"] for item in stored["events"]][-3:], ["auth_flow", "access_token_probe", "completed"])

    def test_persist_does_not_duplicate_an_existing_terminal_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            progress.stage("auth_flow")
            progress.stage("failed", "failed", "signup_auth_state")

            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({"success": False, "error": "signup_auth_state"})

            stored = json.loads(path.read_text(encoding="utf-8").strip())
            failed = [item for item in stored["events"] if item["stage"] == "failed"]
            self.assertEqual(1, len(failed))


if __name__ == "__main__":
    unittest.main()
