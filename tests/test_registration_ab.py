"""Tests for scripts/registration_ab.py.

The A/B harness never touches the network: ``parse_preflight_log``,
``load_funnel``, ``read_toggles``, ``build_record`` and ``compare_records`` are
pure. These tests pin the log grammar of ``commands/registration`` and the
pre-registered decision rule, so a change to either cannot silently change how a
live comparison is judged.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import registration_ab as ab  # noqa: E402

_MIXED_LOG = """\
[*] 注册预检：3 个候选路由（单候选上限 3 次、总预算 180s）
[*] 注册预检 1/3 host-a 可用（1.2s）
[!] 注册预检 2/3 host-b 被 Cloudflare 挑战 (0.9s)：registration_preflight_failed:chatgpt-login:cloudflare_challenge
[!] 注册预检 3/3 host-c 失败 (0.4s)：registration_preflight_failed:sentinel-frame:http_403
[*] 注册预检完成：1/3 条实测可用，批次只使用通过 OpenAI 边界检查的路由
[!] 注册预检：以下出口主机连续失败已达上限，剩余候选已跳过：host-b×3, host-c×2
registration_preflight_failed:no_healthy_route:RuntimeError
"""


def _funnel(attempted: int, registered: int, failures: dict[str, int] | None = None) -> dict:
    return {
        "attempted": attempted,
        "registered": registered,
        "registered_per_attempted": round(registered / attempted, 4) if attempted else None,
        "registration_failures_by_class": failures or {},
    }


def _cf_log(ok: int, cf: int) -> str:
    lines = []
    for index in range(1, ok + cf + 1):
        if index <= ok:
            lines.append(f"[*] 注册预检 {index}/{ok + cf} host-{index} 可用（1.0s）")
        else:
            lines.append(f"[!] 注册预检 {index}/{ok + cf} host-{index} 被 Cloudflare 挑战 (1.0s)：cloudflare_challenge")
    return "\n".join(lines) + "\n"


def _record(experiment: str, arm: str, toggle_value, *, attempted: int, registered: int, log: str = "", failures=None):
    toggle_name = ab.EXPERIMENTS[experiment]["toggle"]
    toggles = {toggle_name: toggle_value} if toggle_name else {}
    return ab.build_record(
        experiment=experiment,
        arm=arm,
        log_text=log,
        funnel=_funnel(attempted, registered, failures),
        toggles=toggles,
    )


class ParsePreflightLogTests(unittest.TestCase):
    def test_counts_and_hosts(self):
        parsed = ab.parse_preflight_log(_MIXED_LOG)
        self.assertEqual(parsed["probes"], 3)
        self.assertEqual(parsed["probes_ok"], 1)
        self.assertEqual(parsed["probes_cloudflare"], 1)
        self.assertEqual(parsed["probes_failed"], 1)
        self.assertEqual(parsed["cloudflare_rate"], 0.3333)
        self.assertEqual(parsed["cloudflare_hosts"], {"host-b": 1})
        self.assertEqual(parsed["failed_hosts"], {"host-c": 1})
        self.assertEqual(parsed["completed_ok"], 1)
        self.assertEqual(parsed["completed_attempted"], 3)
        self.assertEqual(parsed["skipped_hosts"], {"host-b": 3, "host-c": 2})
        self.assertEqual(parsed["no_healthy_route"], "RuntimeError")

    def test_empty_log_is_not_an_error(self):
        parsed = ab.parse_preflight_log("")
        self.assertEqual(parsed["probes"], 0)
        self.assertIsNone(parsed["cloudflare_rate"])
        self.assertEqual(parsed["cloudflare_hosts"], {})

    def test_skipped_summary_separator_is_not_part_of_the_host(self):
        parsed = ab.parse_preflight_log("剩余候选已跳过：a.example×2, b.example×1")
        self.assertEqual(parsed["skipped_hosts"], {"a.example": 2, "b.example": 1})


class IoHelperTests(unittest.TestCase):
    def test_load_funnel_accepts_nested_flat_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested.json"
            nested.write_text(json.dumps({"ok": True, "funnel": {"attempted": 5}}), encoding="utf-8")
            flat = Path(tmp) / "flat.json"
            flat.write_text(json.dumps({"attempted": 7}), encoding="utf-8")
            none = Path(tmp) / "none.json"
            none.write_text(json.dumps({"ok": True}), encoding="utf-8")
            self.assertEqual(ab.load_funnel(nested), {"attempted": 5})
            self.assertEqual(ab.load_funnel(flat), {"attempted": 7})
            self.assertIsNone(ab.load_funnel(none))
        self.assertIsNone(ab.load_funnel(None))

    def test_read_toggles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"registration": {"preflight_login_page": "legacy", "sentinel_password_bundle": True}}),
                encoding="utf-8",
            )
            self.assertEqual(
                ab.read_toggles(path),
                {"registration.preflight_login_page": "legacy", "registration.sentinel_password_bundle": True},
            )
        self.assertEqual(ab.read_toggles(None), {})


class BuildRecordTests(unittest.TestCase):
    def test_verified_when_config_matches_the_arm(self):
        record = _record("p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=30)
        self.assertTrue(record["toggle_verified"])

    def test_unverified_when_config_is_absent_or_wrong(self):
        wrong = _record("p0-1-preflight-endpoint", "browser", "legacy", attempted=40, registered=30)
        self.assertFalse(wrong["toggle_verified"])
        absent = _record("p0-1-preflight-endpoint", "browser", None, attempted=40, registered=30)
        self.assertFalse(absent["toggle_verified"])

    def test_unknown_experiment_is_rejected(self):
        with self.assertRaises(ValueError):
            ab.build_record(experiment="nope", arm="x", log_text="", funnel=None, toggles={})


class CompareRecordsTests(unittest.TestCase):
    def test_no_data(self):
        self.assertEqual(ab.compare_records([])["verdict"], "no_data")

    def test_mixed_experiments_is_refused(self):
        rows = [
            _record("p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=30),
            _record("p1-3-sentinel-password-bundle", "default", False, attempted=40, registered=30),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "mixed_experiments")

    def test_unverified_arm_refuses_a_verdict(self):
        rows = [
            _record("p0-1-preflight-endpoint", "browser", "legacy", attempted=40, registered=30),
            _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=40, registered=30),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "unverified")
        self.assertIn("browser", report["reason"])

    def test_underpowered_before_any_rate_call(self):
        rows = [
            _record("p0-1-preflight-endpoint", "browser", "browser", attempted=5, registered=5, log=_cf_log(5, 0)),
            _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=5, registered=1, log=_cf_log(1, 4)),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "underpowered")

    def test_p0_1_favors_lower_challenge_rate(self):
        browser = _record(
            "p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=35, log=_cf_log(10, 0)
        )
        legacy = _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=40, registered=20, log=_cf_log(8, 2))
        self.assertEqual(ab.compare_records([browser, legacy])["verdict"], "favor_browser")
        self.assertEqual(ab.compare_records([legacy, browser])["verdict"], "favor_browser")

    def test_p0_1_favors_legacy_when_it_is_clearly_better(self):
        browser = _record(
            "p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=20, log=_cf_log(8, 2)
        )
        legacy = _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=40, registered=35, log=_cf_log(10, 0))
        self.assertEqual(ab.compare_records([browser, legacy])["verdict"], "favor_legacy")

    def test_p0_1_inconclusive_within_delta(self):
        browser = _record(
            "p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=30, log=_cf_log(10, 1)
        )
        legacy = _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=40, registered=30, log=_cf_log(10, 1))
        self.assertEqual(ab.compare_records([browser, legacy])["verdict"], "inconclusive")

    def test_p0_2_is_observation_only(self):
        row = ab.build_record(
            experiment="p0-2-cloudflare-observation",
            arm="observe",
            log_text=_cf_log(3, 1),
            funnel=_funnel(40, 30),
            toggles={},
        )
        report = ab.compare_records([row])
        self.assertEqual(report["verdict"], "observation_only")

    def test_p1_3_favors_bundle_when_clearly_better(self):
        default = _record("p1-3-sentinel-password-bundle", "default", False, attempted=40, registered=20)
        bundle = _record("p1-3-sentinel-password-bundle", "bundle", True, attempted=40, registered=32)
        self.assertEqual(ab.compare_records([default, bundle])["verdict"], "favor_bundle")

    def test_p1_3_keeps_default_off_on_sentinel_regression(self):
        default = _record("p1-3-sentinel-password-bundle", "default", False, attempted=40, registered=20)
        bundle = _record(
            "p1-3-sentinel-password-bundle",
            "bundle",
            True,
            attempted=40,
            registered=25,
            failures={"sentinel_extract_failed": 3},
        )
        report = ab.compare_records([default, bundle])
        self.assertEqual(report["verdict"], "keep_default_off")
        self.assertIn("sentinel_extract_failed", report["reason"])

    def test_p1_3_underpowered(self):
        default = _record("p1-3-sentinel-password-bundle", "default", False, attempted=3, registered=1)
        bundle = _record("p1-3-sentinel-password-bundle", "bundle", True, attempted=3, registered=3)
        self.assertEqual(ab.compare_records([default, bundle])["verdict"], "underpowered")


class CliTests(unittest.TestCase):
    def test_plan_lists_every_experiment(self):
        self.assertEqual(ab.main(["plan"]), 0)

    def test_collect_writes_a_verified_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text(_cf_log(10, 0), encoding="utf-8")
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"registration": {"preflight_login_page": "browser"}}), encoding="utf-8")
            out = Path(tmp) / "out"
            code = ab.main(
                [
                    "collect",
                    "--experiment",
                    "p0-1-preflight-endpoint",
                    "--arm",
                    "browser",
                    "--log",
                    str(log),
                    "--config",
                    str(config),
                    "--out-dir",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            record = json.loads((out / "p0-1-preflight-endpoint__browser.json").read_text(encoding="utf-8"))
            self.assertTrue(record["toggle_verified"])
            self.assertEqual(record["preflight"]["probes_ok"], 10)

    def test_compare_exits_nonzero_without_a_verdict(self):
        rows = [
            _record("p0-1-preflight-endpoint", "browser", "legacy", attempted=40, registered=30),
            _record("p0-1-preflight-endpoint", "legacy", "legacy", attempted=40, registered=30),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, row in enumerate(rows):
                path = Path(tmp) / f"{index}.json"
                path.write_text(json.dumps(row), encoding="utf-8")
                paths.append(f"{row['arm']}={path}")
            self.assertEqual(ab.main(["compare", "--arm", paths[0], "--arm", paths[1]]), 1)


if __name__ == "__main__":
    unittest.main()
