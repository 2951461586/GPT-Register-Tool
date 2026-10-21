"""Regression tests for the PayPal proxy state cache + cross-process write lock.

``_PAYPAL_PROXY_STATE_CACHE`` is a module-level dict mutated from proxy-selection
worker threads; without a lock two threads can both insert and the cache can
leak duplicate instances. ``PayPalProxyState._save`` must also serialise the
state-file replace across backend processes. These tests lock both behaviours.
"""

import ast
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.paypal import proxy as pp  # noqa: E402


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
        record = reloaded._load()["stages"]["checkout"].get(pp.proxy_key("http://user:pass@gw.example.com:9000"))
        self.assertIsNotNone(record)
        self.assertEqual(int(record["fail"]), 1)


class PayPalProjectRootDepthTests(unittest.TestCase):
    """``PROJECT_ROOT`` must stay at the repo root after the ``paypal/`` move.

    ``sms_tool/paypal_proxy.py`` and ``sms_tool/paypal_extract.py`` used
    ``Path(__file__).resolve().parent.parent``, which was the repo root while
    they sat directly under ``sms_tool/``. The move into ``sms_tool/paypal/``
    added a level without adding a ``.parent``, so the default
    ``runtime/paypal_proxy_state.json`` started resolving to
    ``sms_tool/runtime/paypal_proxy_state.json``: a shadow state file inside the
    source tree, written by live payment runs and -- unlike ``/runtime/`` -- not
    covered by ``.gitignore``. Nothing failed, because the reader and the writer
    were wrong in the same direction. ``sms_tool/paypal_link/gen_link.py``
    already carries the corrected depth plus a comment about this exact hazard;
    these tests pin the same fact for the newer subpackage.
    """

    def test_proxy_project_root_is_the_repo_root(self):
        self.assertEqual(Path(pp.PROJECT_ROOT).resolve(), ROOT)

    def test_extract_project_root_is_the_repo_root(self):
        from sms_tool.paypal import extract as px

        self.assertEqual(Path(px.PROJECT_ROOT).resolve(), ROOT)

    def test_default_state_file_lands_in_the_runtime_dir_not_the_package(self):
        # No explicit ``state_file``: the default relative path is what live
        # runs use, so this is the case that produced the stray tree.
        state = pp._paypal_proxy_state({})
        self.assertEqual(state.path, ROOT / "runtime" / "paypal_proxy_state.json")
        self.assertNotEqual(state.path.parent.name, "sms_tool")

    def test_no_module_in_the_paypal_package_points_project_root_at_the_package(self):
        """A blanket guard: any moved module that kept the old depth goes red."""
        package_dir = ROOT / "sms_tool" / "paypal"
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if not isinstance(node, ast.Assign):
                    continue
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "PROJECT_ROOT" not in targets:
                    continue
                if not any(isinstance(sub, ast.Attribute) and sub.attr == "resolve" for sub in ast.walk(node.value)):
                    continue
                depth = sum(
                    1 for sub in ast.walk(node.value) if isinstance(sub, ast.Attribute) and sub.attr == "parent"
                )
                offenders.append(f"{path.name}: PROJECT_ROOT uses {depth} .parent(s)")
                if depth < 3:
                    self.fail(
                        f"{path.name}: PROJECT_ROOT resolves to the package dir, not the repo root "
                        f"({depth} .parent(s); sms_tool/paypal/<module>.py needs 3)"
                    )
        self.assertEqual(
            offenders, ["extract.py: PROJECT_ROOT uses 3 .parent(s)", "proxy.py: PROJECT_ROOT uses 3 .parent(s)"]
        )


if __name__ == "__main__":
    unittest.main()
