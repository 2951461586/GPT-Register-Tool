"""Unit tests for services/protocol-payment/common/proxy_seed_file.py.

Batch 4 of the protocol-payment extractor consolidation.  The seed-file
mechanics are exercised with injected fakes (env prefix, chain-key normaliser,
redaction trio, clock) so nothing touches a real seed list.  The differential
proof that the module reproduces the extractors' HEAD bodies lives in
``runtime/tmp/_proxy_state_diff_verify_b4.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_COMMON = Path(__file__).resolve().parents[1] / "services" / "protocol-payment" / "common"
_SPEC = importlib.util.spec_from_file_location("proxy_seed_file_under_test", _COMMON / "proxy_seed_file.py")
assert _SPEC is not None and _SPEC.loader is not None
PSF = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = PSF
_SPEC.loader.exec_module(PSF)


def _seed_file(tmp: Path, *, prefix: str = "TEST", log=None, env_bool=None, clock=None, register=None):
    return PSF.ProxySeedFile(
        env_prefix=prefix,
        base_dir=tmp,
        proxy_chain_key=lambda proxy: f"chain:{str(proxy).strip()}" if str(proxy).strip() else "",
        label=lambda _value: "LABEL",
        redact=lambda value: value,
        register=register or (lambda _proxy: None),
        env_bool=env_bool or (lambda _name, default: default),
        log=log,
        clock=clock or (lambda: 1_700_000_000.0),
    )


class ProxySeedFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env_names: list[str] = []

    def tearDown(self):
        for name in self._env_names:
            os.environ.pop(name, None)
        self._tmp.cleanup()

    def _set_env(self, name: str, value: str) -> None:
        self._env_names.append(name)
        os.environ[name] = value

    # -- path ---------------------------------------------------------------

    def test_path_defaults_to_base_dir(self):
        self.assertEqual(_seed_file(self.tmp).path(), self.tmp / "proxy_seeds.txt")

    def test_path_prefers_the_prefixed_env_var(self):
        target = self.tmp / "custom.txt"
        self._set_env("TEST_PROXY_SEED_FILE", str(target))
        self.assertEqual(_seed_file(self.tmp).path(), target)

    def test_path_falls_back_to_the_shared_env_var(self):
        target = self.tmp / "shared.txt"
        self._set_env("PP_PROXY_SEED_FILE", str(target))
        self.assertEqual(_seed_file(self.tmp).path(), target)

    # -- unique -------------------------------------------------------------

    def test_unique_dedups_by_chain_key_and_logs(self):
        messages: list[tuple] = []
        seed_file = _seed_file(self.tmp, log=lambda *a: messages.append(a))
        self.assertEqual(seed_file.unique(["a", "a", "b"]), ["a", "b"])
        self.assertEqual(messages, [("代理 Seed 去重: 忽略相同 sticky session 1 条", "[WARN] ")])

    def test_unique_counts_blank_keys_as_duplicates(self):
        messages: list[tuple] = []
        seed_file = _seed_file(self.tmp, log=lambda *a: messages.append(a))
        self.assertEqual(seed_file.unique(["", "a"]), ["a"])
        self.assertEqual(messages, [("代理 Seed 去重: 忽略相同 sticky session 1 条", "[WARN] ")])

    # -- remove_failed ------------------------------------------------------

    def test_remove_failed_rewrites_seed_file_and_quarantines(self):
        registered: list[str] = []
        seed_file = _seed_file(self.tmp, register=registered.append)
        path = seed_file.path()
        path.write_text("p1\np2\np3\n", encoding="utf-8")
        removed = seed_file.remove_failed("seed", [("p2", "timeout")])
        self.assertEqual(removed, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "p1\np3\n")
        self.assertEqual(registered, ["p2"])
        records = [
            json.loads(line) for line in (self.tmp / "removed_proxies.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["group"], "seed")
        self.assertEqual(records[0]["proxy"], "LABEL")
        self.assertEqual(records[0]["reason"], "timeout")
        self.assertEqual(records[0]["source"], "proxy_seeds.txt")

    def test_remove_failed_missing_file_is_a_noop(self):
        self.assertEqual(_seed_file(self.tmp).remove_failed("seed", [("p2", "timeout")]), 0)

    def test_remove_failed_no_match_leaves_the_file_untouched(self):
        seed_file = _seed_file(self.tmp)
        path = seed_file.path()
        path.write_text("p1\n", encoding="utf-8")
        self.assertEqual(seed_file.remove_failed("seed", [("p9", "timeout")]), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "p1\n")
        self.assertFalse((self.tmp / "removed_proxies.jsonl").exists())

    def test_remove_failed_disabled_by_env_is_a_noop(self):
        seed_file = _seed_file(self.tmp, env_bool=lambda _name, _default: False)
        seed_file.path().write_text("p1\np2\n", encoding="utf-8")
        self.assertEqual(seed_file.remove_failed("seed", [("p1", "timeout")]), 0)
        self.assertEqual(seed_file.path().read_text(encoding="utf-8"), "p1\np2\n")

    def test_remove_failed_empty_failures_is_a_noop(self):
        self.assertEqual(_seed_file(self.tmp).remove_failed("seed", []), 0)

    # -- free-function surface ---------------------------------------------

    def test_free_functions_delegate_to_the_passed_seed_file(self):
        seed_file = _seed_file(self.tmp)
        self.assertEqual(PSF.proxy_seed_file(seed_file), seed_file.path())
        self.assertEqual(PSF.unique_proxy_seeds(seed_file, ["a", "a"]), ["a"])
        seed_file.path().write_text("a\n", encoding="utf-8")
        self.assertEqual(PSF.remove_failed_proxies(seed_file, "seed", [("a", "x")]), 1)


if __name__ == "__main__":
    unittest.main()
