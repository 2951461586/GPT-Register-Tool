import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "protocol-payment" / "kakao"))

import kakao_extract as kakao  # noqa: E402


_SEED = "http://user-region-KR-session-abc:pass@gw.example.com:9000"


class RecordSeedFailureTests(unittest.TestCase):
    """Lock the seed-level failure classification that survives after the
    role-level helpers were removed as dead code."""

    def setUp(self):
        self._tmp = Path(self.enterContext(_TempDir()))
        self.state_file = self._tmp / "proxy_state.json"
        self.seed_file = self._tmp / "proxy_seeds.txt"
        self.seed_file.write_text(_SEED + "\n", encoding="utf-8")
        self._env = _patch_env(
            KAKAO_PROXY_STATE_FILE=str(self.state_file),
            KAKAO_PROXY_SEED_FILE=str(self.seed_file),
            KAKAO_PROXY_REMOVE_AFTER_FAILS="3",
            KAKAO_PROXY_REMOVE_FAILED="1",
        )
        self._env.start()
        kakao._proxy_state = None  # reset the module-level cache between tests

    def tearDown(self):
        self._env.stop()
        kakao._proxy_state = None

    def test_account_error_is_kept_and_not_counted_as_proxy_fault(self):
        state = kakao.record_seed_failure(_SEED, "invalid access token")
        self.assertEqual(state, "kept")
        # account errors must not increment the seed's failure counter
        self.assertEqual(int(kakao.seed_record(_SEED).get("fail") or 0), 0)

    def test_checkout_shape_error_is_kept(self):
        reason = "checkout_not_kakao_trial: stage=Bootstrap amount=1000 currency=krw"
        self.assertEqual(kakao.record_seed_failure(_SEED, reason), "kept")
        self.assertEqual(int(kakao.seed_record(_SEED).get("fail") or 0), 0)

    def test_generic_failure_cools_down(self):
        self.assertEqual(kakao.record_seed_failure(_SEED, "weird unexpected error"), "cooling")
        self.assertEqual(int(kakao.seed_record(_SEED).get("fail") or 0), 1)

    def test_direct_proxy_error_removes_seed(self):
        state = kakao.record_seed_failure(_SEED, "proxy authentication required (HTTP 407)")
        self.assertEqual(state, "removed")
        # the seed line is physically removed from the seed file
        self.assertNotIn("gw.example.com", self.seed_file.read_text(encoding="utf-8"))

    def test_country_mismatch_removes_seed(self):
        state = kakao.record_seed_failure(_SEED, "出口国家 JP，要求 KR")
        self.assertEqual(state, "removed")

    def test_health_error_cools_then_removes_at_threshold(self):
        reason = "connection timed out"
        self.assertEqual(kakao.record_seed_failure(_SEED, reason), "cooling")
        self.assertEqual(kakao.record_seed_failure(_SEED, reason), "cooling")
        # third failure hits KAKAO_PROXY_REMOVE_AFTER_FAILS=3
        self.assertEqual(kakao.record_seed_failure(_SEED, reason), "removed")


class RemovedHelpersAreGoneTests(unittest.TestCase):
    """Guard against the dead role-level helpers being reintroduced."""

    def test_role_level_helpers_removed(self):
        for name in (
            "select_verified_proxy",
            "record_role_success",
            "record_role_failure",
            "remove_seed_when_all_roles_removed",
            "role_seed_usable",
            "role_seed_record",
        ):
            self.assertFalse(hasattr(kakao, name), f"{name} should have been removed")


# --- tiny stdlib-only helpers (avoid extra deps) -----------------------------

import os
import tempfile
from contextlib import contextmanager
from unittest import mock


class _TempDir:
    def __enter__(self):
        self._dir = tempfile.mkdtemp()
        return self._dir

    def __exit__(self, *exc):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)


def _patch_env(**values):
    return mock.patch.dict(os.environ, values, clear=False)


if __name__ == "__main__":
    unittest.main()
