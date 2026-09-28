"""Unit tests for services/protocol-payment/common/proxy_state.py.

Batch 3 of the protocol-payment extractor consolidation.  The shared store is
exercised with injected fakes (env prefix, base dir, key normalisers, log sink)
so the tests do not depend on any extractor module or on the real
``proxy_state.json`` files.

The differential proof that this module reproduces the extractors' HEAD bodies
byte-for-byte lives in ``runtime/tmp/_proxy_state_diff_verify.py``; these tests
pin the contract and the failure modes so a future edit cannot silently change
either.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_COMMON = Path(__file__).resolve().parents[1] / "services" / "protocol-payment" / "common"
_SPEC = importlib.util.spec_from_file_location("proxy_state_under_test", _COMMON / "proxy_state.py")
assert _SPEC is not None and _SPEC.loader is not None
PS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = PS
_SPEC.loader.exec_module(PS)


def _store(tmp: Path, *, prefix: str = "TEST", log=None, lock=None, clock=None, env_bool=None, env_int=None):
    return PS.ProxyStateStore(
        env_prefix=prefix,
        base_dir=tmp,
        proxy_key=lambda proxy: f"plain:{proxy}" if proxy else "",
        proxy_chain_key=lambda proxy: f"chain:{proxy}" if proxy else "",
        env_bool=env_bool or (lambda name, default: default),
        env_int=env_int or (lambda name, default, minimum=1: default),
        normalize_country=lambda country: str(country or "").strip().upper(),
        lock=lock,
        clock=clock,
        log=log,
    )


class ProxyStateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env_names = []

    def tearDown(self):
        import os

        for name in self._env_names:
            os.environ.pop(name, None)
        self._tmp.cleanup()

    def _set_env(self, name: str, value: str) -> None:
        import os

        self._env_names.append(name)
        os.environ[name] = value

    # -- path ---------------------------------------------------------------

    def test_path_prefers_the_prefixed_env_var(self):
        target = self.tmp / "override.json"
        self._set_env("TEST_PROXY_STATE_FILE", str(target))
        self.assertEqual(_store(self.tmp).path(), target)

    def test_path_defaults_to_base_dir(self):
        self.assertEqual(_store(self.tmp).path(), self.tmp / "proxy_state.json")

    # -- load ---------------------------------------------------------------

    def test_load_missing_file_returns_the_five_group_shape(self):
        state = _store(self.tmp).load()
        self.assertEqual(set(state), {"seed", "checkout", "promotion", "provider", "pair"})

    def test_load_fills_missing_groups_in_a_partial_file(self):
        (self.tmp / "proxy_state.json").write_text(json.dumps({"seed": {"a": {"success": 1}}}), encoding="utf-8")
        state = _store(self.tmp).load()
        self.assertEqual(state["seed"], {"a": {"success": 1}})
        self.assertEqual(state["checkout"], {})

    def test_load_tolerates_corrupt_and_non_dict_payloads(self):
        for raw in (b"{not json", b"[1,2,3]"):
            (self.tmp / "proxy_state.json").write_bytes(raw)
            # A fresh store per case: the first load caches its result.
            state = _store(self.tmp).load()
            self.assertEqual(set(state), {"seed", "checkout", "promotion", "provider", "pair"})

    def test_load_is_cached_after_the_first_call(self):
        store = _store(self.tmp)
        first = store.load()
        first["seed"]["mutated"] = {"success": 9}
        self.assertIs(store.load(), first)

    # -- save ---------------------------------------------------------------

    def test_save_before_load_is_a_noop(self):
        store = _store(self.tmp)
        store.save()
        self.assertFalse((self.tmp / "proxy_state.json").exists())

    def test_save_writes_sorted_indent_json(self):
        store = _store(self.tmp)
        state = store.load()
        state["seed"]["b"] = {"success": 2}
        state["seed"]["a"] = {"fail": 1}
        store.save()
        raw = (self.tmp / "proxy_state.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), state)
        self.assertLess(raw.index('"a"'), raw.index('"b"'))

    # -- key ----------------------------------------------------------------

    def test_key_uses_the_chain_normaliser_for_seed_only(self):
        store = _store(self.tmp)
        self.assertEqual(store.key("seed", "p"), "chain:p")
        self.assertEqual(store.key("checkout", "p"), "plain:p")
        self.assertEqual(store.key("promotion", "p"), "plain:p")
        self.assertEqual(store.key("provider", "p"), "plain:p")

    # -- prune --------------------------------------------------------------

    def test_prune_seed_removes_stale_keys_and_logs(self):
        messages: list[str] = []
        store = _store(self.tmp, log=messages.append)
        state = store.load()
        state["seed"] = {"chain:keep": {"success": 1}, "chain:drop": {"fail": 1}}
        store.save()
        store.prune_seed(["keep"])
        self.assertEqual(list(store.load()["seed"]), ["chain:keep"])
        self.assertEqual(messages, ["Seed 代理状态清理完成: 1"])

    def test_prune_removes_stale_groups_and_pairs_and_logs(self):
        messages: list[str] = []
        store = _store(self.tmp, log=messages.append)
        state = store.load()
        state["checkout"] = {"plain:c1": {}, "plain:c2": {}}
        state["promotion"] = {"plain:p1": {}}
        state["provider"] = {"plain:pr1": {}, "plain:pr2": {}}
        state["pair"] = {
            "c1:pr1": {"checkout": "plain:c1", "provider": "plain:pr1"},
            "c2:pr2": {"checkout": "plain:c2", "provider": "plain:pr2"},
            "gone": {"checkout": "plain:x", "provider": "plain:y"},
        }
        store.save()
        store.prune(["c1"], ["p1"], ["pr1"])
        state = store.load()
        self.assertEqual(list(state["checkout"]), ["plain:c1"])
        self.assertEqual(list(state["pair"]), ["c1:pr1"])
        # checkout=1, provider=1, pair=2 (c2:pr2 lost its checkout; gone was stale)
        self.assertIn("代理状态清理完成:", messages[-1])

    def test_prune_is_a_noop_and_silent_when_nothing_is_stale(self):
        messages: list[str] = []
        store = _store(self.tmp, log=messages.append)
        state = store.load()
        state["checkout"] = {"plain:c1": {}}
        store.save()
        store.prune(["c1"], [], [])
        self.assertEqual(messages, [])

    # -- lock ---------------------------------------------------------------

    def test_injected_lock_is_shared_not_replaced(self):
        lock = threading.RLock()
        store = _store(self.tmp, lock=lock)
        self.assertIs(store._lock, lock)

    def test_default_lock_is_reentrant(self):
        store = _store(self.tmp)
        # prune acquires the lock and then calls load/save, which re-acquire it.
        store.load()
        store.prune([], [], [])  # must not deadlock

    # -- free-function surface ---------------------------------------------

    def test_free_functions_delegate_to_the_passed_store(self):
        store = _store(self.tmp)
        self.assertEqual(PS.proxy_state_path(store), store.path())
        self.assertEqual(PS.load_proxy_state(store), store.load())
        self.assertEqual(PS.proxy_state_key(store, "seed", "p"), "chain:p")
        PS.save_proxy_state(store)
        PS.prune_proxy_seed_state(store, [])
        PS.prune_proxy_state(store, [], [], [])
        self.assertTrue((self.tmp / "proxy_state.json").exists())


class ProxyStateRecordTests(unittest.TestCase):
    """Batch 3b: record / zero-cache / pair primitives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.removed: list[tuple] = []
        self.store = _store(self.tmp, clock=lambda: 1_700_000_000.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_result_counts_and_stamps(self):
        record = self.store.record_result("checkout", "c1", True)
        self.assertEqual((record["success"], record["fail"]), (1, 0))
        self.assertEqual(record["last_success"], 1_700_000_000)
        record = self.store.record_result("checkout", "c1", False, "boom")
        self.assertEqual((record["success"], record["fail"]), (1, 1))
        self.assertEqual(record["last_reason"], "boom")

    def test_record_result_blank_proxy_is_a_noop(self):
        self.assertEqual(self.store.record_result("checkout", "", True), {})

    def test_is_reused_and_remove_after_fails(self):
        self.assertTrue(self.store.is_reused({"success": 1}))
        self.assertFalse(self.store.is_reused({"success": 0}))
        self.assertEqual(self.store.remove_after_fails(), 3)

    def test_record_health_failure_removes_only_at_threshold(self):
        self.store.record_result("seed", "s1", True)  # reused -> threshold 3
        self.store.record_health_failure("seed", "s1", "timeout", lambda *a: self.removed.append(a))
        self.store.record_health_failure("seed", "s1", "timeout", lambda *a: self.removed.append(a))
        self.assertEqual(self.removed, [])
        self.store.record_health_failure("seed", "s1", "timeout", lambda *a: self.removed.append(a))
        self.assertEqual(self.removed, [("seed", "s1", "timeout")])

    def test_zero_cache_status_reads_ok_bad_stale_and_mismatch(self):
        rec = self.store.record("seed", "s1")
        rec.update({"zero_checked_at": 1_700_000_000, "zero_amount": 0, "zero_country": "NL"})
        rec["zero_ok"] = True
        self.assertEqual(self.store.zero_cache_status("s1", "NL"), ("ok", 0, 1_700_000_000))
        rec["zero_ok"] = False
        rec["zero_amount"] = 1250
        self.assertEqual(self.store.zero_cache_status("s1", "NL"), ("bad", 1250, 1_700_000_000))
        rec["zero_ok"] = True
        self.assertEqual(self.store.zero_cache_status("s1", "US"), ("", 0, 1_700_000_000))
        rec["zero_country"] = "NL"
        rec["zero_checked_at"] = 1_699_000_000
        self.assertEqual(self.store.zero_cache_status("s1", "NL"), ("", 0, 1_699_000_000))

    def test_record_zero_result_sets_cache_fields(self):
        self.store.record_zero_result("s1", "NL", 0)
        rec = self.store.record("seed", "s1")
        self.assertIs(rec["zero_ok"], True)
        self.assertEqual(rec["zero_success"], 1)
        self.assertEqual(rec["zero_checked_at"], 1_700_000_000)

    def test_pair_result_and_approve_preferences(self):
        self.store.record_pair_result("c1", "p1", True, "ok")
        key = self.store.pair_key("c1", "p1")
        pair = self.store.load()["pair"][key]
        self.assertEqual(pair["success"], 1)
        self.assertEqual(self.store.successful_approve_preferences("c1", "p1", ["p2", "a1"]), [])
        self.store.record_pair_approve_success("c1", "p1", "a1")
        self.assertEqual(self.store.successful_approve_preferences("c1", "p1", ["p2", "a1"]), ["a1"])

    def test_pair_key_blank_on_missing_component(self):
        self.assertEqual(self.store.pair_key("c1", ""), "")

    def test_order_group_ranks_success_first_and_skips_recent_failures(self):
        self.store.record_result("checkout", "p_fail", False, "boom")
        self.store.record_result("checkout", "p_ok", True)
        self.assertEqual(self.store.order_group("checkout", ["p_fail", "p_ok", "p_new"]), ["p_ok", "p_new"])

    def test_order_group_returns_input_when_scoring_disabled(self):
        store = _store(
            self.tmp,
            clock=lambda: 1_700_000_000.0,
            env_bool=lambda name, default: False if name.endswith("_PROXY_SCORE") else default,
        )
        self.assertEqual(store.order_group("checkout", ["a", "b"]), ["a", "b"])

    def test_order_group_skips_checkout_with_failed_zero_cache(self):
        store = _store(
            self.tmp,
            clock=lambda: 1_700_000_000.0,
            env_bool=lambda name, default: True if name.endswith("_ZERO_CACHE_SCHEDULING") else default,
        )
        rec = store.record("checkout", "p_bad")
        rec.update({"zero_ok": False, "zero_amount": 1250, "zero_country": "NL", "zero_checked_at": 1_700_000_000})
        self.assertEqual(store.order_group("checkout", ["p_bad", "p_ok"]), ["p_ok"])

    def test_pair_preferences_orders_by_success(self):
        self.store.record_pair_result("c1", "p1", True, "ok")
        self.store.record_pair_result("c1", "p2", True, "ok")
        self.store.record_pair_result("c1", "p2", True, "ok")
        self.assertEqual(self.store.pair_preferences(["c1"], ["p1", "p2"]), {"c1": ["p2", "p1"]})

    def test_pair_preferences_ignores_unknown_and_unsuccessful(self):
        self.store.record_pair_result("c1", "p1", False, "boom")
        self.assertEqual(self.store.pair_preferences(["c1"], ["p1", "p2"]), {})
        self.assertEqual(self.store.pair_preferences(["c9"], ["p1"]), {})

    def test_record_failure_by_stage_dispatches_to_the_seed_record(self):
        store = _store(self.tmp, clock=lambda: 1_700_000_000.0)
        store.record_failure_by_stage(
            "checkout 阶段失败: boom",
            "c1",
            "p1",
            "pr1",
            remove_failed=lambda *a: None,
            is_direct_remove=lambda r: "407" in r,
            is_health_failure=lambda r: "timeout" in r,
            is_unavailable=lambda r: "unavailable" in r,
        )
        self.assertEqual(store.record("seed", "c1")["fail"], 1)

    def test_record_failure_by_stage_skips_unavailable_and_coupon_reasons(self):
        store = _store(self.tmp, clock=lambda: 1_700_000_000.0)
        kwargs = {
            "remove_failed": lambda *a: None,
            "is_direct_remove": lambda r: False,
            "is_health_failure": lambda r: False,
            "is_unavailable": lambda r: "unavailable" in r,
        }
        store.record_failure_by_stage("unavailable: x", "c1", "p1", **kwargs)
        store.record_failure_by_stage("0 元优惠未生效: x", "c1", "p1", **kwargs)
        store.record_failure_by_stage("approve blocked: x", "c1", "p1", **kwargs)
        self.assertEqual(store.load()["seed"], {})

    def test_record_failure_by_stage_health_path_can_remove_seed(self):
        store = _store(self.tmp, clock=lambda: 1_700_000_000.0)
        removed: list[tuple] = []
        store.record_failure_by_stage(
            "timeout",
            "c1",
            "p1",
            remove_failed=lambda group, proxy, reason: removed.append((group, proxy)),
            is_direct_remove=lambda r: False,
            is_health_failure=lambda r: "timeout" in r,
            is_unavailable=lambda r: False,
        )
        # Not reused (no prior success) => threshold 1 => removed on first fail.
        self.assertEqual(removed, [("seed", "p1")])


if __name__ == "__main__":
    unittest.main()
