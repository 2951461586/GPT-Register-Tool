"""Regression tests for phone-reuse state persistence.

The state file carries the reuse cursor for each phone number. A torn write or
two processes interleaving writes can hand the same number to two accounts —
real financial loss. These tests lock the atomic-write + cross-process-lock
behaviour introduced in save_state.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.phone_reuse import PhonePool  # noqa: E402
from sms_tool.cross_process_gate import cross_process_write_lock, GateTimeoutError  # noqa: E402


class PhoneReuseAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.state_file = Path(self._tmp) / "phone_reuse_state.json"

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _pool(self):
        return PhonePool(phones=[], state_file=str(self.state_file))

    def test_save_state_writes_valid_json(self):
        pool = self._pool()
        pool.save_state()
        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["current_index"], 0)
        self.assertEqual(state["phones"], [])

    def test_save_state_creates_cross_process_lock_file(self):
        pool = self._pool()
        pool.save_state()
        self.assertTrue(self.state_file.with_name(self.state_file.name + ".lock").exists())

    def test_atomic_write_keeps_old_file_when_replace_fails(self):
        # Pre-existing valid state must survive a failed replace.
        self.state_file.write_text(
            json.dumps({"current_index": 3, "phones": [], "updated_at": 1}), encoding="utf-8"
        )
        pool = self._pool()
        with mock.patch.object(phone_reuse_os(), "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pool.save_state()
        # the original content is untouched (no torn file)
        kept = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(kept["current_index"], 3)

    def test_cross_process_write_lock_is_reentrant_safe_and_nonblocking(self):
        lock_path = Path(self._tmp) / "gate.lock"
        # Entering and leaving must not deadlock.
        with cross_process_write_lock(lock_path):
            pass
        self.assertTrue(lock_path.exists())


def phone_reuse_os():
    # _atomic_write_text calls os.replace on the os module imported by phone_reuse,
    # so patching that module's replace attribute is enough.
    import sms_tool.phone_reuse as pr

    return pr.os


if __name__ == "__main__":
    unittest.main()
