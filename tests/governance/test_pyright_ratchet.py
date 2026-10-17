"""``scripts/pyright_ratchet.py`` — the type-error ratchet.

The ratchet's dangerous failure mode is passing while the checker never ran
(a missing pyright, an unparseable payload, a baseline that lost its rows).
These tests pin the plumbing, not pyright's verdict on the tree: the tree check
lives in ``test_script_reports_ok_on_the_current_tree`` and is the one that runs
the real checker.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import pyright_ratchet as pr


class CountErrors(unittest.TestCase):
    def test_only_errors_are_counted_and_paths_are_repo_relative(self):
        payload = {
            "generalDiagnostics": [
                {"file": str(pr.ROOT / "sms_tool" / "a.py"), "severity": "error"},
                {"file": str(pr.ROOT / "sms_tool" / "a.py"), "severity": "error"},
                {"file": str(pr.ROOT / "sms_tool" / "b.py"), "severity": "warning"},
                {"file": str(pr.ROOT / "sms_tool" / "c.py"), "severity": "information"},
            ]
        }
        errors, warnings = pr.count_errors(payload)
        self.assertEqual(dict(errors), {"sms_tool/a.py": 2})
        self.assertEqual(dict(warnings), {"sms_tool/b.py": 1})

    def test_empty_payload_yields_empty_counters(self):
        errors, warnings = pr.count_errors({"generalDiagnostics": []})
        self.assertEqual(dict(errors), {})
        self.assertEqual(dict(warnings), {})


class Growth(unittest.TestCase):
    def test_a_file_absent_from_the_baseline_has_zero_allowance(self):
        """A brand-new file must be clean, not inherit someone else's budget."""
        from collections import Counter

        regressions = pr.grown(Counter({"sms_tool/new.py": 1}), {})
        self.assertEqual(regressions, [("sms_tool/new.py", 1, 0)])

    def test_a_file_at_or_below_its_allowance_is_clean(self):
        from collections import Counter

        errors = Counter({"sms_tool/a.py": 3, "sms_tool/b.py": 1})
        allowed = {"sms_tool/a.py": 3, "sms_tool/b.py": 5}
        self.assertEqual(pr.grown(errors, allowed), [])

    def test_a_shrink_in_one_file_cannot_mask_growth_in_another(self):
        """The whole reason the baseline is per-file rather than one total."""
        from collections import Counter

        errors = Counter({"sms_tool/shrunk.py": 0, "sms_tool/grown.py": 2})
        allowed = {"sms_tool/shrunk.py": 10, "sms_tool/grown.py": 1}
        self.assertEqual(pr.grown(errors, allowed), [("sms_tool/grown.py", 2, 1)])


class BaselineLoading(unittest.TestCase):
    def test_corrupt_baseline_loads_as_empty_so_every_file_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(pr.load_baseline(path), {})

    def test_missing_baseline_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pr.load_baseline(Path(tmp) / "nope.json"), {})

    def test_non_int_entries_are_skipped_not_crashed(self):
        self.assertEqual(pr.baseline_per_file({"per_file": {"a.py": "x", "b.py": 2}}), {"b.py": 2})


class SafetyRails(unittest.TestCase):
    def test_run_pyright_raises_when_the_checker_produces_nothing(self):
        """A broken checker must never look like a clean tree."""
        original = pr.subprocess.run

        class _Result:
            stdout = ""
            stderr = "pyright not found"
            returncode = 127

        pr.subprocess.run = lambda *a, **k: _Result()  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                pr.run_pyright()
        finally:
            pr.subprocess.run = original  # type: ignore[assignment]

    def test_run_pyright_raises_on_unparseable_json(self):
        original = pr.subprocess.run

        class _Result:
            stdout = "<html>not json</html>"
            stderr = ""
            returncode = 0

        pr.subprocess.run = lambda *a, **k: _Result()  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                pr.run_pyright()
        finally:
            pr.subprocess.run = original  # type: ignore[assignment]


class RealTree(unittest.TestCase):
    def test_baseline_exists_and_pins_a_total(self):
        self.assertTrue(pr.BASELINE.is_file(), f"missing baseline: {pr.BASELINE}")
        data = json.loads(pr.BASELINE.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("total"), int)
        self.assertIsInstance(data.get("per_file"), dict)
        # The pinned total must equal the sum of the rows, or a lost row would
        # silently lower the gate.
        self.assertEqual(data["total"], sum(pr.baseline_per_file(data).values()))

    def test_script_reports_ok_on_the_current_tree(self):
        self.assertEqual(0, pr.main([]), "pyright regressed above the frozen baseline")


if __name__ == "__main__":
    unittest.main()
