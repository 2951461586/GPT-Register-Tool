"""Cooperative cancellation wiring (registration_cancel + batch/pulse/protocol).

Covers the four seams the cancel flag touches:
- the flag primitives and the cancel_scope ownership contract,
- batch_runner checkpoints (before an account, between attempts, retry sleep),
- pulse-wave cancellation of untouched accounts,
- the protocol workflow converting RegistrationCancelled into the same
  cancelled result contract the browser path reports.
"""

import signal
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from sms_tool import registration_cancel
from sms_tool.batch_runner import run_batch_impl
from sms_tool.registration_cancel import (
    RegistrationCancelled,
    cancellable_sleep,
    cancel_scope,
    clear_registration_cancel,
    ensure_not_cancelled,
    registration_cancel_requested,
    request_registration_cancel,
)
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_outcome import _failure_result
from sms_tool.registration_pulse import PulseConfig, run_pulse_batch
from sms_tool.registration_state import RegistrationStateMachine


def _mailbox(email="user@example.com"):
    mailbox = Mock()
    mailbox.email = email
    return mailbox


class CancelFlagTests(unittest.TestCase):
    def tearDown(self):
        clear_registration_cancel()

    def test_request_and_clear_roundtrip(self):
        self.assertFalse(registration_cancel_requested())
        request_registration_cancel()
        self.assertTrue(registration_cancel_requested())
        with self.assertRaises(RegistrationCancelled):
            ensure_not_cancelled()
        clear_registration_cancel()
        self.assertFalse(registration_cancel_requested())
        ensure_not_cancelled()  # no raise after clear

    def test_ensure_not_cancelled_raises(self):
        request_registration_cancel()
        with self.assertRaises(RegistrationCancelled):
            ensure_not_cancelled()
        self.assertEqual(RegistrationCancelled().code, "registration_cancelled")
        self.assertEqual(str(RegistrationCancelled()), "registration_cancelled")

    def test_cancellable_sleep_reports_cancel_immediately(self):
        request_registration_cancel()
        self.assertTrue(cancellable_sleep(5.0))

    def test_cancellable_sleep_reports_custom_predicate(self):
        called = []

        def requested():
            called.append(1)
            return bool(called)  # True on first poll

        self.assertTrue(cancellable_sleep(5.0, poll_interval=0.05, requested=requested))

    def test_cancellable_sleep_elapsed_without_cancel(self):
        self.assertFalse(cancellable_sleep(0.05))

    def test_cancel_scope_clears_flag_on_exit(self):
        request_registration_cancel()
        with cancel_scope(install_signals=False):
            self.assertTrue(registration_cancel_requested())
        self.assertFalse(registration_cancel_requested())

    def test_cancel_scope_clears_flag_set_inside(self):
        with cancel_scope(install_signals=False):
            request_registration_cancel()
        self.assertFalse(registration_cancel_requested())

    def test_cancel_scope_restores_signal_handlers(self):
        previous_int = signal.getsignal(signal.SIGINT)
        try:
            with cancel_scope():
                self.assertIsNot(signal.getsignal(signal.SIGINT), previous_int)
            self.assertIs(signal.getsignal(signal.SIGINT), previous_int)
        finally:
            signal.signal(signal.SIGINT, previous_int)

    def test_installed_handler_sets_flag_then_hard_interrupts(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            registration_cancel._install_cancel_signal_handlers()
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertTrue(registration_cancel_requested())
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        finally:
            signal.signal(signal.SIGINT, previous)


class BatchCancelTests(unittest.TestCase):
    def test_cancel_before_batch_skips_every_account(self):
        request_registration_cancel()
        calls = []

        def run_email(**kwargs):
            calls.append(kwargs)
            return {"success": True}

        results = run_batch_impl(
            count=2,
            run_email_func=run_email,
            workers=1,
            registration_driver="protocol",
        )
        self.assertEqual(calls, [])
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result["error"], "registration_cancelled")
            self.assertEqual(result["failure_class"], "cancelled")
            self.assertEqual(result["registration_state"], "cancelled")
            self.assertFalse(result["retryable"])

    def test_cancel_scope_clears_flag_after_batch(self):
        def run_email(**kwargs):
            request_registration_cancel()
            return {
                "success": False,
                "error": "auth_flow_transport:timeout",
                "failure_class": "network",
            }

        results = run_batch_impl(
            count=1,
            run_email_func=run_email,
            workers=1,
            registration_driver="protocol",
        )
        self.assertEqual(results[0]["failure_class"], "cancelled")
        self.assertFalse(registration_cancel_requested())

    def test_cancel_between_attempts_skips_retry(self):
        attempts = []

        def run_email(**kwargs):
            attempts.append(kwargs.get("registration_attempt"))
            request_registration_cancel()
            return {
                "success": False,
                "error": "auth_flow_transport:timeout",
                "failure_class": "network",
            }

        results = run_batch_impl(
            count=1,
            run_email_func=run_email,
            workers=1,
            max_attempts=2,
            retry_delay_seconds=5.0,
            registration_driver="protocol",
        )
        self.assertEqual(attempts, [1])
        self.assertEqual(results[0]["error"], "registration_cancelled")
        self.assertEqual(results[0]["registration_attempts"], 1)

    def test_cancel_during_retry_sleep_returns_cancelled(self):
        def run_email(**kwargs):
            return {
                "success": False,
                "error": "auth_flow_transport:timeout",
                "failure_class": "network",
            }

        def slow_cancelled_sleep(seconds, **kwargs):
            request_registration_cancel()
            return True

        with patch(
            "sms_tool.registration_cancel.cancellable_sleep",
            side_effect=slow_cancelled_sleep,
        ):
            results = run_batch_impl(
                count=1,
                run_email_func=run_email,
                workers=1,
                max_attempts=2,
                retry_delay_seconds=5.0,
                registration_driver="protocol",
            )
        self.assertEqual(results[0]["error"], "registration_cancelled")

    def test_cancel_event_channel_is_honoured(self):
        event = threading.Event()
        event.set()
        calls = []

        def run_email(**kwargs):
            calls.append(kwargs)
            return {"success": True}

        results = run_batch_impl(
            count=1,
            run_email_func=run_email,
            workers=1,
            cancel_event=event,
            registration_driver="protocol",
        )
        self.assertEqual(calls, [])
        self.assertEqual(results[0]["failure_class"], "cancelled")


class PulseCancelTests(unittest.TestCase):
    def test_cancel_marks_remaining_accounts_without_running_them(self):
        request_registration_cancel()
        calls = []

        def run_one(idx):
            calls.append(idx)
            return idx, {"success": True}

        results = run_pulse_batch(3, run_one_fn=run_one, workers=1)
        self.assertEqual(calls, [])
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result["error"], "registration_cancelled")
            self.assertEqual(result["failure_class"], "cancelled")

    def test_pulse_wakes_from_wave_delay_on_cancel(self):
        def run_one(idx):
            request_registration_cancel()
            return idx, {"success": True}

        pulse_config = PulseConfig(wave_size=1, wave_delay_seconds=1.0)

        with patch(
            "sms_tool.registration_pulse.cancellable_sleep", return_value=True
        ) as sleep_mock:
            results = run_pulse_batch(
                2,
                run_one_fn=run_one,
                workers=1,
                pulse_config=pulse_config,
            )
        self.assertTrue(sleep_mock.called)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[1]["error"], "registration_cancelled")


class ProtocolCancelTests(unittest.TestCase):
    def _workflow(self, machine):
        operations = Mock()
        operations._tl.return_value = []
        operations.runtime_config_scope.return_value = nullcontext()
        operations._failure_result.side_effect = lambda error, **kwargs: _failure_result(
            error, **kwargs
        )
        return RegistrationEmailWorkflow(
            machine,
            config={"registration": {}},
            operations=operations,
        )

    def test_cancel_before_first_stage_returns_cancelled_result(self):
        request_registration_cancel()
        machine = RegistrationStateMachine(lambda *_: None)
        workflow = self._workflow(machine)
        workflow._bootstrap = Mock()
        workflow._resume_post_create = Mock(return_value=None)
        workflow._close_sessions = Mock()

        result = workflow.run()

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "registration_cancelled")
        self.assertEqual(result["failure_class"], "cancelled")
        self.assertEqual(result["registration_state"], "cancelled")
        workflow._bootstrap.assert_not_called()

    def test_cancel_between_stages_not_classified_as_transport_failure(self):
        machine = RegistrationStateMachine(lambda *_: None)
        workflow = self._workflow(machine)
        workflow._close_sessions = Mock()

        def _bootstrap():
            request_registration_cancel()

        workflow._bootstrap = _bootstrap
        workflow._resume_post_create = Mock(return_value=None)

        result = workflow.run()

        self.assertEqual(result["error"], "registration_cancelled")
        self.assertEqual(result["failure_class"], "cancelled")
        self.assertEqual(result["registration_state"], "cancelled")
        self.assertNotIn("registration_internal_error", result["error"])

    def test_cancel_raised_inside_stage_passes_through_stage_wrapper(self):
        machine = RegistrationStateMachine(lambda *_: None)
        workflow = self._workflow(machine)
        workflow._close_sessions = Mock()
        workflow._bootstrap = Mock()
        workflow._resume_post_create = Mock(return_value=None)

        def auth_flow():
            request_registration_cancel()
            raise RegistrationCancelled()

        workflow.auth_flow = auth_flow

        result = workflow.run()

        self.assertEqual(result["error"], "registration_cancelled")
        self.assertEqual(result["failure_class"], "cancelled")
        self.assertEqual(result["registration_state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
