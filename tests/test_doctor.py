"""Offline tests for the --doctor environment self-check."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from sms_tool import doctor


def _ok(name):
    return lambda: doctor._check(name, "ok", "stub")


class DoctorUnitTests(unittest.TestCase):
    def test_all_ok_report_is_green(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl")}
        report = doctor.run_doctor(
            {
                "proxy": {"default": "http://p:1", "pool": []},
                "email_registration": {"remail": {"enabled": True, "api_key": "rk-test"}},
            },
            "F:/repo/config.json",
            probes=probes,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["warned"], 0)

    def test_missing_required_dependency_fails(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl")}
        probes["node"] = lambda: doctor._check("node", "fail", "not found", "install node")
        report = doctor.run_doctor({}, "", probes=probes)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)

    def test_bundled_fallback_config_is_flagged(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl")}
        bundled = doctor.__file__.replace("doctor.py", "config.json")
        report = doctor.run_doctor({}, bundled, probes=probes)
        source_check = next(item for item in report["checks"] if item["name"] == "config_source")
        self.assertEqual(source_check["status"], "warn")
        self.assertGreaterEqual(report["warned"], 1)

    def test_missing_proxy_and_mailbox_are_warnings_not_failures(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl")}
        report = doctor.run_doctor(
            {"proxy": {}, "email_registration": {"token_file": "does-not-exist.txt"}},
            "F:/repo/config.json",
            probes=probes,
        )
        self.assertTrue(report["ok"])  # config gaps warn but do not block
        names = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(names["config_proxy"], "warn")
        self.assertEqual(names["config_mailbox"], "warn")

    def test_cli_json_flag_emits_report(self):
        from sms_tool import cli

        fake = {"ok": True, "failed": 0, "warned": 1, "checks": [{"name": "python", "status": "ok", "detail": "", "hint": ""}]}
        buffer = io.StringIO()
        with patch("sms_tool.doctor.run_doctor", return_value=fake):
            with patch("sys.argv", ["sms_tool", "--doctor", "--json"]):
                with contextlib.redirect_stdout(buffer):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main()
        self.assertEqual(ctx.exception.code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warned"], 1)


if __name__ == "__main__":
    unittest.main()
