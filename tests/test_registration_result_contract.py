"""Registration result contract tests.

The protocol path (registration_handlers.finalize) and the browser path
(browser_flow/orchestrator.run_browser_registration) must assemble their
result dicts through the shared builder, so the common key set cannot drift
between the two registration modes.
"""

import ast
import unittest
from pathlib import Path

from sms_tool.registration_result import COMMON_RESULT_KEYS, build_registration_result

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDLERS = REPO_ROOT / "sms_tool" / "registration_handlers.py"
ORCHESTRATOR = REPO_ROOT / "sms_tool" / "registration_drivers" / "browser_flow" / "orchestrator.py"


class BuildRegistrationResultTests(unittest.TestCase):
    def test_result_contains_every_common_key(self):
        result = build_registration_result(
            success=True,
            registration_mode="browser",
            registration_state="active",
            email="user@example.com",
        )
        self.assertTrue(COMMON_RESULT_KEYS <= result.keys())

    def test_extra_keys_are_merged_after_the_common_core(self):
        result = build_registration_result(
            success=False,
            registration_mode="browser",
            registration_state="failed",
            extra={"registration_driver": "camoufox"},
        )
        self.assertEqual(result["registration_driver"], "camoufox")
        self.assertFalse(result["success"])

    def test_free_text_fields_are_sanitized(self):
        result = build_registration_result(
            success=False,
            registration_mode="protocol",
            registration_state="failed",
            error="boom https://user:pass@proxy.example",
            registration_warning="warn sk_live_abc123def456ghi789",
        )
        self.assertNotIn("pass@", result["error"])
        self.assertNotIn("sk_live_abc123def456ghi789", result["registration_warning"])

    def test_twofa_enrollment_defaults_to_skipped(self):
        result = build_registration_result(
            success=True,
            registration_mode="protocol",
            registration_state="active",
        )
        self.assertEqual(result["twofa_enrollment"], {"ok": False, "reason": "skipped"})


class SharedAssemblyGuardTests(unittest.TestCase):
    """Both paths must assemble results through the shared builder."""

    def test_both_paths_call_the_shared_builder(self):
        for path in (HANDLERS, ORCHESTRATOR):
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and (
                        (isinstance(node.func, ast.Name) and node.func.id == "build_registration_result")
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "build_registration_result"
                        )
                    )
                ]
                self.assertEqual(len(calls), 1)

    def test_no_raw_result_literal_with_contract_keys_remains(self):
        # Failure-path dicts and the resume checkpoint legitimately carry a
        # few common keys, so the guard only targets the exact hand-rolled
        # assembly shape: a dict literal assigned to a variable named
        # ``result`` that sets assembly-only keys.
        sentinel_keys = {"register_method", "session_type", "plan_type", "registration_success_basis"}
        for path in (HANDLERS, ORCHESTRATOR):
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    if not any(
                        isinstance(target, ast.Name) and target.id == "result"
                        for target in node.targets
                    ):
                        continue
                    value = node.value
                    if not isinstance(value, ast.Dict):
                        continue
                    keys = {
                        key.value
                        for key in value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
                    overlap = keys & sentinel_keys
                    self.assertEqual(
                        set(),
                        overlap,
                        f"{path.name}: hand-rolled result assembly reintroduced "
                        f"with contract keys {sorted(overlap)}",
                    )


if __name__ == "__main__":
    unittest.main()
