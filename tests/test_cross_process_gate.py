"""Offline tests for the cross-process file-lock semaphore."""

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


if __name__ == "__main__":
    unittest.main()
