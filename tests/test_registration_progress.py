import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import registration_progress


class RegistrationProgressTests(unittest.TestCase):
    def test_duration_is_attached_to_stage_being_exited(self):
        progress = registration_progress.RegistrationProgress("user@example.com")
        with patch.object(
            registration_progress.time,
            "monotonic",
            side_effect=[progress._stage_started_monotonic + 1.25],
        ):
            progress.stage("auth_flow")

        self.assertEqual(progress.events[0]["stage"], "started")
        self.assertEqual(progress.events[0]["duration_ms"], 1250)
        self.assertEqual(progress.events[1]["stage"], "auth_flow")
        self.assertEqual(progress.events[1]["duration_ms"], 0)

    def test_first_log_record_carries_the_progress_run_id(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                from sms_tool.logging_setup import CorrelatedJsonFormatter

                records.append(json.loads(CorrelatedJsonFormatter().format(record)))

        handler = Capture()
        logger = logging.getLogger(registration_progress.__name__)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        token = registration_progress.current_run_id.set("bound-run")
        try:
            registration_progress.RegistrationProgress(run_id="bound-run")
        finally:
            registration_progress.current_run_id.reset(token)
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        self.assertTrue(records)
        self.assertEqual(records[0]["run_id"], "bound-run")

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

    def test_invalid_driver_still_persists_a_failed_row(self):
        """Driver/config resolution failures must not vanish before tracking starts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"

            @registration_progress.track_registration
            def run(**kwargs):
                raise ValueError("unsupported_registration_driver")

            with patch.object(registration_progress, "runtime_file", return_value=path):
                with self.assertRaises(ValueError):
                    run(registration_driver="not-a-driver")

            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertFalse(stored["success"])
            self.assertEqual(stored["registration_driver"], "not-a-driver")
            self.assertEqual(stored["events"][-1]["status"], "failed")

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

    def test_persist_does_not_duplicate_successful_completed_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            progress.stage("completed", "success")

            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({"success": True})

            stored = json.loads(path.read_text(encoding="utf-8").strip())
            completed = [
                item for item in stored["events"] if item["stage"] == "completed"
            ]
            self.assertEqual(1, len(completed))
            self.assertEqual("success", completed[0]["status"])

    def test_persist_includes_batch_and_retry_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({
                    "success": False,
                    "error": "browser_registration_state_unknown",
                    "batch_id": "batch-1",
                    "registration_attempts": 2,
                    "failure_class": "auth_state",
                    "retryable": True,
                    "registration_state": "retry_pending",
                    "proxy_audit": {"pool_index": 0},
                })
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(stored["batch_id"], "batch-1")
            self.assertEqual(stored["attempt"], 2)
            self.assertEqual(stored["failure_class"], "auth_state")
            self.assertTrue(stored["retryable"])
            self.assertEqual(stored["proxy_pool_index"], 0)

    def test_constructor_preserves_batch_and_attempt_when_result_omits_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress(
                "user@example.com", batch_id="batch-2", attempt=2
            )
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({"success": False, "error": "registration_cancelled", "registration_state": "cancelled"})
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(stored["batch_id"], "batch-2")
            self.assertEqual(stored["attempt"], 2)
            self.assertEqual(stored["events"][-1]["status"], "cancelled")

    def test_progress_row_uses_account_reference_and_rotates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            registration_progress._append_progress_row(
                path, {"account_ref": "first"}, max_bytes=30, backups=2
            )
            registration_progress._append_progress_row(
                path, {"account_ref": "second"}, max_bytes=30, backups=2
            )

            self.assertTrue(path.with_name("progress.jsonl.1").is_file())
            self.assertIn("second", path.read_text(encoding="utf-8"))

    def test_failed_row_persists_browser_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({
                    "success": False,
                    "error": "browser_email_verification_stuck",
                    "browser_diagnostics": {
                        "driver": "camoufox",
                        "url_path": "/u/email-verification",
                        "verification_inputs": 1,
                    },
                })
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(stored["browser_diagnostics"]["url_path"], "/u/email-verification")
            self.assertEqual(stored["browser_diagnostics"]["driver"], "camoufox")

    def test_success_and_undiagnosed_rows_omit_browser_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({
                    "success": True,
                    "browser_diagnostics": {"driver": "camoufox", "url_path": "/"},
                })
                progress_no_diag = registration_progress.RegistrationProgress("second@example.com")
                progress_no_diag.persist({"success": False, "error": "browser_email_otp_timeout"})
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertNotIn("browser_diagnostics", rows[0])
            self.assertNotIn("browser_diagnostics", rows[1])


    def test_stage_event_carries_failure_class(self):
        """The event must be aggregatable by failure class while the run is live.

        Before 2026-09-11 the class only appeared on the persisted row, which
        does not exist until the run finishes -- so a batch with 56% failures
        could not be triaged until it was over.
        """
        progress = registration_progress.RegistrationProgress("user@example.com")
        progress.stage(
            "email_otp_wait", "failed", "email_otp_poll_timeout", failure_class="mailbox"
        )
        event = progress.events[-1]
        self.assertEqual(event["stage"], "email_otp_wait")
        self.assertEqual(event["failure_class"], "mailbox")

    def test_stage_event_failure_class_defaults_to_empty(self):
        progress = registration_progress.RegistrationProgress("user@example.com")
        progress.stage("auth_flow")
        self.assertEqual(progress.events[-1]["failure_class"], "")

    def test_module_level_registration_stage_forwards_failure_class(self):
        progress = registration_progress.RegistrationProgress("user@example.com")
        registration_progress._current.set(progress)
        try:
            registration_progress.registration_stage("failed", "failed", failure_class="mailbox")
        finally:
            registration_progress._current.set(None)
        self.assertEqual(progress.events[-1]["failure_class"], "mailbox")

    def test_persist_terminal_event_carries_derived_failure_class(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({"success": False, "error": "email_otp_poll_timeout"})

            stored = json.loads(path.read_text(encoding="utf-8").strip())
            terminal = stored["events"][-1]
            self.assertEqual(terminal["stage"], "failed")
            self.assertEqual(terminal["failure_class"], "mailbox")
            self.assertEqual(stored["failure_class"], "mailbox")

    def test_persist_backfills_failure_class_on_caller_emitted_terminal_stage(self):
        """The registration loop stages 'failed' itself before persisting.

        That is the common production path, so backfilling is what actually
        makes the operator log aggregatable -- emitting it only on events this
        method creates would leave the real failures unclassified.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.jsonl"
            progress = registration_progress.RegistrationProgress("user@example.com")
            progress.stage("failed", "failed", "email_otp_poll_timeout")
            with patch.object(registration_progress, "runtime_file", return_value=path):
                progress.persist({"success": False, "error": "email_otp_poll_timeout"})

            stored = json.loads(path.read_text(encoding="utf-8").strip())
            failed = [item for item in stored["events"] if item["stage"] == "failed"]
            self.assertEqual(1, len(failed))
            self.assertEqual(failed[0]["failure_class"], "mailbox")


if __name__ == "__main__":
    unittest.main()
