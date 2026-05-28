import unittest
from types import SimpleNamespace

from sms_tool.gopay_wa_rebind import after_completed_payment


class GoPayWARebindTests(unittest.TestCase):
    def test_completed_payment_waits_for_change_phone_otp(self):
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body))
            if method == "GetGoPayState":
                return {"success": True, "stateJson": "{}"}
            if method == "AuthStart":
                return {"success": True, "ready": True, "stateJson": "{\"stage\":\"ready\"}"}
            if method == "ChangePhoneStart":
                return {"success": True, "otpSent": True, "stateJson": "{\"stage\":\"change_phone_otp_pending\"}"}
            raise AssertionError(method)

        result = after_completed_payment(
            email="buyer@example.com",
            data={},
            payment_result={"ok": True, "paypal_status": "completed", "charge_ref": "A123"},
            args=SimpleNamespace(
                gopay_wa_phone=None,
                gopay_rebind_phone=None,
                gopay_rebind_otp=None,
                gopay_auth_otp=None,
                gopay_user_id=None,
                gopay_pin=None,
                gopay_country_code=None,
            ),
            gopay_cfg={
                "pin": "123456",
                "country_code": "62",
                "wa_rebind": {
                    "enabled": True,
                    "gopay_app_service_addr": "127.0.0.1:50060",
                    "user_id": "local",
                    "wa_phone": "85900000001",
                    "rebind_phone": "85900000002",
                },
            },
            caller=fake_call,
        )

        self.assertEqual(result["paypal_status"], "completed")
        self.assertEqual(result["gopay_wa_rebind"]["status"], "wa_rebind_otp_required")
        self.assertEqual(calls[0][0], "GetGoPayState")
        self.assertEqual(calls[1][0], "AuthStart")
        self.assertEqual(calls[1][1]["otp_channel"], "wa")
        self.assertEqual(calls[2][0], "ChangePhoneStart")
        self.assertEqual(calls[2][1]["new_phone"], "85900000002")

    def test_completed_payment_completes_rebind_when_otp_present(self):
        calls = []

        def fake_call(method, body, cfg):
            calls.append((method, body))
            if method == "GetGoPayState":
                return {"success": True, "stateJson": "{}"}
            if method == "AuthStart":
                return {"success": True, "ready": True, "stateJson": "{\"stage\":\"ready\"}"}
            if method == "ChangePhoneStart":
                return {"success": True, "otpSent": True, "stateJson": "{\"stage\":\"pending\"}"}
            if method == "ChangePhoneComplete":
                return {"success": True, "stateJson": "{\"stage\":\"changed\"}"}
            if method == "UpsertGoPayState":
                return {"success": True}
            raise AssertionError(method)

        result = after_completed_payment(
            email="buyer@example.com",
            data={},
            payment_result={"ok": True, "paypal_status": "completed", "charge_ref": "A123"},
            args=SimpleNamespace(
                gopay_wa_phone=None,
                gopay_rebind_phone=None,
                gopay_rebind_otp="654321",
                gopay_auth_otp=None,
                gopay_user_id=None,
                gopay_pin=None,
                gopay_country_code=None,
            ),
            gopay_cfg={
                "pin": "123456",
                "country_code": "62",
                "wa_rebind": {
                    "enabled": True,
                    "gopay_app_service_addr": "127.0.0.1:50060",
                    "user_id": "local",
                    "wa_phone": "85900000001",
                    "rebind_phone": "85900000002",
                },
            },
            caller=fake_call,
        )

        self.assertEqual(result["gopay_wa_rebind"]["status"], "completed")
        self.assertEqual(calls[-2][0], "ChangePhoneComplete")
        self.assertEqual(calls[-2][1]["otp"], "654321")
        self.assertEqual(calls[-1][0], "UpsertGoPayState")


if __name__ == "__main__":
    unittest.main()
