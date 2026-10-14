"""Pure helpers of scripts/at_survival_watch.py."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "at_survival_watch", Path(__file__).resolve().parents[2] / "scripts" / "at_survival_watch.py"
)
watch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watch)


class AtSurvivalWatchTests(unittest.TestCase):
    def test_schedule_is_sorted_and_deduplicated(self):
        self.assertEqual(watch.parse_schedule("15, 1,5,5"), [1.0, 5.0, 15.0])
        with self.assertRaises(ValueError):
            watch.parse_schedule("")
        with self.assertRaises(ValueError):
            watch.parse_schedule("-1")

    def test_t0_specs(self):
        self.assertEqual(watch.resolve_t0("created", 100, 200), 100.0)
        self.assertEqual(watch.resolve_t0("now", 100, 200), 200.0)
        self.assertEqual(watch.resolve_t0("12345", 100, 200), 12345.0)

    def test_row_carries_no_secrets_and_flags_revocation(self):
        row = watch.probe_row(
            "A", 7, 5.0, 0, {"status_code": 401, "status": "token_invalid", "error": "Encountered invalidated oauth token"}
        )
        self.assertEqual((row["label"], row["account_id"], row["status_code"]), ("A", 7, 401))
        self.assertTrue(row["revoked"])
        self.assertFalse(row["alive"])
        self.assertNotIn("access_token", row)
        alive = watch.probe_row("A", 7, 1.0, 0, {"status_code": 200, "status": "active"})
        self.assertTrue(alive["alive"])
        self.assertEqual(alive["error"], "")

    def test_summary_ignores_transport_unknowns(self):
        rows = [
            {"label": "A", "offset_min": 5, "status_code": 200},
            {"label": "A", "offset_min": 5, "status_code": 401},
            {"label": "A", "offset_min": 5, "status_code": 0},
            {"label": "B", "offset_min": 1, "status_code": 200},
        ]
        self.assertEqual(watch.summarize(rows), {"A": {5.0: "1/2"}, "B": {1.0: "1/1"}})


if __name__ == "__main__":
    unittest.main()