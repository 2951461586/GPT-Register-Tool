import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import gopay_payment


class GoPayPaymentTests(unittest.TestCase):
    def test_link_mode_generates_and_persists_gopay_link(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({"email": "buyer@example.com", "access_token": "at_test", "session_token": "st_test"}),
            "json_path": "",
        }

        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(
                gopay_payment,
                "generate_payment_link",
                return_value={"ok": True, "url": "https://app.midtrans.com/snap/v4/redirection/snap123"},
            ) as gen:
                with patch.object(gopay_payment, "upsert_account", return_value=True) as upsert:
                    with patch.object(gopay_payment.webbrowser, "open") as opened:
                        result = gopay_payment.one_click_pay(
                            "buyer@example.com",
                            cfg={"gopay": {"one_click_mode": "link", "open_link": True}},
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_method"], "gopay")
        gen.assert_called_once_with("at_test", proxy=None, payment_method="gopay")
        opened.assert_called_once_with("https://app.midtrans.com/snap/v4/redirection/snap123")
        saved = upsert.call_args.args[0]
        self.assertEqual(saved["payment_method"], "gopay")
        self.assertEqual(saved["paypal_status"], "link_ready")

    def test_provider_mode_stores_otp_required_state(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({"email": "buyer@example.com", "access_token": "at_test", "session_token": "st_test"}),
            "json_path": "",
        }
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body, cfg))
            return {"success": True, "flowId": "flow123", "otpRequired": True, "issuedAfterUnix": 123}

        cfg = {"gopay": {"one_click_mode": "provider", "payment_service_addr": "127.0.0.1:50054", "phone": "81234567890"}}
        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", side_effect=fake_call):
                with patch.object(gopay_payment, "upsert_account", return_value=True) as upsert:
                    result = gopay_payment.one_click_pay("buyer@example.com", cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_status"], "otp_required")
        self.assertEqual(result["flow_id"], "flow123")
        self.assertEqual(calls[0][0], "StartGoPay")
        self.assertEqual(calls[0][1]["credential"]["session_token"], "st_test")
        self.assertEqual(calls[0][1]["credential"]["access_token"], "at_test")
        self.assertEqual(calls[0][1]["gopay_phone"], "81234567890")
        self.assertEqual(calls[0][1]["gopay_country_code"], "62")
        saved = upsert.call_args.args[0]
        self.assertEqual(saved["paypal"]["flow_id"], "flow123")
        self.assertEqual(saved["paypal_status"], "otp_required")

    def test_gopay_defaults_to_protocol_mode(self):
        self.assertEqual(gopay_payment._one_click_mode({}), "provider")

    def test_smsbower_protocol_mode_auto_completes_without_static_phone(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({"email": "buyer@example.com", "access_token": "at_test", "session_token": "st_test"}),
            "json_path": "",
        }
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body, cfg))
            if method == "StartGoPay":
                return {"success": True, "flowId": "flow_smsbower", "gopayPhone": "+6281234567890"}
            return {"success": True, "chargeRef": "A123ID", "snapToken": "snap123"}

        cfg = {
            "gopay": {
                "one_click_mode": "protocol",
                "payment_service_addr": "127.0.0.1:50054",
                "otp_source": "smsbower",
                "otp": {"source": "smsbower"},
            }
        }
        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", side_effect=fake_call):
                with patch.object(gopay_payment, "upsert_account", return_value=True):
                    result = gopay_payment.one_click_pay("buyer@example.com", cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_status"], "completed")
        self.assertEqual(calls[0][0], "StartGoPay")
        self.assertEqual(calls[0][1]["gopay_phone"], "")
        self.assertEqual(calls[1][0], "CompleteGoPay")
        self.assertEqual(calls[1][1]["otp"], "")

    def test_provider_error_classifies_sms_otp_timeout_as_new_number(self):
        result = gopay_payment._provider_error(
            "buyer@example.com",
            "complete_gopay_failed",
            {"errorMessage": "SMSBower OTP timeout after 240s"},
        )

        self.assertEqual(result["gopay_failure_class"], "otp_timeout")
        self.assertEqual(result["recommended_action"], "change_number")
        self.assertEqual(result["retry_policy"], "retry_with_new_smsbower_number")

    def test_provider_error_classifies_waf_as_new_proxy(self):
        result = gopay_payment._provider_error(
            "buyer@example.com",
            "start_gopay_failed",
            {"errorMessage": "customer_signup failed: HTTP 403 WAF Block Page"},
        )

        self.assertEqual(result["gopay_failure_class"], "proxy_blocked")
        self.assertEqual(result["recommended_action"], "change_proxy")

    def test_provider_error_classifies_fraud_deny_as_terminal(self):
        result = gopay_payment._provider_error(
            "buyer@example.com",
            "complete_gopay_failed",
            {"errorMessage": "midtrans fraud denied: {\"fraud_status\":\"deny\"}"},
        )

        self.assertEqual(result["gopay_failure_class"], "terminal_fraud_deny")
        self.assertEqual(result["recommended_action"], "terminal_fail")

    def test_provider_mode_completes_with_otp_and_pin(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({"email": "buyer@example.com", "access_token": "at_test", "session_token": "st_test"}),
            "json_path": "",
        }
        responses = [
            {"success": True, "flowId": "flow123", "otpRequired": True},
            {"success": True, "chargeRef": "A123ID", "snapToken": "snap123"},
        ]
        args = SimpleNamespace(
            gopay_otp="123456",
            gopay_flow_id=None,
            gopay_pin="123456",
            gopay_phone=None,
            gopay_country_code=None,
            gopay_otp_channel=None,
            gopay_wa_phone=None,
        )
        cfg = {"gopay": {"one_click_mode": "provider", "payment_service_addr": "127.0.0.1:50054", "phone": "81234567890"}}

        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", side_effect=responses) as call:
                with patch.object(gopay_payment, "upsert_account", return_value=True) as upsert:
                    result = gopay_payment.one_click_pay("buyer@example.com", args=args, cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_status"], "completed")
        self.assertEqual(result["charge_ref"], "A123ID")
        self.assertEqual(call.call_args_list[1].args[0], "CompleteGoPay")
        self.assertEqual(call.call_args_list[1].args[1], {"flow_id": "flow123", "otp": "123456", "pin": "123456"})
        saved = upsert.call_args.args[0]
        self.assertEqual(saved["paypal_status"], "completed")

    def test_provider_mode_completes_saved_flow_without_starting_new_one(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({
                "email": "buyer@example.com",
                "access_token": "at_test",
                "session_token": "st_test",
                "paypal": {"flow_id": "flow_saved"},
            }),
            "json_path": "",
        }
        args = SimpleNamespace(
            gopay_otp="123456",
            gopay_flow_id=None,
            gopay_pin="123456",
            gopay_phone=None,
            gopay_country_code=None,
            gopay_otp_channel=None,
            gopay_wa_phone=None,
        )
        cfg = {"gopay": {"one_click_mode": "provider", "payment_service_addr": "127.0.0.1:50054", "phone": "81234567890"}}

        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", return_value={"success": True, "chargeRef": "A123ID"}) as call:
                with patch.object(gopay_payment, "upsert_account", return_value=True):
                    result = gopay_payment.one_click_pay("buyer@example.com", args=args, cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "CompleteGoPay")
        self.assertEqual(call.call_args.args[1], {"flow_id": "flow_saved", "otp": "123456", "pin": "123456"})

    def test_provider_mode_reads_session_token_from_cookie_header(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({
                "email": "buyer@example.com",
                "access_token": "at_test",
                "cookie_header": "foo=bar; __Secure-next-auth.session-token=st_cookie; other=1",
            }),
            "json_path": "",
        }
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body, cfg))
            return {"success": True, "flowId": "flow_cookie"}

        cfg = {"gopay": {"one_click_mode": "provider", "payment_service_addr": "127.0.0.1:50054", "phone": "81234567890"}}
        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", side_effect=fake_call):
                with patch.object(gopay_payment, "upsert_account", return_value=True):
                    result = gopay_payment.one_click_pay("buyer@example.com", cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][1]["credential"]["session_token"], "st_cookie")

    def test_wa_rebind_mode_uses_wa_phone_and_channel(self):
        row = {
            "email": "buyer@example.com",
            "access_token": "at_test",
            "raw_json": json.dumps({"email": "buyer@example.com", "access_token": "at_test", "session_token": "st_test"}),
            "json_path": "",
        }
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body, cfg))
            return {"success": True, "flowId": "flow_wa", "otpRequired": True}

        cfg = {
            "gopay": {
                "one_click_mode": "wa_rebind",
                "payment_service_addr": "127.0.0.1:50054",
                "phone": "81234567890",
                "wa_rebind": {"enabled": True, "wa_phone": "85900000001"},
            }
        }
        with patch.object(gopay_payment, "_account_row", return_value=row):
            with patch.object(gopay_payment, "_call_payment_service", side_effect=fake_call):
                with patch.object(gopay_payment, "upsert_account", return_value=True):
                    result = gopay_payment.one_click_pay("buyer@example.com", cfg=cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "StartGoPay")
        self.assertEqual(calls[0][1]["gopay_phone"], "85900000001")
        self.assertEqual(calls[0][1]["otp_channel"], "wa")


if __name__ == "__main__":
    unittest.main()
