"""Offline tests for the cross-process file-lock semaphore."""

import ast
import tempfile
import unittest
from concurrent import futures as _futures
from pathlib import Path

from sms_tool.cross_process_gate import CrossProcessSemaphore, GateTimeoutError


class CrossProcessGateTests(unittest.TestCase):
    def test_two_instances_share_the_slot_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CrossProcessSemaphore("shared", 2, base_dir=tmp)
            second = CrossProcessSemaphore("shared", 2, base_dir=tmp)
            first.acquire(timeout=5)
            second.acquire(timeout=5)
            # Both slots are now held (by different instances == processes);
            # a third acquisition must time out instead of overselling.
            third = CrossProcessSemaphore("shared", 2, base_dir=tmp)
            with self.assertRaises(GateTimeoutError):
                third.acquire(timeout=0.3, poll_interval=0.05)
            second.release()
            third.acquire(timeout=5)  # freed slot is reusable by another instance
            third.release()
            first.release()

    def test_slots_are_exclusive_per_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = CrossProcessSemaphore("solo", 1, base_dir=tmp)
            gate.acquire(timeout=5)
            other = CrossProcessSemaphore("solo", 1, base_dir=tmp)
            with self.assertRaises(GateTimeoutError):
                other.acquire(timeout=0.3, poll_interval=0.05)
            gate.release()
            other.acquire(timeout=5)
            other.release()

    def test_context_manager_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = CrossProcessSemaphore("ctx", 1, base_dir=tmp)
            with gate:
                probe = CrossProcessSemaphore("ctx", 1, base_dir=tmp)
                with self.assertRaises(GateTimeoutError):
                    probe.acquire(timeout=0.2, poll_interval=0.05)
            probe.acquire(timeout=5)
            probe.release()

    def test_threads_within_one_instance_do_not_oversell(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = CrossProcessSemaphore("threads", 3, base_dir=tmp)
            acquired = []

            def worker(index: int) -> None:
                gate.acquire(timeout=10)
                try:
                    acquired.append(index)
                finally:
                    gate.release()

            with _futures.ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(worker, range(6)))
            self.assertEqual(len(acquired), 6)

    def test_gate_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            CrossProcessSemaphore("fresh", 4, base_dir=tmp)
            self.assertTrue((Path(tmp) / "gates" / "fresh").is_dir())


class CrossProcessGateDiscardTests(unittest.TestCase):
    """``discard()`` — the fix for unbounded gate directories.

    ``payment_batch`` and ``payment_operation`` build a gate whose name embeds a
    per-run id (``payment-batch-<batch_id>``, ``payment-operation-<key_hash>``).
    ``release()`` only unlocks and closes the handle, so every run used to leave
    one empty slot file plus one empty directory behind forever: 3,949 batch
    gates and 806 operation gates had accumulated under ``runtime/``.
    """

    def test_discard_removes_the_slot_file_and_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = CrossProcessSemaphore("ephemeral", 1, base_dir=tmp)
            gate.acquire(timeout=5)
            gate.release()
            slot = Path(tmp) / "gates" / "ephemeral" / "slot-0.lock"
            self.assertTrue(slot.exists(), "sentinel: the slot must exist before discard")
            gate.discard()
            self.assertFalse(slot.exists(), "discard left the slot file behind")
            self.assertFalse(
                (Path(tmp) / "gates" / "ephemeral").exists(),
                "discard left the gate directory behind",
            )

    def test_discard_is_idempotent_and_safe_without_acquiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = CrossProcessSemaphore("twice", 2, base_dir=tmp)
            gate.discard()  # never acquired
            gate.discard()
            gate.acquire(timeout=5)
            gate.release()
            gate.discard()
            gate.discard()

    def test_acquire_rebuilds_a_directory_that_discard_removed(self):
        """A waiter must not starve against a gate nobody holds any more.

        ``__init__`` is what normally creates the gate directory, so an instance
        built *before* the discard would otherwise poll forever.  Deleting the
        re-``mkdir`` from ``_try_acquire_any_slot`` must turn this red.
        """
        with tempfile.TemporaryDirectory() as tmp:
            owner = CrossProcessSemaphore("eph", 1, base_dir=tmp)
            waiter = CrossProcessSemaphore("eph", 1, base_dir=tmp)  # built before the discard
            owner.acquire(timeout=5)
            owner.release()
            owner.discard()
            self.assertFalse(
                (Path(tmp) / "gates" / "eph").exists(),
                "sentinel: discard must have removed the directory",
            )
            waiter.acquire(timeout=5)
            # The rebuilt slot must still exclude a third party: rebuilding is
            # not allowed to hand the gate to two holders at once.
            third = CrossProcessSemaphore("eph", 1, base_dir=tmp)
            with self.assertRaises(GateTimeoutError):
                third.acquire(timeout=0.3, poll_interval=0.05)
            waiter.release()
            third.acquire(timeout=5)
            third.release()


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SMS_TOOL = _PROJECT_ROOT / "sms_tool"

# Every ``<gate>.discard()`` call allowed inside ``sms_tool/``.  ``discard()``
# deletes the gate directory, which is only safe when the gate name is unique
# per operation and will never be acquired again after release.  On a *reused*
# gate (``registration_<stage>``) it lets a newcomer create a fresh slot file
# while an existing holder still owns the previous inode — two processes would
# then hold the same slot.  New call sites must be added here deliberately.
_ALLOWED_DISCARD_SITES = {
    ("sms_tool/payment_batch.py", "process_gate"),
    ("sms_tool/payment_operation.py", "gate"),
}

# ``set.discard()`` calls that merely share the method name.
_EXEMPT_DISCARD_SITES = {
    ("sms_tool/proxy_pool.py", "_active_tasks"),
}


def _discard_call_sites() -> set[tuple[str, str]]:
    """Every ``<receiver>.discard()`` call in ``sms_tool/`` as (relpath, receiver)."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(_SMS_TOOL.rglob("*.py")):
        rel = path.relative_to(_PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "discard":
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Attribute):
                name = receiver.attr
            elif isinstance(receiver, ast.Name):
                name = receiver.id
            else:
                name = "?"
            sites.add((rel, name))
    return sites


class CrossProcessGateCallSiteTests(unittest.TestCase):
    def test_discard_is_only_called_from_ephemeral_gates(self):
        sites = _discard_call_sites() - _EXEMPT_DISCARD_SITES
        self.assertTrue(sites, "sentinel: the AST scan found nothing — it is broken")
        for site in _ALLOWED_DISCARD_SITES:
            self.assertIn(site, sites, f"{site} no longer calls discard()")
        unexpected = sites - _ALLOWED_DISCARD_SITES
        self.assertEqual(set(), unexpected, "unreviewed discard() call site")


if __name__ == "__main__":
    unittest.main()
