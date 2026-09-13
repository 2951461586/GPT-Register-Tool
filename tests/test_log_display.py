"""Operator-facing log rendering: stage-based human log vs JSON machine log.

``sms_tool.log`` must read as normalized stage lines (no schema_version /
command_id / run_id envelope metadata, no raw JSON); ``sms_tool.jsonl`` keeps
the full telemetry envelope for tooling.
"""
import json
import logging
import unittest

from sms_tool.logging_setup import (
    CorrelatedJsonFormatter,
    HumanLogFormatter,
    module_label,
    stage_display,
)


def _record(message, *, name="sms_tool.registration_progress", level=logging.INFO, args=None):
    return logging.LogRecord(name, level, __file__, 0, message, args, None)


class StageDisplayTests(unittest.TestCase):
    def test_known_stage_renders_chinese_label_and_status(self):
        self.assertEqual(
            stage_display("email_otp_send", "running"),
            "阶段 · 发送邮箱验证码 (email_otp_send) — 进行中",
        )
        self.assertEqual(stage_display("completed", "success"), "阶段 · 完成 (completed) — 成功")

    def test_retry_suffix_renders_against_the_base_stage(self):
        self.assertEqual(
            stage_display("auth_flow_retry", "running"),
            "阶段 · 授权流程（重试） (auth_flow_retry) — 进行中",
        )

    def test_unknown_stage_falls_back_to_the_raw_code(self):
        self.assertEqual(stage_display("some_future_stage", "failed"), "阶段 · some_future_stage — 失败")


class ModuleLabelTests(unittest.TestCase):
    def test_known_loggers_map_to_module_labels(self):
        self.assertEqual(module_label("sms_tool.registration_progress"), "注册")
        self.assertEqual(
            module_label("sms_tool.registration_drivers.browser_flow.orchestrator"),
            "浏览器注册",
        )
        self.assertEqual(module_label("proxy_bridge"), "代理桥接")
        self.assertEqual(module_label("sms_tool.commands.one_click"), "一键接码")
        self.assertEqual(module_label("sms_tool.accounts.account_liveness"), "账号测活")
        self.assertEqual(module_label("sms_tool.accounts.account_promotion"), "优惠检测")
        self.assertEqual(module_label("py.warnings"), "告警")

    def test_most_specific_prefix_wins(self):
        self.assertEqual(module_label("sms_tool.registration"), "注册")
        self.assertEqual(module_label("sms_tool.registration_retry_guard"), "注册")

    def test_unknown_logger_uses_the_last_dotted_segment(self):
        self.assertEqual(module_label("third_party.noisy"), "noisy")


class HumanLogFormatterTests(unittest.TestCase):
    def test_stage_line_is_normalized_without_envelope_metadata(self):
        line = HumanLogFormatter().format(
            _record("Registration stage=%s status=%s", args=("user_register", "running"))
        )
        self.assertRegex(
            line,
            r"^\d{2}:\d{2}:\d{2} \[\*\] \[注册\] 阶段 · 提交注册 \(user_register\) — 进行中$",
        )
        self.assertNotIn("run_id", line)
        self.assertNotIn("schema_version", line)

    def test_level_markers(self):
        self.assertIn("[*]", HumanLogFormatter().format(_record("note")))
        self.assertIn("[!]", HumanLogFormatter().format(_record("warn", level=logging.WARNING)))
        self.assertIn("[x]", HumanLogFormatter().format(_record("boom", level=logging.ERROR)))
        self.assertIn("[.]", HumanLogFormatter().format(_record("dbg", level=logging.DEBUG)))

    def test_proxy_credentials_are_sanitized(self):
        line = HumanLogFormatter().format(
            _record("upstream http://user:pass@proxy.test:8000", name="proxy_bridge")
        )
        self.assertNotIn("user:pass", line)
        self.assertIn("[代理桥接]", line)

    def test_account_email_is_masked_in_persisted_log_text(self):
        line = HumanLogFormatter().format(
            _record("registration failed for user.name@example.com")
        )
        self.assertNotIn("user.name@example.com", line)
        self.assertIn("us***@example.com", line)

    def test_account_ref_is_appended_to_the_operator_line(self):
        """Concurrent attempts must be attributable from ``sms_tool.log`` alone.

        Before this, the human channel had *zero* occurrences of ``account_ref``
        (the desktop progress line and the JSONL envelope both carry it), so
        telling two interleaved registrations apart needed time adjacency --
        which this repo's playbook explicitly rules out.
        """
        record = _record("Registration stage=%s status=%s", args=("user_register", "running"))
        record.account_ref = "77011ced116a1194"
        self.assertRegex(
            HumanLogFormatter().format(record),
            r"^\d{2}:\d{2}:\d{2} \[\*\] \[注册\] 阶段 · 提交注册 \(user_register\)"
            r" — 进行中 · account_ref=77011ced116a1194$",
        )

    def test_records_without_an_account_ref_keep_the_plain_shape(self):
        line = HumanLogFormatter().format(
            _record("Registration stage=%s status=%s", args=("user_register", "running"))
        )
        self.assertNotIn("account_ref", line)

    def test_json_machine_envelope_keeps_full_metadata(self):
        record = _record("Registration stage=%s status=%s", args=("user_register", "running"))
        data = json.loads(CorrelatedJsonFormatter().format(record))
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("timestamp", data)
        self.assertEqual(data["message"], "Registration stage=user_register status=running")

    def test_json_machine_envelope_keeps_structured_event_fields(self):
        record = _record("stage")
        record.event = "registration_stage"
        record.stage = "auth_flow"
        record.previous_stage_duration_ms = 1250
        data = json.loads(CorrelatedJsonFormatter().format(record))
        self.assertEqual(data["event"], "registration_stage")
        self.assertEqual(data["stage"], "auth_flow")
        self.assertEqual(data["previous_stage_duration_ms"], 1250)

    def test_warnings_records_drop_the_raw_path_prefix(self):
        # captureWarnings() messages look like "<abs path>.py:339: LeakWarning: <text>"
        # plus an indented copy of the offending source line. The human log keeps
        # only the category and the warning text.
        message = (
            "F:\\epsoft\\GPT-Register-Tool\\sms_tool\\managed.py:339: LeakWarning: "
            "When using a proxy, it is heavily recommended that you pass `geoip=True`.\n"
            "  self.context = self._camoufox_ctx.__enter__()"
        )
        line = HumanLogFormatter().format(_record(message, name="py.warnings", level=logging.WARNING))
        self.assertRegex(
            line,
            r"^\d{2}:\d{2}:\d{2} \[!\] \[告警\] LeakWarning: When using a proxy.*geoip=True`\.$",
        )
        self.assertNotIn("managed.py:339", line)
        self.assertNotIn("__enter__", line)


if __name__ == "__main__":
    unittest.main()
