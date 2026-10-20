"""``scripts/payment_legacy_manifest.py`` -- the legacy-payment file-set ratchet.

The gate exists because ``docs/architecture.md`` already says new payment methods
belong in ``sms_tool/pay_link/``, and nothing enforced it: a method could be added
to ``paypal_link/`` or ``upi_link/`` without touching the registry, the catalog or
the desktop method picker. What is frozen here is the *file set*, not the file
contents -- the legacy lanes still take bug fixes.

These tests pin the predicate in both directions, because a gate that can only
say "FAIL" is not a gate: additions must fail, removals must ask for a baseline
refresh, and an unchanged set must pass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import payment_legacy_manifest as plm


class DirectionOfTravel(unittest.TestCase):
    def test_a_new_file_fails(self):
        current = {"sms_tool/upi_link": ["a.py", "b.py"]}
        allowed = {"sms_tool/upi_link": ["a.py"]}
        self.assertEqual(plm.additions(current, allowed), [("sms_tool/upi_link", "b.py")])

    def test_an_unchanged_set_has_no_additions_or_removals(self):
        current = {"sms_tool/paypal_link": ["gen_link.py"]}
        allowed = {"sms_tool/paypal_link": ["gen_link.py"]}
        self.assertEqual(plm.additions(current, allowed), [])
        self.assertEqual(plm.removals(current, allowed), [])

    def test_a_removal_is_reported_so_the_baseline_can_be_refreshed(self):
        current = {"sms_tool/upi_link": ["a.py"]}
        allowed = {"sms_tool/upi_link": ["a.py", "b.py"]}
        self.assertEqual(plm.removals(current, allowed), [("sms_tool/upi_link", "b.py")])

    def test_a_rename_is_an_addition_and_a_removal_not_a_pass(self):
        current = {"sms_tool/upi_link": ["renamed.py"]}
        allowed = {"sms_tool/upi_link": ["original.py"]}
        self.assertEqual(plm.additions(current, allowed), [("sms_tool/upi_link", "renamed.py")])
        self.assertEqual(plm.removals(current, allowed), [("sms_tool/upi_link", "original.py")])

    def test_an_empty_baseline_makes_every_file_new(self):
        current = {"sms_tool/upi_link": ["a.py"]}
        self.assertEqual(plm.additions(current, {}), [("sms_tool/upi_link", "a.py")])


class CorruptBaseline(unittest.TestCase):
    def test_a_corrupt_baseline_loads_as_empty_so_every_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(plm.load_baseline(path), {})

    def test_a_missing_baseline_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(plm.load_baseline(Path(tmp) / "nope.json"), {})

    def test_non_list_entries_are_dropped_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps({"d": "not-a-list", "e": ["a.py"]}), encoding="utf-8")
            self.assertEqual(plm.load_baseline(path), {"e": ["a.py"]})


class RealTree(unittest.TestCase):
    def test_the_committed_baseline_matches_the_tree(self):
        """The manifest must describe today's tree, not a memory of it."""
        self.assertEqual(plm.main([]), 0)

    def test_pay_link_is_not_frozen(self):
        """The registry is the open directory; freezing it would block new methods."""
        self.assertNotIn("sms_tool/pay_link", plm.LEGACY_DIRS)

    def test_both_legacy_directories_are_scanned(self):
        current = plm.manifest()
        self.assertEqual(sorted(current), sorted(plm.LEGACY_DIRS))
        self.assertTrue(all(current.values()))
