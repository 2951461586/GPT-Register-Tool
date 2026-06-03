import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import paypal_auto, paypal_browser_auto


class PayPalBrowserAutoUnitTests(unittest.TestCase):
    def test_get_next_phone_prefers_browser_pool_and_rotates(self):
        cfg = {
            "paypal_browser": {
                "phone_index_file": "runtime/browser_phone_idx.txt",
                "phone_pool": [
                    {"phone": "+14482160001", "sms_api_url": "https://sms.example/a"},
                    {"phone": "+14482160002", "sms_api_url": "https://sms.example/b"},
                ],
            },
            "paypal_nocard": {
                "phone_pool": [
                    {"phone": "+19999999999", "sms_api_url": "https://sms.example/old"},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paypal_browser_auto, "PROJECT_ROOT", Path(tmp)):
                self.assertEqual(paypal_browser_auto.get_next_phone(cfg)["phone"], "+14482160001")
                self.assertEqual(paypal_browser_auto.get_next_phone(cfg)["phone"], "+14482160002")
                self.assertEqual((Path(tmp) / "runtime" / "browser_phone_idx.txt").read_text().strip(), "2")

    def test_get_next_phone_falls_back_to_paypal_nocard_pool(self):
        cfg = {
            "paypal_browser": {"phone_index_file": "runtime/browser_phone_idx.txt"},
            "paypal_nocard": {
                "phone_pool": [
                    {"phone": "+14482160003", "sms_api_url": "https://sms.example/c"},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paypal_browser_auto, "PROJECT_ROOT", Path(tmp)):
                self.assertEqual(
                    paypal_browser_auto.get_next_phone(cfg),
                    {"phone": "+14482160003", "sms_api_url": "https://sms.example/c"},
                )

    def test_resolve_paypal_url_reuses_saved_url_without_fresh_generation(self):
        now = 2000
        data = {
            "paypal": {"url": "https://paypal.example/saved"},
            "paypal_status": "link_ready",
            "paypal_updated_at": now - 10,
        }
        with patch.object(paypal_browser_auto.time, "time", return_value=now):
            result = paypal_browser_auto._resolve_paypal_url(
                "at_test",
                data=data,
                email="user@example.com",
                proxy="socks5h://127.0.0.1:7897",
                cfg={},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], "https://paypal.example/saved")

    def test_resolve_paypal_url_reuses_stale_saved_url(self):
        now = 2000
        data = {
            "paypal": {"url": "https://paypal.example/saved"},
            "paypal_status": "link_ready",
            "paypal_updated_at": now - 300,
        }
        with patch.object(paypal_browser_auto.time, "time", return_value=now):
            result = paypal_browser_auto._resolve_paypal_url(
                "at_test",
                data=data,
                email="user@example.com",
                proxy="socks5h://127.0.0.1:7897",
                cfg={},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], "https://paypal.example/saved")

    def test_resolve_paypal_url_fails_without_saved_url(self):
        with patch.object(paypal_browser_auto, "list_paypal_accounts", return_value=[]):
            result = paypal_browser_auto._resolve_paypal_url(
                "at_test",
                data={},
                email="user@example.com",
                proxy="socks5h://127.0.0.1:7897",
                cfg={},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_saved_paypal_url")

    def test_run_internal_browser_flow_uses_project_paypal_auto(self):
        calls = {}

        def fake_try_browser_pay(**kwargs):
            calls.update(kwargs)
            return {
                "ok": True,
                "access_token": "new_at",
                "oauth_refresh_token": "new_rt",
                "refresh_token_status": "oauth_present",
                "paypal_status": "completed",
            }

        with patch("sms_tool.paypal_auto._try_browser_pay", side_effect=fake_try_browser_pay):
            result = paypal_browser_auto._run_internal_browser_flow(
                {"browser_engine": "camoufox", "headless": True, "country": "US"},
                paypal_url="https://paypal.example",
                identity={"first_name": "John", "last_name": "Smith"},
                password="Secret123!",
                card={"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123", "brand": "visa"},
                billing={"line1": "1 Main St", "city": "New York", "state": "NY", "postal_code": "10001", "country": "US"},
                email="pay@example.com",
                phone={"phone": "+14482160001", "sms_api_url": "https://sms.example/a"},
                proxy="socks5h://127.0.0.1:7897",
                cookie_header="a=b",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(calls["paypal_url"], "https://paypal.example")
        self.assertEqual(calls["first_name"], "John")
        self.assertEqual(calls["alias_email"], "pay@example.com")
        self.assertEqual(calls["phone"], "+14482160001")
        self.assertEqual(calls["sms_api_url"], "https://sms.example/a")
        self.assertEqual(calls["proxy"], "socks5h://127.0.0.1:7897")
        self.assertTrue(calls["headless"])
        self.assertEqual(calls["cookie_header"], "a=b")

    def test_generated_card_is_luhn_valid_and_paypal_auto_compatible(self):
        card = paypal_browser_auto._generate_card("visa")

        self.assertEqual(card["brand"], "visa")
        self.assertEqual(len(card["number"]), 16)
        self.assertEqual(card["number"][0], "4")
        self.assertIn("exp_month", card)
        self.assertIn("exp_year", card)
        self.assertIn("cvv", card)
        self.assertTrue(_is_luhn_valid(card["number"]))

    def test_persist_browser_result_keeps_refreshed_tokens(self):
        data = {"email": "user@example.com", "success": True}
        result = {
            "ok": True,
            "paypal_url": "https://paypal.example",
            "access_token": "new_at",
            "oauth_refresh_token": "new_rt",
            "refresh_token_status": "oauth_present",
            "engine": "camoufox",
            "country": "US",
            "alias_email": "pay@example.com",
            "card_last4": "1111",
            "phone_last4": "0001",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with patch.object(paypal_browser_auto, "upsert_account") as upsert:
                paypal_browser_auto._persist_browser_result(data, str(path), result)

            saved = path.read_text(encoding="utf-8")

        self.assertIn("new_at", saved)
        self.assertEqual(data["access_token"], "new_at")
        self.assertEqual(data["oauth_refresh_token"], "new_rt")
        self.assertEqual(data["refresh_token_status"], "oauth_present")
        upsert.assert_called_once()

    def test_paypal_auto_detects_human_verification_page(self):
        page = _FakePage(
            body_text="PayPal\nConfirm you're human\nMove the slider all the way to the right"
        )

        self.assertTrue(paypal_auto._is_human_verification_page(page))

    def test_paypal_auto_human_verification_fails_fast_when_manual_disabled(self):
        page = _FakePage(
            body_text="PayPal\nConfirm you're human\nMove the slider all the way to the right"
        )

        with self.assertRaises(Exception) as ctx:
            paypal_auto._handle_human_verification_gate(
                page,
                {"manual_human_verification": False, "human_verification_timeout": 1},
                "runtime/paypal_debug",
                False,
                "human_verification",
            )

        self.assertIn("paypal_human_verification_required", str(ctx.exception))


def _is_luhn_valid(number: str) -> bool:
    total = 0
    reverse_digits = [int(ch) for ch in reversed(number)]
    for index, digit in enumerate(reverse_digits):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class _FakePage:
    def __init__(self, body_text: str = ""):
        self.body_text = body_text

    def locator(self, selector: str):
        if selector == "body":
            return _FakeLocator(self.body_text, visible=True)
        visible = any(
            marker in self.body_text
            for marker in [
                "Confirm you're human",
                "Move the slider all the way to the right",
                "Please enable JS and disable any ad blocker",
            ]
        )
        return _FakeLocator("", visible=visible)


class _FakeLocator:
    @property
    def first(self):
        return self

    def __init__(self, text: str, visible: bool):
        self._text = text
        self._visible = visible

    def is_visible(self, timeout: int = 0):
        return self._visible

    def inner_text(self, timeout: int = 0):
        return self._text


if __name__ == "__main__":
    unittest.main()
