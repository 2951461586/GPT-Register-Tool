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
import registration_ab as ab  # noqa: E402  # type: ignore

_MIXED_LOG = """\
[*] 注册预检：3 个候选路由（单候选上限 3 次、总预算 180s）
[*] 注册预检 1/3 host-a 可用（1.2s）
[!] 注册预检 2/3 host-b 被 Cloudflare 挑战 (0.9s)：registration_preflight_failed:chatgpt-login:cloudflare_challenge
[!] 注册预检 3/3 host-c 失败 (0.4s)：registration_preflight_failed:sentinel-frame:http_403
[*] 注册预检完成：1/3 条实测可用，批次只使用通过 OpenAI 边界检查的路由
[!] 注册预检：以下出口主机连续失败已达上限，剩余候选已跳过：host-b×3, host-c×2
registration_preflight_failed:no_healthy_route:RuntimeError
"""


def _funnel(attempted: int, registered: int, failures: dict[str, int] | None = None, edge: dict | None = None) -> dict:
    funnel = {
        "attempted": attempted,
        "registered": registered,
        "registered_per_attempted": round(registered / attempted, 4) if attempted else None,
        "registration_failures_by_class": failures or {},
    }
    if edge is not None:
        funnel["edge_challenge"] = edge
    return funnel


def _cf_log(ok: int, cf: int) -> str:
    lines = []
    for index in range(1, ok + cf + 1):
        if index <= ok:
            lines.append(f"[*] 注册预检 {index}/{ok + cf} host-{index} 可用（1.0s）")
        else:
            lines.append(f"[!] 注册预检 {index}/{ok + cf} host-{index} 被 Cloudflare 挑战 (1.0s)：cloudflare_challenge")
    return "\n".join(lines) + "\n"


def _record(
    experiment: str,
    arm: str,
    toggle_value,
    *,
    attempted: int,
    registered: int,
    log: str = "",
    failures=None,
    edge=None,
):
    """Build one arm record with both manipulation checks satisfied by default.

    ``build_record`` now also demands the arm's *mechanism* line when the arm's
    toggle value is truthy (and its absence in the control).  A caller that
    passes no log would otherwise get every powered comparison refused as
    ``manipulation_failed``, so the marker is synthesised here; tests that care
    about the check itself pass ``mechanism=False`` to omit it, or a literal
    marker line in ``log`` to place it deliberately.
    """
    toggle_name = ab.EXPERIMENTS[experiment]["toggle"]
    toggles = {toggle_name: toggle_value} if toggle_name else {}
    marker = ab.EXPERIMENTS[experiment].get("mechanism") or ""
    if marker and toggle_value:
        log = f"{log}\n  {marker}: 200\n"
    return ab.build_record(
        experiment=experiment,
        arm=arm,
        log_text=log,
        funnel=_funnel(attempted, registered, failures, edge),
        toggles=toggles,
    )


class PrimePasswordPageExperimentTests(unittest.TestCase):
    """P1-4 判定规则：prime 密码页对照的预注册语义。"""

    def test_favor_prime_when_rate_improves_and_mailbox_family_shrinks(self):
        rows = [
            _record("p1-4-prime-password-page", "default", False, attempted=30, registered=5, failures={"mailbox": 20}),
            _record("p1-4-prime-password-page", "prime", True, attempted=30, registered=14, failures={"mailbox": 11}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_prime")
        self.assertTrue(report["powered"])

    def test_prime_win_with_a_growing_mailbox_family_is_flagged(self):
        """机制读数与速率判定矛盾时必须显式暴露，不许静默吞掉。"""
        rows = [
            _record("p1-4-prime-password-page", "default", False, attempted=30, registered=5, failures={"mailbox": 10}),
            _record("p1-4-prime-password-page", "prime", True, attempted=30, registered=14, failures={"mailbox": 15}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_prime_with_red_flag")

    def test_worse_prime_keeps_the_default(self):
        rows = [
            _record("p1-4-prime-password-page", "default", False, attempted=30, registered=12, failures={"mailbox": 8}),
            _record("p1-4-prime-password-page", "prime", True, attempted=30, registered=4, failures={"mailbox": 15}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_underpowered_arms_are_not_judged(self):
        rows = [
            _record("p1-4-prime-password-page", "default", False, attempted=10, registered=2),
            _record("p1-4-prime-password-page", "prime", True, attempted=10, registered=5),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "underpowered")

    def test_unverified_toggle_refuses_the_verdict(self):
        rows = [
            _record("p1-4-prime-password-page", "default", False, attempted=30, registered=5),
            _record("p1-4-prime-password-page", "prime", True, attempted=30, registered=14),
        ]
        rows[1]["toggle_verified"] = False
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "unverified")


class SignupContinueScreenHintExperimentTests(unittest.TestCase):
    """P1-5 判定规则：signup continue 声明 screen_hint 的预注册语义。"""

    def test_favor_hint_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record(
                "p1-5-signup-continue-screen-hint",
                "default",
                False,
                attempted=30,
                registered=5,
                failures={"mailbox": 20},
            ),
            _record(
                "p1-5-signup-continue-screen-hint", "hint", True, attempted=30, registered=14, failures={"mailbox": 12}
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_hint")

    def test_auth_state_growth_keeps_the_default_even_when_rate_improves(self):
        """声明的 screen 与服务端事务解读冲突 ⇒ 无论速率如何都不默认开启。"""
        rows = [
            _record(
                "p1-5-signup-continue-screen-hint",
                "default",
                False,
                attempted=30,
                registered=5,
                failures={"auth_state": 3},
            ),
            _record(
                "p1-5-signup-continue-screen-hint",
                "hint",
                True,
                attempted=30,
                registered=14,
                failures={"auth_state": 9},
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-5-signup-continue-screen-hint", "default", False, attempted=30, registered=10),
            _record("p1-5-signup-continue-screen-hint", "hint", True, attempted=30, registered=11),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_read_toggles_exposes_both_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "registration": {
                            "prime_create_account_password": True,
                            "signup_continue_screen_hint": True,
                            "prime_about_you_page": True,
                            "create_account_disallowed_backoff": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            toggles = ab.read_toggles(config)
        self.assertEqual(toggles["registration.prime_create_account_password"], True)
        self.assertEqual(toggles["registration.signup_continue_screen_hint"], True)
        self.assertEqual(toggles["registration.prime_about_you_page"], True)
        self.assertEqual(toggles["registration.create_account_disallowed_backoff"], True)


class PrimeAboutYouPageExperimentTests(unittest.TestCase):
    """P1-6 判定规则：about-you 页面 prime 的预注册语义。"""

    def test_favor_prime_when_rate_improves_without_account_growth(self):
        rows = [
            _record(
                "p1-6-prime-about-you-page", "default", False, attempted=30, registered=5, failures={"account": 18}
            ),
            _record("p1-6-prime-about-you-page", "prime", True, attempted=30, registered=13, failures={"account": 12}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_prime")

    def test_account_class_growth_keeps_the_default_even_when_rate_improves(self):
        """导航扰动事务 ⇒ 无论速率如何都不默认开启。"""
        rows = [
            _record("p1-6-prime-about-you-page", "default", False, attempted=30, registered=5, failures={"account": 4}),
            _record("p1-6-prime-about-you-page", "prime", True, attempted=30, registered=13, failures={"account": 10}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_worse_prime_keeps_the_default(self):
        rows = [
            _record("p1-6-prime-about-you-page", "default", False, attempted=30, registered=11),
            _record("p1-6-prime-about-you-page", "prime", True, attempted=30, registered=4),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")


class CreateDisallowedBackoffExperimentTests(unittest.TestCase):
    """P1-7 判定规则：create_account 退避重试的预注册语义。"""

    def test_favor_backoff_when_rate_improves(self):
        rows = [
            _record("p1-7-create-disallowed-backoff", "default", False, attempted=30, registered=4),
            _record("p1-7-create-disallowed-backoff", "backoff", True, attempted=30, registered=12),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_backoff")

    def test_worse_backoff_keeps_the_default(self):
        """拒绝是永久性而非风险窗口 ⇒ 重试只添延迟。"""
        rows = [
            _record("p1-7-create-disallowed-backoff", "default", False, attempted=30, registered=10),
            _record("p1-7-create-disallowed-backoff", "backoff", True, attempted=30, registered=3),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-7-create-disallowed-backoff", "default", False, attempted=30, registered=10),
            _record("p1-7-create-disallowed-backoff", "backoff", True, attempted=30, registered=11),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_unverified_toggle_refuses_the_verdict(self):
        rows = [
            _record("p1-7-create-disallowed-backoff", "default", False, attempted=30, registered=4),
            _record("p1-7-create-disallowed-backoff", "backoff", True, attempted=30, registered=12),
        ]
        rows[1]["toggle_verified"] = False
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "unverified")


class InflowChallengeHandoffExperimentTests(unittest.TestCase):
    """P0-2b 判定规则：三臂 + §4.1 的无效结果判据。"""

    def _arm(self, arm, value, *, attempted=40, registered=10, failures=None, edge=None):
        return _record(
            "p0-2b-inflow-challenge-handoff",
            arm,
            value,
            attempted=attempted,
            registered=registered,
            failures=failures,
            edge=edge,
        )

    def _three(self, *, observe_reg=10, rotate_reg=22, edge_hits=3, rotations=2, failures=None, edge=None):
        return [
            self._arm(
                "observe", False, registered=observe_reg, edge={"hits": edge_hits, "rotations": 0}, failures=failures
            ),
            self._arm(
                "rotate",
                True,
                registered=rotate_reg,
                edge={"hits": edge_hits, "rotations": rotations},
                failures=failures,
            ),
            self._arm(
                "handoff",
                True,
                registered=rotate_reg,
                edge={"hits": edge_hits, "rotations": rotations},
                failures=failures,
            ),
        ]

    def test_favors_rotate_when_the_rate_moves_without_a_new_class(self):
        report = ab.compare_records(self._three())
        self.assertEqual(report["verdict"], "favor_rotate")
        self.assertIn("H2 (handoff) is not judged", report["reason"])
        self.assertEqual(report["metrics"]["observe"]["mailboxes_consumed_per_registered"], 4.0)
        self.assertEqual(report["metrics"]["rotate"]["mailboxes_consumed_per_registered"], 1.8182)

    def test_no_challenge_in_any_arm_is_not_judgeable_not_no_effect(self):
        rows = self._three(edge_hits=0, rotations=0)
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "not_judgeable")
        self.assertIn("measured nothing", report["reason"])

    def test_a_rotate_arm_that_never_rotated_is_a_copy_of_observe(self):
        """§4 manipulation check: single-slot pool ⇒ the arm proves nothing."""
        rows = self._three(edge_hits=3, rotations=0)
        rows[1]["funnel"]["edge_challenge"]["rotate_failed"] = 3
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "not_judgeable")
        self.assertIn("rotated 0 times", report["reason"])

    def test_a_funnel_without_the_block_is_inconclusive(self):
        rows = self._three()
        for row in rows:
            row["funnel"].pop("edge_challenge", None)
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "inconclusive")
        self.assertIn("no edge_challenge block", report["reason"])

    def test_a_new_failure_class_stops_all_arms(self):
        rows = [
            self._arm("observe", False, registered=10, edge={"hits": 3, "rotations": 0}),
            self._arm(
                "rotate",
                True,
                registered=22,
                edge={"hits": 3, "rotations": 2},
                failures={"sentinel": 1},
            ),
            self._arm("handoff", True, registered=22, edge={"hits": 3, "rotations": 2}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "stop_all_arms")
        self.assertIn("sentinel", report["reason"])

    def test_worse_rotate_keeps_the_default_off(self):
        report = ab.compare_records(self._three(observe_reg=22, rotate_reg=10))
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        report = ab.compare_records(self._three(observe_reg=10, rotate_reg=11))
        self.assertEqual(report["verdict"], "inconclusive")

    def test_the_handoff_arm_is_verified_against_the_rotate_axis(self):
        """The harness verifies one toggle per arm; handoff is the all-on superset."""
        rows = self._three()
        report = ab.compare_records(rows)
        self.assertIn("handoff", report["arms"])
        self.assertTrue(report["powered"])

    def test_read_toggles_exposes_the_edge_challenge_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "registration": {
                            "edge_challenge_discrimination": True,
                            "edge_challenge_rotate_exit": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                ab.read_toggles(path),
                {
                    "registration.edge_challenge_discrimination": True,
                    "registration.edge_challenge_rotate_exit": False,
                },
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

    def test_read_toggles_exposes_every_new_toggle(self):
        """P1-4..P1-8 的开关都必须进记录，否则 ``collect`` 无法校验操纵。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "registration": {
                            "prime_create_account_password": True,
                            "signup_continue_screen_hint": False,
                            "signup_email_verification_continue_hint": True,
                            "prime_about_you_page": False,
                            "create_account_disallowed_backoff": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                ab.read_toggles(path),
                {
                    "registration.prime_create_account_password": True,
                    "registration.signup_continue_screen_hint": False,
                    "registration.signup_email_verification_continue_hint": True,
                    "registration.prime_about_you_page": False,
                    "registration.create_account_disallowed_backoff": True,
                },
            )

    def test_read_toggles_covers_every_declared_experiment_toggle(self):
        """``EXPERIMENTS`` 的每个 toggle 都必须能被 ``read_toggles`` 读到。

        2026-10-10 扫描：``read_toggles`` 是手写 if 链，P1-9..P1-12 加进设计表时
        漏加，``compare`` 对它们恒返回 ``unverified``（``--config`` 也救不回），
        而 ``unverified`` 先于机制门禁返回 ⇒ 刚补的假对照门禁对这四个实验失效。
        这个测试遍历设计表而不是写死键名，所以下一个新实验不能再静默漏掉。
        """
        declared = {str(design["toggle"]) for design in ab.EXPERIMENTS.values() if design.get("toggle")}
        self.assertTrue(declared, "EXPERIMENTS 必须至少声明一个 toggle")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"registration": {name.split(".", 1)[1]: True for name in declared}}),
                encoding="utf-8",
            )
            toggles = ab.read_toggles(path)
        self.assertEqual(sorted(toggles), sorted(declared))
        # 布尔 arm 的实验必须因此通过第一道操纵检查（字符串 arm 的 p0-1 不适用）。
        for experiment, design in ab.EXPERIMENTS.items():
            truthy_arms = [name for name, value in design["arms"].items() if value is True]
            if not truthy_arms:
                continue
            record = ab.build_record(
                experiment=experiment, arm=truthy_arms[0], log_text="", funnel=None, toggles=toggles
            )
            self.assertTrue(
                record["toggle_verified"],
                f"{experiment}/{truthy_arms[0]} 的 toggle 未被 read_toggles 覆盖 ⇒ compare 恒 unverified",
            )

    def test_unreadable_artifacts_degrade_instead_of_raising(self):
        """缺失/损坏的输入不得让工具在能报 ``unverified`` 之前崩掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertIsNone(ab.load_funnel(missing))
            self.assertEqual(ab.read_toggles(missing), {})
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertIsNone(ab.load_funnel(broken))
            self.assertEqual(ab.read_toggles(broken), {})


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

    def test_mechanism_seen_when_the_arm_log_carries_the_marker(self):
        record = _record("p1-5-signup-continue-screen-hint", "hint", True, attempted=40, registered=20)
        self.assertEqual(record["mechanism_marker"], "Signup continue declares screen_hint=signup")
        self.assertTrue(record["mechanism_seen"])
        self.assertTrue(record["mechanism_ok"])

    def test_mechanism_missing_in_the_treatment_arm_is_a_failure(self):
        """``run_p15_hint.log`` 的形状：开关开着，但日志里没有那条独占行。"""
        record = _record("p1-5-signup-continue-screen-hint", "hint", True, attempted=5, registered=0)
        record["mechanism_seen"] = False
        record["mechanism_ok"] = False
        self.assertTrue(ab._mechanism_failed(record))

    def test_mechanism_marker_in_the_control_arm_is_a_failure(self):
        """对照臂出现处理行 ⇒ 不是对照（同一开关在两个 arm 都跑了）。"""
        record = _record("p1-4-prime-password-page", "default", False, attempted=40, registered=20)
        record["mechanism_seen"] = True
        record["mechanism_ok"] = False
        self.assertTrue(ab._mechanism_failed(record))

    def test_experiments_without_a_marker_are_not_judged(self):
        """P0-1 的 arm 值是端点名（字符串），不能推出 marker 应在哪一侧。"""
        record = _record("p0-1-preflight-endpoint", "browser", "browser", attempted=40, registered=30)
        self.assertIsNone(record["mechanism_ok"])
        self.assertFalse(ab._mechanism_failed(record))

    def test_none_is_not_a_mechanism_failure(self):
        self.assertFalse(ab._mechanism_failed({"mechanism_ok": None}))
        self.assertFalse(ab._mechanism_failed({}))
        self.assertTrue(ab._mechanism_failed({"mechanism_ok": False}))
        self.assertFalse(ab._mechanism_failed({"mechanism_ok": True}))


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

    def test_manipulation_failed_beats_underpowered(self):
        """P0-A 的核心断言：机制没跑的 arm 是 ``manipulation_failed``，不是 ``underpowered``。

        回放 ``run_p15_hint.log`` 的形状：两个 arm 都只有 5 手（远低于 30），hint 臂的
        开关也验证通过，但它的日志里从来没有 P1-5 的机制行。
        """
        default = _record("p1-5-signup-continue-screen-hint", "default", False, attempted=5, registered=0)
        hint = _record("p1-5-signup-continue-screen-hint", "hint", True, attempted=5, registered=0)
        hint["mechanism_seen"] = False
        hint["mechanism_ok"] = False
        report = ab.compare_records([default, hint])
        self.assertEqual(report["verdict"], "manipulation_failed")
        self.assertIn("hint", report["reason"])
        self.assertIn("Signup continue declares screen_hint=signup", report["reason"])
        self.assertFalse(report["powered"])

    def test_manipulation_failed_when_the_marker_leaks_into_the_control(self):
        default = _record("p1-6-prime-about-you-page", "default", False, attempted=40, registered=20)
        default["mechanism_seen"] = True
        default["mechanism_ok"] = False
        prime = _record("p1-6-prime-about-you-page", "prime", True, attempted=40, registered=32)
        self.assertEqual(ab.compare_records([default, prime])["verdict"], "manipulation_failed")

    def test_mechanism_status_is_reported_per_arm(self):
        rows = [
            _record("p1-5-signup-continue-screen-hint", "default", False, attempted=40, registered=20),
            _record("p1-5-signup-continue-screen-hint", "hint", True, attempted=40, registered=32),
        ]
        report = ab.compare_records(rows)
        self.assertTrue(report["metrics"]["hint"]["mechanism_ok"])
        self.assertTrue(report["metrics"]["default"]["mechanism_ok"])
        self.assertEqual(report["metrics"]["hint"]["mechanism_marker"], "Signup continue declares screen_hint=signup")


class EmailVerificationContinueHintExperimentTests(unittest.TestCase):
    """P1-8 判定规则：从 ``/email-verification`` 补发声明 screen 的 continue。"""

    def test_favor_hint_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record("p1-8-email-verification-continue-hint", "default", False, attempted=40, registered=6),
            _record("p1-8-email-verification-continue-hint", "hint", True, attempted=40, registered=18),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_hint")
        self.assertTrue(report["powered"])

    def test_auth_state_growth_keeps_the_default_off(self):
        rows = [
            _record(
                "p1-8-email-verification-continue-hint",
                "default",
                False,
                attempted=40,
                registered=6,
                failures={"auth_state": 4},
            ),
            _record(
                "p1-8-email-verification-continue-hint",
                "hint",
                True,
                attempted=40,
                registered=18,
                failures={"auth_state": 11},
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")
        self.assertIn("auth_state", report["reason"])

    def test_underpowered_arms_are_not_judged(self):
        rows = [
            _record("p1-8-email-verification-continue-hint", "default", False, attempted=5, registered=1),
            _record("p1-8-email-verification-continue-hint", "hint", True, attempted=5, registered=3),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "underpowered")

    def test_its_marker_is_distinct_from_p1_5(self):
        """两个实验的机制行必须不同，否则一个 arm 能冒充另一个实验的操纵。"""
        self.assertNotEqual(
            ab.EXPERIMENTS["p1-8-email-verification-continue-hint"]["mechanism"],
            ab.EXPERIMENTS["p1-5-signup-continue-screen-hint"]["mechanism"],
        )


class PrimeNavigationHeadersExperimentTests(unittest.TestCase):
    """P1-9 判定规则：prime 导航头对照（P1-A）。"""

    def test_favor_headers_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record("p1-9-prime-navigation-headers", "default", False, attempted=40, registered=6),
            _record("p1-9-prime-navigation-headers", "headers", True, attempted=40, registered=18),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_headers")
        self.assertTrue(report["powered"])

    def test_auth_state_growth_keeps_the_default_off(self):
        rows = [
            _record(
                "p1-9-prime-navigation-headers",
                "default",
                False,
                attempted=40,
                registered=10,
                failures={"auth_state": 2},
            ),
            _record(
                "p1-9-prime-navigation-headers",
                "headers",
                True,
                attempted=40,
                registered=14,
                failures={"auth_state": 9},
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-9-prime-navigation-headers", "default", False, attempted=40, registered=10),
            _record("p1-9-prime-navigation-headers", "headers", True, attempted=40, registered=11),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "inconclusive")

    def test_hold_constant_pins_prime_on_in_both_arms(self):
        """导航头是 prime 上的**增量**，不是 prime 的替代品。"""
        held = " ".join(ab.EXPERIMENTS["p1-9-prime-navigation-headers"]["hold_constant"])
        self.assertIn("prime_create_account_password true in BOTH arms", held)

    def test_p1_5_hold_constant_pins_the_p1_8_toggle_off(self):
        """P1-8 的强制路径会复用 continue 分支，所以 P1-5 必须钉它关。"""
        held = " ".join(ab.EXPERIMENTS["p1-5-signup-continue-screen-hint"]["hold_constant"])
        self.assertIn("signup_email_verification_continue_hint false in BOTH arms", held)


class SigninScreenHintExperimentTests(unittest.TestCase):
    """P1-10 判定规则：signin 的 screen_hint 对照。"""

    def test_favor_login_or_signup_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record("p1-10-signin-screen-hint", "default", False, attempted=40, registered=6),
            _record("p1-10-signin-screen-hint", "login_or_signup", True, attempted=40, registered=18),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_login_or_signup")
        self.assertTrue(report["powered"])

    def test_auth_state_growth_keeps_the_default_off(self):
        rows = [
            _record(
                "p1-10-signin-screen-hint", "default", False, attempted=40, registered=10, failures={"auth_state": 2}
            ),
            _record(
                "p1-10-signin-screen-hint",
                "login_or_signup",
                True,
                attempted=40,
                registered=14,
                failures={"auth_state": 9},
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-10-signin-screen-hint", "default", False, attempted=40, registered=10),
            _record("p1-10-signin-screen-hint", "login_or_signup", True, attempted=40, registered=11),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "inconclusive")

    def test_hold_constant_pins_the_continue_behaviour(self):
        """只准动 signin 的 screen_hint，continue 行为必须两臂恒定。"""
        held = " ".join(ab.EXPERIMENTS["p1-10-signin-screen-hint"]["hold_constant"])
        self.assertIn("authorize/continue behaviour unchanged in BOTH arms", held)

    def test_its_marker_is_distinct_from_every_other_experiment(self):
        markers = {name: design["mechanism"] for name, design in ab.EXPERIMENTS.items() if design.get("mechanism")}
        self.assertEqual(len(markers), len(set(markers.values())), f"duplicate mechanism lines: {markers}")


class SigninPromptLoginExperimentTests(unittest.TestCase):
    """P1-11 判定规则：signin 的 prompt 对照。"""

    def test_favor_prompt_login_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record("p1-11-signin-prompt-login", "default", False, attempted=40, registered=6),
            _record("p1-11-signin-prompt-login", "prompt_login", True, attempted=40, registered=18),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_prompt_login")
        self.assertTrue(report["powered"])

    def test_auth_state_growth_keeps_the_default_off(self):
        rows = [
            _record(
                "p1-11-signin-prompt-login", "default", False, attempted=40, registered=10, failures={"auth_state": 2}
            ),
            _record(
                "p1-11-signin-prompt-login",
                "prompt_login",
                True,
                attempted=40,
                registered=14,
                failures={"auth_state": 9},
            ),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-11-signin-prompt-login", "default", False, attempted=40, registered=10),
            _record("p1-11-signin-prompt-login", "prompt_login", True, attempted=40, registered=11),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "inconclusive")

    def test_hold_constant_pins_the_p1_10_toggle_on(self):
        """turb 把 ``prompt=login`` 与 ``login_or_signup`` 配对，所以两臂都要开 P1-10。"""
        held = " ".join(ab.EXPERIMENTS["p1-11-signin-prompt-login"]["hold_constant"])
        self.assertIn("signin_screen_hint_login_or_signup true in BOTH arms", held)


class SigninLocaleExperimentTests(unittest.TestCase):
    """P1-12 判定规则：signin 的 locale 对照（SunnyRegister 形状）。"""

    def test_favor_locale_when_rate_improves_without_auth_state_growth(self):
        rows = [
            _record("p1-12-signin-locale", "default", False, attempted=40, registered=6),
            _record("p1-12-signin-locale", "locale", True, attempted=40, registered=18),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "favor_locale")
        self.assertTrue(report["powered"])

    def test_auth_state_growth_keeps_the_default_off(self):
        rows = [
            _record("p1-12-signin-locale", "default", False, attempted=40, registered=10, failures={"auth_state": 2}),
            _record("p1-12-signin-locale", "locale", True, attempted=40, registered=14, failures={"auth_state": 9}),
        ]
        report = ab.compare_records(rows)
        self.assertEqual(report["verdict"], "keep_default_off")

    def test_within_delta_is_inconclusive(self):
        rows = [
            _record("p1-12-signin-locale", "default", False, attempted=40, registered=10),
            _record("p1-12-signin-locale", "locale", True, attempted=40, registered=11),
        ]
        self.assertEqual(ab.compare_records(rows)["verdict"], "inconclusive")

    def test_hold_constant_pins_sunnyregisters_shape(self):
        """SunnyRegister 把 locale 与 ``prompt=login`` + ``screen_hint=signup`` 配对。"""
        held = " ".join(ab.EXPERIMENTS["p1-12-signin-locale"]["hold_constant"])
        self.assertIn("signin_prompt_login true in BOTH arms", held)
        self.assertIn("signin_screen_hint_login_or_signup false in BOTH arms", held)


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


_REPLAY_LOG = ROOT / "runtime" / "tmp" / "reg_in" / "run_p15_hint.log"


@unittest.skipUnless(_REPLAY_LOG.exists(), "live replay log not present (runtime/tmp is gitignored)")
class P15HintReplayTests(unittest.TestCase):
    """Replay the 2026-10-07 P1-5 hint arm through ``build_record``.

    The recorded artifact ``runtime/ab/p1-5-signup-continue-screen-hint__hint.json``
    predates the mechanism check, so this re-reads the raw run log rather than
    the record.  That is the assertion the scan asks for: the arm's 5/5
    ``email_otp_send_stuck`` is a **failed manipulation**, not a null result for
    the ``screen_hint`` hypothesis.
    """

    def test_the_hint_arm_is_a_failed_manipulation_not_underpowered(self):
        log_text = _REPLAY_LOG.read_text(encoding="utf-8", errors="replace")
        hint = ab.build_record(
            experiment="p1-5-signup-continue-screen-hint",
            arm="hint",
            log_text=log_text,
            funnel=None,
            toggles={"registration.signup_continue_screen_hint": True},
        )
        self.assertTrue(hint["toggle_verified"])
        self.assertFalse(hint["mechanism_seen"])
        self.assertFalse(hint["mechanism_ok"])

        default = ab.build_record(
            experiment="p1-5-signup-continue-screen-hint",
            arm="default",
            log_text=log_text,
            funnel=None,
            toggles={"registration.signup_continue_screen_hint": False},
        )
        self.assertTrue(default["mechanism_ok"])

        report = ab.compare_records([default, hint])
        self.assertEqual(report["verdict"], "manipulation_failed")
        self.assertNotEqual(report["verdict"], "underpowered")


if __name__ == "__main__":
    unittest.main()
