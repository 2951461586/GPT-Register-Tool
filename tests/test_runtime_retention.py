"""``scripts/runtime_retention.py`` — planner correctness + safety rails.

The dangerous failure mode for a retention tool is deleting live state, so the
tests here are weighted toward the *veto* logic rather than the happy path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.runtime_retention import (
    NEVER_DELETE,
    RELOCATE_DIRNAME,
    RULES,
    Rule,
    is_protected,
    main,
    plan_retention,
    purge_relocated,
    relocate,
)

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400.0


def _touch(path: Path, age_days: float, size: int = 4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


class IsProtected(unittest.TestCase):
    """The veto list is the whole safety story -- test it exhaustively."""

    def test_every_never_delete_entry_is_vetoed(self):
        self.assertGreater(len(NEVER_DELETE), 5, "veto list looks empty")
        for name in NEVER_DELETE:
            with self.subTest(name=name):
                self.assertTrue(
                    is_protected(name),
                    f"{name} is in NEVER_DELETE but is_protected() says no",
                )

    def test_database_files_are_vetoed_anywhere(self):
        for rel in ("accounts.sqlite3", "nested/dir/state.db", "a/b/c.sqlite3"):
            with self.subTest(rel=rel):
                self.assertTrue(is_protected(rel))

    def test_top_level_lock_is_vetoed(self):
        """``paypal_proxy_state.json.lock`` guards live state."""
        self.assertTrue(is_protected("paypal_proxy_state.json.lock"))

    def test_batch_gate_lock_is_NOT_vetoed(self):
        """Regression lock for a real bug.

        An early revision vetoed every ``.lock``, which protected all 3,949
        ``payment_batch_locks/gates/<batch>/slot-0.lock`` files -- the single
        largest target this tool exists to retire. The dry run reported 33
        candidates instead of ~3,100.
        """
        rel = "payment_batch_locks/gates/payment-batch-gcash_20260819_112844_2f88ff/slot-0.lock"
        self.assertFalse(
            is_protected(rel),
            "batch gate locks must NOT be vetoed -- they are the main target",
        )

    def test_ordinary_output_is_not_vetoed(self):
        for rel in (
            "payment_batches/gcash_20260802_191050.json",
            "app_20260908.log",
            "mailbox_imports/report.json",
        ):
            with self.subTest(rel=rel):
                self.assertFalse(is_protected(rel))


class RuleMatching(unittest.TestCase):
    def test_dir_rule_matches_whole_subtree(self):
        rule = Rule("t", 1, "", match_dir="payment_batch_locks")
        self.assertTrue(
            rule.matches("payment_batch_locks/gates/b/slot-0.lock"),
        )
        self.assertFalse(rule.matches("payment_batches/x.json"))

    def test_non_recursive_name_rule_only_matches_top_level(self):
        rule = Rule("t", 1, "", match_name="*.log", recursive=False)
        self.assertTrue(rule.matches("app_20260908.log"))
        self.assertFalse(rule.matches("logs/app_20260908.log"))

    def test_recursive_name_rule_matches_nested(self):
        rule = Rule("t", 1, "", match_name="*scan*.json")
        self.assertTrue(rule.matches("nested/deep/promotion_scan.json"))

    def test_operation_gate_rule_spares_the_idempotency_record(self):
        """``payment_operations/<hash>.json`` is idempotency state, not litter.

        It is the record that tells a retry whether replaying is allowed;
        retiring it on age alone would quietly make retries unsafe.  The rule
        has to be anchored on ``*.lock`` under ``gates/`` for exactly this
        reason -- dropping ``match_name`` must turn this test red.
        """
        rule = next(r for r in RULES if r.name == "empty payment-operation gate locks")
        self.assertTrue(
            rule.matches("payment_operations/gates/payment-operation-abc/slot-0.lock"),
            "sentinel: the rule must still reach the empty gate locks",
        )
        self.assertFalse(
            rule.matches("payment_operations/deadbeef.json"),
            "the idempotency record must never be retired",
        )
        self.assertFalse(rule.matches("payment_operations/gates/h/state.json"))

    def test_every_rule_matches_something(self):
        """A rule that can never fire is dead weight and hides intent."""
        samples = {
            "empty batch gate locks": "payment_batch_locks/gates/b/slot-0.lock",
            "empty payment-operation gate locks": (
                "payment_operations/gates/payment-operation-abc123/slot-0.lock"
            ),
            "browser profiles": "browser_profiles/camoufox/p/default/x.json",
            "per-batch payment artifacts": "payment_batches/gcash_1.json",
            "one-off scan snapshots": "promotion_scan.json",
            "operator logs and scratch files": "app_20260908.log",
            "mailbox imports": "mailbox_imports/report.json",
        }
        for rule in RULES:
            with self.subTest(rule=rule.name):
                sample = samples.get(rule.name)
                self.assertIsNotNone(sample, f"no sample for rule {rule.name!r}")
                self.assertTrue(
                    rule.matches(sample),
                    f"rule {rule.name!r} does not match its own sample {sample!r}",
                )


class PlanRetention(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_old_files_are_planned_and_fresh_ones_are_not(self):
        _touch(self.root / "payment_batches" / "old.json", age_days=40)
        _touch(self.root / "payment_batches" / "fresh.json", age_days=2)
        items = {i.relpath: i for i in plan_retention(self.root)}
        self.assertIn("payment_batches/old.json", {i.replace("\\", "/") for i in items})
        self.assertNotIn(
            "payment_batches/fresh.json", {i.replace("\\", "/") for i in items}
        )

    def test_veto_outranks_a_rule_that_would_otherwise_fire(self):
        """The veto must win even where a rule covers the location.

        A first version of this test put the files at the **runtime root**,
        where no rule matches them -- so it passed whether or not the veto
        worked at all (proved by mutation: removing an entry from
        ``NEVER_DELETE`` left all 23 tests green). The files must live under a
        directory a rule actually covers, and a sentinel must confirm the rule
        really fires there -- otherwise the assertion is vacuous.
        """
        for name in ("accounts.sqlite3", "payment_link_runs.jsonl"):
            _touch(self.root / "payment_batches" / name, age_days=3650)
        _touch(self.root / "payment_batches" / "ordinary.json", age_days=3650)

        planned = {Path(i.relpath).name for i in plan_retention(self.root, min_age_days=0)}

        self.assertIn(
            "ordinary.json",
            planned,
            "sentinel: the rule must fire here, or the veto check below is vacuous",
        )
        for name in ("accounts.sqlite3", "payment_link_runs.jsonl"):
            with self.subTest(name=name):
                self.assertNotIn(
                    name, planned, f"veto failed to protect {name} from a matching rule"
                )

    def test_min_age_override_applies_to_every_rule(self):
        _touch(self.root / "payment_batches" / "mid.json", age_days=10)
        default = plan_retention(self.root)
        self.assertEqual([], default, "10d old batch file should survive the 30d rule")
        relaxed = plan_retention(self.root, min_age_days=5)
        self.assertEqual(1, len(relaxed), "min_age_days=5 should catch it")
        stricter = plan_retention(self.root, min_age_days=60)
        self.assertEqual([], stricter, "min_age_days=60 should spare it")

    def test_planner_never_returns_a_protected_path(self):
        """Invariant, not a sample: no rule may outrank the veto."""
        _touch(self.root / "accounts.sqlite3", age_days=999)
        _touch(self.root / "payment_batch_locks" / "gates" / "b" / "slot-0.lock", age_days=99)
        _touch(self.root / "payment_batches" / "old.json", age_days=99)
        for item in plan_retention(self.root, min_age_days=0):
            with self.subTest(rel=item.relpath):
                self.assertFalse(
                    is_protected(item.relpath),
                    f"planner proposed a protected path: {item.relpath}",
                )

    def test_missing_root_yields_empty_plan(self):
        self.assertEqual([], plan_retention(self.root / "nope"))

    def test_relocation_area_is_skipped(self):
        _touch(self.root / RELOCATE_DIRNAME / "stamp" / "x.json", age_days=999)
        self.assertEqual([], plan_retention(self.root, min_age_days=0))

    def test_plan_does_not_touch_the_filesystem(self):
        target = _touch(self.root / "payment_batches" / "old.json", age_days=99)
        before = target.stat().st_mtime
        plan_retention(self.root)
        self.assertTrue(target.exists())
        self.assertEqual(before, target.stat().st_mtime)


class ApplyAndPurge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relocate_preserves_layout_and_is_reversible(self):
        src = _touch(self.root / "payment_batches" / "old.json", age_days=99)
        items = plan_retention(self.root)
        self.assertEqual(1, len(items))
        target = relocate(self.root, items, "20260909-000000")
        self.assertFalse(src.exists(), "source should have moved")
        moved = target / "payment_batches" / "old.json"
        self.assertTrue(moved.exists(), "layout must be preserved under the stamp")
        # Reversible by construction: the bytes are still on disk. The parent
        # directory was pruned (see the next test), so recreate it first.
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(moved), str(src))
        self.assertTrue(src.exists())

    def test_relocate_prunes_directories_that_became_empty(self):
        """Otherwise retiring 3,949 gate locks would leave 3,949 empty dirs."""
        _touch(self.root / "payment_batch_locks" / "gates" / "b" / "slot-0.lock", age_days=99)
        gate_dir = self.root / "payment_batch_locks" / "gates" / "b"
        relocate(self.root, plan_retention(self.root), "20260909-000000")
        self.assertFalse(
            gate_dir.exists(), "emptied batch gate directory should be pruned"
        )

    def test_purge_removes_relocated_trees(self):
        _touch(self.root / "payment_batches" / "old.json", age_days=99)
        relocate(self.root, plan_retention(self.root), "20260909-000000")
        self.assertEqual(1, purge_relocated(self.root))
        self.assertFalse((self.root / RELOCATE_DIRNAME / "20260909-000000").exists())


class SafetyRails(unittest.TestCase):
    """The tool must stay an operator tool and stay opt-in."""

    def test_no_production_module_imports_it(self):
        hits: list[str] = []
        for base in ("sms_tool", "services"):
            for path in (ROOT / base).rglob("*.py"):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "runtime_retention" in text:
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            [],
            hits,
            "retention must never run on a registration/payment hot path",
        )

    def test_cli_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch(root / "payment_batches" / "old.json", age_days=99)
            code = main(["--root", str(root)])
            self.assertEqual(0, code)
            self.assertFalse(
                (root / RELOCATE_DIRNAME).exists(),
                "default invocation must not move anything",
            )
            self.assertTrue((root / "payment_batches" / "old.json").exists())

    def test_cli_reports_nothing_to_do_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(0, main(["--root", tmp]))


class CliSmoke(unittest.TestCase):
    def test_module_runs_as_a_script(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "runtime_retention.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("--apply", proc.stdout)


if __name__ == "__main__":
    unittest.main()
