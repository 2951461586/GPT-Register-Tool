import os
import sqlite3
import tempfile
import time
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import paypal_links, paypal_nocard


class PayPalNoCardUnitTests(unittest.TestCase):
    def test_extract_ba_and_ec_tokens(self):
        self.assertEqual(
            paypal_nocard.extract_ba_token("https://www.paypal.com/agreements/approve?ba_token=BA-123_ABC-def"),
            "BA-123_ABC-def",
        )
        self.assertEqual(
            paypal_nocard.extract_ec_token("<input value='EC-1ABCD234EFGH56789'>"),
            "EC-1ABCD234EFGH56789",
        )
        self.assertIsNone(paypal_nocard.extract_ba_token("https://pm-redirects.stripe.com/authorize/test"))
        self.assertIsNone(paypal_nocard.extract_ec_token("missing"))

    def test_extract_paypal_approve_url_from_body_or_bare_ba_token(self):
        self.assertEqual(
            paypal_nocard._extract_paypal_approve_url(
                r'{"url":"https:\/\/www.paypal.com\/agreements\/approve?ba_token=BA-BODY_123.456-789\u0026x=1"}'
            ),
            "https://www.paypal.com/agreements/approve?ba_token=BA-BODY_123.456-789&x=1",
        )
        self.assertEqual(
            paypal_nocard._extract_paypal_approve_url("next=paypal&ba_token=BA-ONLY_123.456-789"),
            "https://www.paypal.com/agreements/approve?ba_token=BA-ONLY_123.456-789",
        )

    def test_follow_stripe_redirect_reads_location_then_body(self):
        class FakeSession:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)
                if len(self.urls) == 1:
                    return SimpleNamespace(
                        status_code=302,
                        headers={"Location": "https://pm-redirects.stripe.com/authorize/next"},
                        text="",
                        url=url,
                    )
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text=r'{"url":"https:\/\/www.paypal.com\/agreements\/approve?ba_token=BA-FOLLOW_123.456-789\u0026x=1"}',
                    url=url,
                )

        fake = FakeSession()
        logs = []
        with patch.object(paypal_nocard, "_make_session", return_value=fake):
            resolved = paypal_nocard._follow_stripe_redirect(
                "https://pm-redirects.stripe.com/authorize/start",
                proxy="socks5h://127.0.0.1:7897",
                log=logs.append,
            )

        self.assertEqual(
            resolved,
            "https://www.paypal.com/agreements/approve?ba_token=BA-FOLLOW_123.456-789&x=1",
        )
        self.assertEqual(fake.urls[-1], "https://pm-redirects.stripe.com/authorize/next")
        self.assertTrue(any("location=" in entry for entry in logs))
        self.assertTrue(any("body=" in entry for entry in logs))

    def test_phone_split_handles_us_local_and_e164(self):
        self.assertEqual(paypal_nocard._phone_split("4482162932"), ("1", "4482162932"))
        self.assertEqual(paypal_nocard._phone_split("+14482162932"), ("1", "4482162932"))
        self.assertEqual(paypal_nocard._phone_split("+447911123456"), ("44", "7911123456"))

    def test_round_robin_pools_are_persisted(self):
        cfg = {
            "paypal_auto": {
                "cards": [
                    {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"},
                    {"number": "5555555555554444", "exp_month": "02", "exp_year": "2031", "cvv": "456"},
                ],
            },
            "paypal_nocard": {
                "card_index_file": "runtime/card_idx.txt",
                "phone_index_file": "runtime/phone_idx.txt",
                "phone_pool": [
                    {"phone": "+14482162932", "sms_api_url": "https://sms.example/a"},
                    {"phone": "+14482162933", "sms_api_url": "https://sms.example/b"},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paypal_nocard, "PROJECT_ROOT", tmp):
                self.assertEqual(paypal_nocard.get_next_card(cfg)["number"], "4111111111111111")
                self.assertEqual(paypal_nocard.get_next_card(cfg)["number"], "5555555555554444")
                self.assertEqual(paypal_nocard.get_next_phone(cfg)["phone"], "+14482162932")
                self.assertEqual(paypal_nocard.get_next_phone(cfg)["phone"], "+14482162933")

                self.assertEqual((Path(tmp) / "runtime" / "card_idx.txt").read_text().strip(), "0")
                self.assertEqual((Path(tmp) / "runtime" / "phone_idx.txt").read_text().strip(), "0")

    def test_one_click_pay_regenerates_fresh_url_by_default(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-SAVED123456789"
        fresh_url = "https://www.paypal.com/agreements/approve?ba_token=BA-FRESH123456789"
        signup_result = paypal_nocard.SignupResult(
            success=True,
            return_url="https://www.paypal.com/checkoutnow/return",
            ba_token="BA-FRESH123456789",
            user_id="user_123",
            ec_token="EC-1ABCD234EFGH56789",
        )

        with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "url": fresh_url, "cs_id": "cs_test"}) as gen:
            with patch.object(paypal_nocard, "signup_no_card", return_value=signup_result) as signup:
                with patch("builtins.print"):
                    result = paypal_nocard.one_click_pay(
                        "at_test",
                        card=card,
                        phone=phone,
                        proxy="socks5h://127.0.0.1:7897",
                        cfg={"paypal_nocard": {"locale_country": "US", "locale_lang": "en", "otp_timeout": 30}},
                        paypal_url=saved_url,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["return_url"], "https://www.paypal.com/checkoutnow/return")
        self.assertEqual(result["paypal_url"], fresh_url)
        self.assertEqual(result["link_info"]["cs_id"], "cs_test")
        gen.assert_called_once_with("at_test", proxy="socks5h://127.0.0.1:7897")
        self.assertEqual(signup.call_args.args[0], "BA-FRESH123456789")
        self.assertEqual(signup.call_args.kwargs["proxy"], "socks5h://127.0.0.1:7897")
        self.assertEqual(signup.call_args.kwargs["card"], card)
        self.assertEqual(signup.call_args.kwargs["phone_e164"], "+14482162932")

    def test_one_click_pay_can_explicitly_reuse_saved_paypal_url(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-SAVED123456789"
        signup_result = paypal_nocard.SignupResult(
            success=True,
            return_url="https://www.paypal.com/checkoutnow/return",
            ba_token="BA-SAVED123456789",
            user_id="user_123",
            ec_token="EC-1ABCD234EFGH56789",
        )

        with patch("sms_tool.gen_pp_link.generate_pp_link") as gen:
            with patch.object(paypal_nocard, "signup_no_card", return_value=signup_result) as signup:
                with patch("builtins.print"):
                    result = paypal_nocard.one_click_pay(
                        "at_test",
                        card=card,
                        phone=phone,
                        proxy="socks5h://127.0.0.1:7897",
                        cfg={
                            "paypal_nocard": {
                                "locale_country": "US",
                                "locale_lang": "en",
                                "otp_timeout": 30,
                                "reuse_saved_url": True,
                            }
                        },
                        paypal_url=saved_url,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], saved_url)
        self.assertNotIn("link_info", result)
        gen.assert_not_called()
        self.assertEqual(signup.call_args.args[0], "BA-SAVED123456789")

    def test_one_click_pay_reuses_recent_link_ready_url(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-READY123456789"
        signup_result = paypal_nocard.SignupResult(
            success=True,
            return_url="https://www.paypal.com/checkoutnow/return",
            ba_token="BA-READY123456789",
            user_id="user_123",
            ec_token="EC-1ABCD234EFGH56789",
        )

        with patch("sms_tool.gen_pp_link.generate_pp_link") as gen:
            with patch.object(paypal_nocard, "signup_no_card", return_value=signup_result) as signup:
                with patch("builtins.print"):
                    result = paypal_nocard.one_click_pay(
                        "at_test",
                        card=card,
                        phone=phone,
                        proxy="socks5h://127.0.0.1:7897",
                        cfg={"paypal_nocard": {"locale_country": "US", "locale_lang": "en", "otp_timeout": 30}},
                        paypal_url=saved_url,
                        paypal_status="link_ready",
                        paypal_updated_at=int(time.time()) - 30,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], saved_url)
        gen.assert_not_called()
        self.assertEqual(signup.call_args.args[0], "BA-READY123456789")

    def test_one_click_pay_can_fallback_to_saved_paypal_url(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-SAVED123456789"
        signup_result = paypal_nocard.SignupResult(
            success=True,
            return_url="https://www.paypal.com/checkoutnow/return",
            ba_token="BA-SAVED123456789",
            user_id="user_123",
            ec_token="EC-1ABCD234EFGH56789",
        )

        with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": False, "error": "stripe refused"}) as gen:
            with patch.object(paypal_nocard, "signup_no_card", return_value=signup_result) as signup:
                with patch("builtins.print"):
                    result = paypal_nocard.one_click_pay(
                        "at_test",
                        card=card,
                        phone=phone,
                        proxy="socks5h://127.0.0.1:7897",
                        cfg={
                            "paypal_nocard": {
                                "locale_country": "US",
                                "locale_lang": "en",
                                "otp_timeout": 30,
                                "fallback_to_saved_url": True,
                            }
                        },
                        paypal_url=saved_url,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], saved_url)
        gen.assert_called_once_with("at_test", proxy="socks5h://127.0.0.1:7897")
        self.assertEqual(signup.call_args.args[0], "BA-SAVED123456789")

    def test_one_click_pay_returns_error_when_signup_raises(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-READY123456789"

        with patch("sms_tool.gen_pp_link.generate_pp_link") as gen:
            with patch.object(paypal_nocard, "signup_no_card", side_effect=RuntimeError("PayPal DataDome 拦截 (需要代理或换 IP)")):
                with patch("builtins.print"):
                    result = paypal_nocard.one_click_pay(
                        "at_test",
                        card=card,
                        phone=phone,
                        proxy="socks5h://127.0.0.1:7897",
                        cfg={"paypal_nocard": {"locale_country": "US", "locale_lang": "en", "otp_timeout": 30}},
                        paypal_url=saved_url,
                        paypal_status="link_ready",
                        paypal_updated_at=int(time.time()) - 30,
                    )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PAYPAL_DATADOME_BLOCKED")
        self.assertIn("DataDome", result["error"])
        gen.assert_not_called()

    def test_one_click_pay_regenerates_when_saved_link_no_longer_yields_ba(self):
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}
        saved_url = "https://pm-redirects.stripe.com/authorize/old"
        fresh_url = "https://pm-redirects.stripe.com/authorize/fresh"
        fresh_ba_url = "https://www.paypal.com/agreements/approve?ba_token=BA-FRESH123456789"
        signup_result = paypal_nocard.SignupResult(
            success=True,
            return_url="https://www.paypal.com/checkoutnow/return",
            ba_token="BA-FRESH123456789",
            user_id="user_123",
            ec_token="EC-1ABCD234EFGH56789",
        )

        with patch.object(
            paypal_nocard,
            "_follow_stripe_redirect",
            side_effect=["https://checkout.stripe.com/c/pay/cs_test?redirect_status=pending", fresh_ba_url],
        ) as follow:
            with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "url": fresh_url, "cs_id": "cs_test"}) as gen:
                with patch.object(paypal_nocard, "signup_no_card", return_value=signup_result) as signup:
                    with patch("builtins.print"):
                        result = paypal_nocard.one_click_pay(
                            "at_test",
                            card=card,
                            phone=phone,
                            proxy="socks5h://127.0.0.1:7897",
                            cfg={"paypal_nocard": {"locale_country": "US", "locale_lang": "en", "otp_timeout": 30}},
                            paypal_url=saved_url,
                            paypal_status="link_ready",
                            paypal_updated_at=int(time.time()) - 30,
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], fresh_ba_url)
        self.assertEqual(result["link_info"]["cs_id"], "cs_test")
        self.assertEqual(follow.call_count, 2)
        gen.assert_called_once_with("at_test", proxy="socks5h://127.0.0.1:7897")
        self.assertEqual(signup.call_args.args[0], "BA-FRESH123456789")

    def test_one_click_pay_batch_passes_saved_paypal_url_as_candidate(self):
        args = SimpleNamespace(email="paid@example.com", email_file="", proxy="socks5h://127.0.0.1:7897")
        cfg = {"paypal_nocard": {"enabled": True}}
        card = {"number": "4111111111111111", "exp_month": "01", "exp_year": "2030", "cvv": "123"}
        phone = {"phone": "+14482162932", "sms_api_url": "https://sms.example/code"}

        with patch.object(paypal_nocard, "_load_config", return_value=cfg):
            with patch.object(paypal_nocard, "get_next_card", return_value=card):
                with patch.object(paypal_nocard, "get_next_phone", return_value=phone):
                    with patch.object(paypal_nocard, "_get_access_token", return_value="at_test"):
                        with patch.object(paypal_nocard, "one_click_pay", return_value={"ok": True}) as pay:
                            with patch(
                                "sms_tool.storage.list_paypal_accounts",
                                return_value=[
                                    {
                                        "email": "paid@example.com",
                                        "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST123456789",
                                        "paypal_status": "link_ready",
                                        "paypal_updated_at": 123456,
                                        "updated_at": 123456,
                                    }
                                ],
                            ):
                                with patch("sms_tool.storage.mark_paypal_status", return_value=True) as mark:
                                    with patch("builtins.print"):
                                        paypal_nocard.one_click_pay_batch(args)

        self.assertEqual(pay.call_count, 1)
        self.assertEqual(
            pay.call_args.kwargs["paypal_url"],
            "https://www.paypal.com/agreements/approve?ba_token=BA-TEST123456789",
        )
        self.assertEqual(pay.call_args.kwargs["paypal_status"], "link_ready")
        self.assertEqual(pay.call_args.kwargs["paypal_updated_at"], 123456)
        mark.assert_called_once_with("paid@example.com", "completed")

    def test_regenerate_paypal_link_stores_resolved_ba_url(self):
        original_url = "https://pm-redirects.stripe.com/authorize/sa_nonce_test"
        resolved_url = "https://www.paypal.com/agreements/approve?ba_token=BA-RESOLVED123456789"
        seed = {"email": "paid@example.com", "access_token": "at_test", "success": True}
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value={"ok": True, "url": original_url, "cs_id": "cs_test"}):
                with patch.object(paypal_links, "_follow_stripe_redirect", return_value=resolved_url) as follow:
                    with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                        result = paypal_links.regenerate_paypal_link(
                            email="paid@example.com",
                            proxy="socks5h://127.0.0.1:7897",
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], resolved_url)
        self.assertEqual(saved["paypal"]["url"], resolved_url)
        self.assertEqual(saved["paypal"]["stripe_redirect_url"], original_url)
        self.assertTrue(saved["paypal"]["ba_resolved"])
        follow.assert_called_once()

    def test_regenerate_paypal_link_refreshes_session_after_checkout_401(self):
        seed = {"email": "paid@example.com", "access_token": "old_at", "cookie_header": "cookie", "success": True}
        refreshed_seed = {"email": "paid@example.com", "access_token": "new_at", "cookie_header": "cookie", "success": True}
        saved = {}

        def fake_load_seed(email="", session_file=""):
            if fake_load_seed.calls == 0:
                fake_load_seed.calls += 1
                return seed, "session.json"
            return refreshed_seed, "session.json"

        fake_load_seed.calls = 0

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", side_effect=fake_load_seed):
            with patch.object(paypal_links, "generate_pp_link", side_effect=[
                {"ok": False, "error": "checkout unauthorized: 401", "error_code": "checkout_unauthorized"},
                {"ok": True, "url": "https://www.paypal.com/agreements/approve?ba_token=BA-NEW123456789"},
            ]) as gen:
                with patch.object(paypal_links, "_refresh_seed_session", return_value={"ok": True}):
                    with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                        result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual([call.args[0] for call in gen.call_args_list], ["old_at", "new_at"])
        self.assertEqual(saved["access_token"], "new_at")

    def test_refresh_seed_session_falls_back_when_cookie_refresh_returns_same_token(self):
        with patch("sms_tool.session_refresh.refresh_session", return_value={"ok": True, "mode": "protocol"}):
            with patch.object(paypal_links, "_load_seed", return_value=({"access_token": "old_at"}, "session.json")):
                with patch.object(
                    paypal_links,
                    "_try_oauth_refresh_token",
                    return_value={"ok": False, "error": "no_oauth_refresh_token"},
                ) as oauth:
                    with patch.object(
                        paypal_links,
                        "_try_passwordless_oauth_login",
                        return_value={"ok": False, "error": "passwordless_missing_mailbox"},
                    ) as passwordless:
                        result = paypal_links._refresh_seed_session(
                            "paid@example.com",
                            "session.json",
                            stale_access_token="old_at",
                        )

        self.assertFalse(result["ok"])
        self.assertIn("cookie_refresh_returned_same_access_token", result["error"])
        self.assertIn("oauth_fallback=no_oauth_refresh_token", result["error"])
        self.assertIn("passwordless_fallback=passwordless_missing_mailbox", result["error"])
        oauth.assert_called_once()
        passwordless.assert_called_once()

    def test_refresh_seed_session_uses_passwordless_login_after_expired_oauth_refresh(self):
        load_results = [
            ({"access_token": "old_at"}, "session.json"),
            ({"access_token": "new_at"}, "session.json"),
        ]

        with patch("sms_tool.session_refresh.refresh_session", return_value={"ok": True, "mode": "protocol"}):
            with patch.object(paypal_links, "_load_seed", side_effect=load_results):
                with patch.object(
                    paypal_links,
                    "_try_oauth_refresh_token",
                    return_value={"ok": False, "error": "oauth_refresh_http_401: token_expired"},
                ):
                    with patch.object(
                        paypal_links,
                        "_try_passwordless_oauth_login",
                        return_value={"ok": True, "mode": "passwordless_email_otp_login"},
                    ) as passwordless:
                        result = paypal_links._refresh_seed_session(
                            "paid@example.com",
                            "session.json",
                            stale_access_token="old_at",
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "passwordless_email_otp_login")
        passwordless.assert_called_once()

    def test_regenerate_gopay_failure_does_not_reuse_old_paypal_link(self):
        old_url = "https://www.paypal.com/agreements/approve?ba_token=BA-OLD123456789"
        seed = {
            "email": "paid@example.com",
            "access_token": "at_test",
            "success": True,
            "payment_method": "gopay",
            "paypal_status": "link_ready",
            "paypal": {
                "ok": True,
                "url": old_url,
                "currency": "usd",
                "payment_method_types": ["card", "paypal"],
            },
        }
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(
                paypal_links,
                "_generate_link",
                return_value={
                    "ok": False,
                    "payment_method": "gopay",
                    "method": "gopay",
                    "error": "You cannot combine currencies on a single customer.",
                    "error_code": "checkout_failed",
                },
            ):
                with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                    result = paypal_links.regenerate_paypal_link(email="paid@example.com", payment_method="gopay")

        self.assertFalse(result["ok"])
        self.assertEqual(result["payment_method"], "gopay")
        self.assertEqual(result["paypal_status"], "failed")
        self.assertEqual(result["paypal_url"], "")
        self.assertEqual(saved["payment_method"], "gopay")
        self.assertEqual(saved["paypal_status"], "failed")
        self.assertEqual(saved["paypal"].get("url", ""), "")
        self.assertEqual(saved["previous_paypal"]["url"], old_url)

    def test_sqlite_smoke_reads_existing_paypal_url_when_enabled(self):
        if os.environ.get("PAYPAL_NOCARD_SQLITE_SMOKE") != "1":
            self.skipTest("set PAYPAL_NOCARD_SQLITE_SMOKE=1 to read the local SQLite account pool")

        db_path = Path(os.environ.get("PAYPAL_NOCARD_SQLITE_PATH", "runtime/accounts.sqlite3"))
        self.assertTrue(db_path.exists(), f"SQLite database not found: {db_path}")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT email,paypal_url FROM accounts "
                "WHERE paypal_url IS NOT NULL AND paypal_url<>'' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row, "no account with paypal_url found")
        url = str(row["paypal_url"] or "")
        self.assertTrue(url.startswith("https://"), "paypal_url must be an https URL")

        if os.environ.get("PAYPAL_NOCARD_FOLLOW_REDIRECT") == "1":
            proxy = os.environ.get("PAYPAL_NOCARD_PROXY", "socks5h://127.0.0.1:7897")
            resolved = paypal_nocard._follow_stripe_redirect(url, proxy=proxy, timeout=20)
            original_host = urllib.parse.urlparse(url).netloc.lower()
            self.assertTrue(
                paypal_nocard.extract_ba_token(resolved) or "paypal.com" in resolved or "stripe.com" in resolved,
                "resolved URL should stay in Stripe/PayPal redirect chain or contain BA token",
            )
            if "pm-redirects.stripe.com" in original_host and not paypal_nocard.extract_ba_token(url):
                self.assertNotEqual(resolved, url, "Stripe redirect smoke did not advance beyond the saved pm-redirect URL")


if __name__ == "__main__":
    unittest.main()
