import unittest
import time
import ast
from pathlib import Path

from sms_tool.registration_state import (
    RegistrationState,
    RegistrationStage,
    RegistrationStateMachine,
    prepare_registration_context,
)
from sms_tool.registration_handlers import BoundRegistrationStage, RegistrationStageRunner


class Mailbox:
    email = "user@example.com"


class RegistrationStateTests(unittest.TestCase):
    def test_stage_runner_shares_state_and_runs_cleanup(self):
        events = []
        machine = RegistrationStateMachine(lambda *event: events.append(event))
        context = object()
        cleaned = []
        runner = RegistrationStageRunner(context, machine, cleanup=lambda state: cleaned.append(dict(state)))
        first = BoundRegistrationStage(
            RegistrationStage(RegistrationState.AUTH_FLOW, lambda _ctx: {"auth": "ok"}),
            lambda *_: {"auth": "ok"},
        )
        second = BoundRegistrationStage(
            RegistrationStage(RegistrationState.ACCESS_TOKEN_PROBE, lambda _ctx: {"probe": 200}),
            lambda *_: {"probe": 200},
        )
        # Stage handlers are represented by the stage callback in this minimal contract.
        result = runner.run([first, second])
        self.assertEqual(result, {"auth": "ok", "probe": 200})
        self.assertEqual(cleaned, [{"auth": "ok", "probe": 200}])

    def test_stage_runner_single_stage_is_the_production_execution_seam(self):
        machine = RegistrationStateMachine(lambda *_: None)
        runner = RegistrationStageRunner(object(), machine)
        self.assertEqual(
            runner.run_stage(RegistrationState.AUTH_FLOW, lambda: "ok", timeout_seconds=1),
            "ok",
        )

    def test_handlers_do_not_reverse_import_registration_facade(self):
        path = Path(__file__).resolve().parents[1] / "sms_tool" / "registration_handlers.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        imported_modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        self.assertNotIn("registration", imported_modules)
    def test_stage_handler_applies_common_timeout_and_failure_transition(self):
        machine = RegistrationStateMachine(lambda *_: None)
        stage = RegistrationStage(RegistrationState.AUTH_FLOW, lambda _context: time.sleep(0.05), timeout_seconds=0.001)
        with self.assertRaises(TimeoutError):
            stage.run(object(), machine)
        self.assertEqual(machine.snapshot()["state"], "failed")

    def test_state_machine_allows_forward_skips_and_rejects_backtracking(self):
        events = []
        machine = RegistrationStateMachine(lambda state, status, detail: events.append((state, status, detail)))
        machine.transition(RegistrationState.MAILBOX_READY)
        machine.transition(RegistrationState.AUTH_FLOW)

        with self.assertRaises(ValueError):
            machine.transition(RegistrationState.SENTINEL)

        self.assertEqual(machine.snapshot()["state"], "auth_flow")
        self.assertEqual([event[0] for event in events], ["mailbox_ready", "auth_flow"])

    def test_context_reuses_device_and_stored_password_without_exposing_them_in_repr(self):
        context = prepare_registration_context(
            proxy="http://proxy.example:8080",
            mailbox=Mailbox(),
            sentinel_data={"sentinel_token": "secret-sentinel"},
            password=None,
            registration_mode="password",
            auth_base="https://auth.example",
            chat_base="https://chat.example",
            stored_password=lambda _email: "StoredPassword!1",
            generate_password=lambda: "GeneratedPassword!1",
            random_name=lambda: ("Ada", "Lovelace"),
            random_birthdate=lambda: "1990-01-01",
            normalize_mode=lambda value: str(value),
            get_device_context=lambda _email: {
                "device_id": "existing-device",
                "auth_session_logging_id": "existing-log",
            },
            sentinel_device_id=lambda _data: "sentinel-device",
            new_uuid=lambda: "new-uuid",
        )

        self.assertEqual(context.password, "StoredPassword!1")
        self.assertEqual(context.device_id, "existing-device")
        self.assertNotIn("StoredPassword!1", repr(context))
        self.assertNotIn("secret-sentinel", repr(context))


if __name__ == "__main__":
    unittest.main()
