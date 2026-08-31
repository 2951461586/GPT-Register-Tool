"""Regression tests for the PayPal proxy state cache + cross-process write lock.

``_PAYPAL_PROXY_STATE_CACHE`` is a module-level dict mutated from proxy-selection
worker threads; without a lock two threads can both insert and the cache can
leak duplicate instances. ``PayPalProxyState._save`` must also serialise the
state-file replace across backend processes. These tests lock both behaviours.
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool import paypal_proxy as pp  # noqa: E402


class PayPalProxyStateCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved_cache = dict(pp._PAYPAL_PROXY_STATE_CACHE)
        pp._PAYPAL_PROXY_STATE_CACHE.clear()
        self.state_file = Path(self._tmp) / "paypal_proxy_state.json"

    def tearDown(self):
        import shutil

        pp._PAYPAL_PROXY_STATE_CACHE.clear()
        pp._PAYPAL_PROXY_STATE_CACHE.update(self._saved_cache)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _cfg(self):
        return {"proxy_health": {"state_file": str(self.state_file), "enabled": True}}

    def test_same_config_returns_cached_instance(self):
        first = pp._paypal_proxy_state(self._cfg())
        second = pp._paypal_proxy_state(self._cfg())
        self.assertIs(first, second)
        self.assertEqual(len(pp._PAYPAL_PROXY_STATE_CACHE), 1)

    def test_concurrent_threads_share_one_cache_instance(self):
        # Without _CACHE_LOCK a ThreadPoolExecutor fan-out could create several
        # PayPalProxyState objects for the same key. With it, every thread must
        # observe the single instance.
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(pp._paypal_proxy_state(self._cfg()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 8)
        self.assertEqual(len({id(state) for state in results}), 1)

    def test_state_save_creates_cross_process_lock_file(self):
        state = pp._paypal_proxy_state(self._cfg())
        state.record_result("checkout", "http://user:pass@gw.example.com:9000", True)
        self.assertTrue(self.state_file.exists())
        self.assertTrue(self.state_file.with_name(self.state_file.name + ".lock").exists())

    def test_state_round_trips_result(self):
        state = pp._paypal_proxy_state(self._cfg())
        state.record_result("checkout", "http://user:pass@gw.example.com:9000", False, reason="proxy auth")
        reloaded = pp._paypal_proxy_state(self._cfg())
        record = reloaded._load()["stages"]["checkout"].get(
            pp.proxy_key("http://user:pass@gw.example.com:9000")
        )
        self.assertIsNotNone(record)
        self.assertEqual(int(record["fail"]), 1)


if __name__ == "__main__":
    unittest.main()
