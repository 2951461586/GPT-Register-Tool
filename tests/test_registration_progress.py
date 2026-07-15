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
                registration_progress.registration_stage("payment_link")
                return {"success": True, "email": "user@example.com"}

            with patch.object(registration_progress, "runtime_file", return_value=path):
                result = run()

            self.assertEqual(result["registration_progress"]["last_stage"], "completed")
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertTrue(stored["success"])
            self.assertEqual([item["stage"] for item in stored["events"]][-3:], ["auth_flow", "payment_link", "completed"])


if __name__ == "__main__":
    unittest.main()
