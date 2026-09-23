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
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl",
                   "config_unread_keys")}
        report = doctor.run_doctor(
            {
                "proxy": {"default": "http://p:1", "pool": []},
                "email_registration": {"remail": {"enabled": True, "api_key": "rk-test"}},
                # A literal key, so the phone-provider check does not depend on
                # whatever the developer happens to have exported.
                "phone_reuse": {"source": "smsbower", "smsbower": {"api_key": "k"}},
            },
            "F:/repo/config.json",
            probes=probes,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["warned"], 0)

    def test_missing_required_dependency_fails(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl",
                   "config_unread_keys")}
        probes["node"] = lambda: doctor._check("node", "fail", "not found", "install node")
        report = doctor.run_doctor({}, "", probes=probes)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)

    def test_bundled_fallback_config_is_flagged(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl",
                   "config_unread_keys")}
        bundled = doctor.__file__.replace("doctor.py", "config.json")
        report = doctor.run_doctor({}, bundled, probes=probes)
        source_check = next(item for item in report["checks"] if item["name"] == "config_source")
        self.assertEqual(source_check["status"], "warn")
        self.assertGreaterEqual(report["warned"], 1)

    def test_missing_proxy_and_mailbox_are_warnings_not_failures(self):
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl",
                   "config_unread_keys")}
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


class PhoneProviderCheckTests(unittest.TestCase):
    """`phone_providers`: which SMS vendors actually have a usable key.

    Warns rather than fails -- a machine set up for mailbox-only work has no
    reason to own an SMS key, and a check that fails there would be turned off
    rather than fixed.
    """

    def test_the_selected_provider_having_a_key_is_ok(self):
        check = doctor._check_phone_providers(
            {"phone_reuse": {"source": "herosms", "herosms": {"api_key": "k"}}}
        )
        self.assertEqual(check["status"], "ok")
        self.assertIn("selected=herosms", check["detail"])
        self.assertIn("origin=config", check["detail"])

    def test_the_selected_provider_missing_a_key_warns_with_the_fix(self):
        with patch.dict("os.environ", {}, clear=True):
            check = doctor._check_phone_providers(
                {"phone_reuse": {"source": "nexsms", "smsbower": {"api_key": "k"}}}
            )
        self.assertEqual(check["status"], "warn")
        self.assertIn("selected=nexsms has no key", check["detail"])
        self.assertIn("phone_reuse.nexsms.api_key", check["hint"])
        self.assertIn("NEXSMS_API_KEY", check["hint"])

    def test_the_other_providers_being_unconfigured_is_not_a_second_warning(self):
        """One warning for the provider that blocks the run, not four."""
        with patch.dict("os.environ", {}, clear=True):
            check = doctor._check_phone_providers(
                {"phone_reuse": {"source": "herosms", "herosms": {"api_key": "k"}}}
            )
        self.assertEqual(check["status"], "ok")
        # All four are still named -- that is the question asked right after.
        for provider in ("smsbower", "herosms", "grizzly", "nexsms"):
            self.assertIn(provider, check["detail"])

    def test_an_unset_placeholder_is_not_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            check = doctor._check_phone_providers(
                {"phone_reuse": {"source": "grizzly", "grizzly": {"api_key": "$GRIZZLY_API_KEY"}}}
            )
        self.assertEqual(check["status"], "warn")
        self.assertIn("selected=grizzly has no key", check["detail"])

    def test_an_empty_config_warns_about_the_default_provider(self):
        with patch.dict("os.environ", {}, clear=True):
            check = doctor._check_phone_providers({})
        self.assertEqual(check["status"], "warn")
        self.assertIn("selected=smsbower has no key", check["detail"])

    def test_the_report_carries_the_structured_rows_as_well(self):
        """The desktop probe wants rows, not a string to re-parse."""
        probes = {name: _ok(name) for name in
                  ("python", "node", "playwright", "curl_cffi", "requests", "pyotp", "qrcode", "nacl",
                   "config_unread_keys")}
        report = doctor.run_doctor(
            {"phone_reuse": {"source": "herosms", "herosms": {"api_key": "k"}}},
            "F:/repo/config.json",
            probes=probes,
        )
        rows = {row["provider"]: row for row in report["phone_providers"]}
        self.assertTrue(rows["herosms"]["configured"])
        self.assertEqual(rows["herosms"]["endpoint"], "https://hero-sms.com/stubs/handler_api.php")
        check = next(item for item in report["checks"] if item["name"] == "phone_providers")
        self.assertEqual(check["status"], "ok")


if __name__ == "__main__":
    unittest.main()
