import os
import sqlite3
import tempfile
import time
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import paypal_links, paypal_protocol


class PayPalLinksTests(unittest.TestCase):
    def test_checkout_unauthorized_recognizes_token_401_without_checkout_word(self):
        self.assertTrue(
            paypal_links._is_checkout_unauthorized(
                {"ok": False, "error": "access_token invalid or expired (401)"}
            )
        )
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
                with patch.object(paypal_links, "CFG", {"paypal": {"resolve_ba_redirect": True}}):
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

    def test_regenerate_paypal_link_can_keep_stripe_authorize_url(self):
        original_url = "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test?useWebAuthSession=true"
        seed = {"email": "paid@example.com", "access_token": "at_test", "success": True}
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value={"ok": True, "url": original_url, "cs_id": "cs_test"}):
                with patch.object(paypal_links, "CFG", {"paypal": {"resolve_ba_redirect": False}}):
                    with patch.object(paypal_links, "_follow_stripe_redirect") as follow:
                        with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                            result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], original_url)
        self.assertEqual(saved["paypal"]["url"], original_url)
        self.assertNotIn("ba_resolved", saved["paypal"])
        follow.assert_not_called()

    def test_regenerate_paypal_link_requires_ba_token_when_configured(self):
        original_url = "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test?useWebAuthSession=true"
        seed = {"email": "paid@example.com", "access_token": "at_test", "success": True}
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value={"ok": True, "url": original_url, "cs_id": "cs_test"}):
                with patch.object(
                    paypal_links,
                    "CFG",
                    {"paypal": {"resolve_ba_redirect": True, "require_ba_token": True}},
                ):
                    with patch.object(paypal_links, "_follow_stripe_redirect", return_value="https://paypal.com/webapps/hermes") as follow:
                        with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                            result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["paypal_status"], "failed")
        self.assertEqual(saved["paypal"]["error_code"], "paypal_ba_token_missing")
        self.assertEqual(saved["paypal"]["ba_resolve_error"], "missing_ba_token")
        follow.assert_called_once()

    def test_saved_paypal_link_requires_ba_token_when_configured(self):
        self.assertFalse(
            paypal_links._saved_paypal_link_matches_target_mode(
                {"url": "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test"},
                {"link_mode": "ba_redirect", "redirect_url_format": "any", "require_ba_token": True},
            )
        )
        self.assertTrue(
            paypal_links._saved_paypal_link_matches_target_mode(
                {"url": "https://www.paypal.com/agreements/approve?ba_token=BA-RESOLVED123456789"},
                {"link_mode": "ba_redirect", "redirect_url_format": "any", "require_ba_token": True},
            )
        )

    def test_paypal_links_payment_cfg_applies_generation_type(self):
        with patch.object(paypal_links, "CFG", {"paypal": {"link_generation_type": "paypal_direct"}}):
            direct = paypal_links._payment_cfg("paypal")
        with patch.object(paypal_links, "CFG", {"paypal": {"link_generation_type": "paypal_direct_zero_due"}}):
            direct_zero = paypal_links._payment_cfg("paypal")
        with patch.object(paypal_links, "CFG", {"paypal": {"link_generation_type": "hosted_long_url"}}):
            hosted = paypal_links._payment_cfg("paypal")

        self.assertEqual(direct["link_mode"], "stripe_redirect")
        self.assertTrue(direct["resolve_ba_redirect"])
        self.assertTrue(direct["require_ba_token"])
        self.assertFalse(direct["require_zero_due"])
        self.assertEqual(direct_zero["link_mode"], "stripe_redirect")
        self.assertTrue(direct_zero["resolve_ba_redirect"])
        self.assertTrue(direct_zero["require_ba_token"])
        self.assertTrue(direct_zero["require_zero_due"])
        self.assertEqual(hosted["link_mode"], "chatgpt_checkout")
        self.assertEqual(hosted["checkout_ui_mode"], "hosted")
        self.assertFalse(hosted["resolve_ba_redirect"])
        self.assertFalse(hosted["require_ba_token"])

    def test_regenerate_paypal_link_direct_zero_does_not_reuse_old_ba_link_on_failure(self):
        old_url = "https://www.paypal.com/agreements/approve?ba_token=BA-OLD123456789"
        seed = {
            "email": "paid@example.com",
            "access_token": "at_test",
            "success": True,
            "paypal_status": "link_ready",
            "paypal": {"ok": True, "url": old_url, "payment_method": "paypal"},
        }
        failed = {
            "ok": False,
            "error": "ChatGPT checkout approve was blocked after Stripe confirm returned no redirect",
            "error_code": "checkout_approve_blocked",
            "zero_due_verified": True,
        }
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value=failed):
                with patch.object(paypal_links, "CFG", {"paypal": {"link_generation_type": "paypal_direct_zero_due"}}):
                    with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                        result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["paypal_status"], "failed")
        self.assertEqual(result["paypal_url"], "")
        self.assertEqual(saved["previous_paypal"]["url"], old_url)
        self.assertEqual(saved["paypal"]["error_code"], "checkout_approve_blocked")

    def test_regenerate_paypal_link_keeps_chatgpt_checkout_url(self):
        checkout_url = "https://chatgpt.com/checkout/openai_llc/cs_live_TEST123"
        seed = {"email": "paid@example.com", "access_token": "at_test", "success": True}
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(
                paypal_links,
                "generate_pp_link",
                return_value={
                    "ok": True,
                    "url": checkout_url,
                    "checkout_url": checkout_url,
                    "link_type": "chatgpt_checkout",
                    "cs_id": "cs_live_TEST123",
                },
            ):
                with patch.object(paypal_links, "CFG", {"paypal": {"link_mode": "chatgpt_checkout", "require_ba_token": False}}):
                    with patch.object(paypal_links, "_follow_stripe_redirect") as follow:
                        with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                            result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_url"], checkout_url)
        self.assertEqual(saved["paypal"]["url"], checkout_url)
        self.assertNotIn("ba_resolve_error", saved["paypal"])
        follow.assert_not_called()

    def test_regenerate_paypal_link_accepts_pm_created_without_url(self):
        seed = {"email": "paid@example.com", "access_token": "at_test", "success": True}
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        pm_created = {
            "ok": True,
            "url": "",
            "link_type": "pm_created",
            "status": "pm_created",
            "paypal_status": "pm_created",
            "payment_method": "paypal",
            "cs_id": "cs_live_TEST123",
            "pm_id": "pm_TESTPAYPAL",
        }

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value=pm_created):
                with patch.object(paypal_links, "CFG", {"paypal": {"require_ba_token": True}}):
                    with patch.object(paypal_links, "_follow_stripe_redirect") as follow:
                        with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                            result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["paypal_status"], "pm_created")
        self.assertEqual(result["paypal_url"], "")
        self.assertEqual(result["pm_id"], "pm_TESTPAYPAL")
        self.assertEqual(saved["paypal_status"], "pm_created")
        self.assertEqual(saved["paypal"]["pm_id"], "pm_TESTPAYPAL")
        self.assertNotIn("paypal_regenerate_error", saved)
        follow.assert_not_called()

    def test_regenerate_paypal_link_does_not_reuse_hosted_link_for_stripe_redirect_target(self):
        old_url = "https://pay.openai.com/c/pay/cs_live_OLD#fragment"
        seed = {
            "email": "paid@example.com",
            "access_token": "at_test",
            "success": True,
            "paypal_status": "link_ready",
            "paypal": {
                "ok": True,
                "url": old_url,
                "payment_method": "paypal",
                "link_type": "chatgpt_checkout",
                "link_mode": "chatgpt_checkout",
            },
        }
        saved = {}

        def fake_upsert(data, json_path=""):
            saved.update(data)
            return True

        with patch.object(paypal_links, "_load_seed", return_value=(seed, "")):
            with patch.object(paypal_links, "generate_pp_link", return_value={
                "ok": False,
                "error": "Stripe confirm did not return PayPal redirect URL",
                "error_code": "stripe_confirm_missing_redirect",
                "payment_method": "paypal",
            }):
                with patch.object(paypal_links, "CFG", {"paypal": {"link_mode": "stripe_redirect", "resolve_ba_redirect": False}}):
                    with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                        result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["paypal_status"], "failed")
        self.assertEqual(result["paypal_url"], "")
        self.assertEqual(saved["previous_paypal"]["url"], old_url)
        self.assertEqual(saved["paypal"]["error_code"], "stripe_confirm_missing_redirect")

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
                        result = paypal_links.regenerate_paypal_link(
                            email="paid@example.com",
                            checkout_proxy="http://checkout",
                            provider_proxy="http://provider",
                            approve_proxy="http://approve",
                        )

        self.assertTrue(result["ok"])
        self.assertEqual([call.args[0] for call in gen.call_args_list], ["old_at", "new_at"])
        for call in gen.call_args_list:
            self.assertEqual(call.kwargs["checkout_proxy"], "http://checkout")
            self.assertEqual(call.kwargs["provider_proxy"], "http://provider")
            self.assertEqual(call.kwargs["approve_proxy"], "http://approve")
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

    def test_regenerate_paypal_link_marks_at_invalid_when_refresh_fallbacks_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = os.path.join(tmp, "session.json")
            seed = {"email": "paid@example.com", "access_token": "old_at", "success": True}
            saved = {}

            def fake_upsert(data, json_path=""):
                saved.update(data)
                return True

            with patch.object(paypal_links, "_load_seed", return_value=(seed, session_path)):
                with patch.object(paypal_links, "generate_pp_link", return_value={
                    "ok": False,
                    "error": "checkout unauthorized: 401 token_invalidated",
                    "error_code": "checkout_unauthorized",
                }):
                    with patch.object(paypal_links, "_refresh_seed_session", return_value={
                        "ok": False,
                        "error": "cookie_refresh_returned_same_access_token; oauth_fallback=oauth_refresh_http_401: token_expired; passwordless_fallback=add_phone_required",
                    }):
                        with patch.object(paypal_links, "upsert_account", side_effect=fake_upsert):
                            result = paypal_links.regenerate_paypal_link(email="paid@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(saved["status"], "at_invalid")
        self.assertIn("add_phone_required", saved["error"])

    def test_saved_upi_link_matches_upi_but_not_paypal(self):
        upi_link = {
            "ok": True,
            "url": "https://pay.openai.com/c/pay/cs_live_UPI",
            "currency": "inr",
            "payment_method_types": ["card", "upi"],
        }

        self.assertTrue(paypal_links._saved_link_matches_payment_method(upi_link, "upi", {}))
        self.assertFalse(paypal_links._saved_link_matches_payment_method(upi_link, "paypal", {"link_mode": "chatgpt_checkout"}))

    def test_sqlite_smoke_reads_existing_paypal_url_when_enabled(self):
        if os.environ.get("PAYPAL_LINKS_SQLITE_SMOKE") != "1":
            self.skipTest("set PAYPAL_LINKS_SQLITE_SMOKE=1 to read the local SQLite account pool")

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

        if os.environ.get("PAYPAL_LINKS_FOLLOW_REDIRECT") == "1":
            proxy = os.environ.get("PAYPAL_LINKS_PROXY", "socks5h://127.0.0.1:7897")
            resolved = paypal_protocol._follow_stripe_redirect(url, proxy=proxy, timeout=20)
            original_host = urllib.parse.urlparse(url).netloc.lower()
            self.assertTrue(
                paypal_protocol.extract_ba_token(resolved) or "paypal.com" in resolved or "stripe.com" in resolved,
                "resolved URL should stay in Stripe/PayPal redirect chain or contain BA token",
            )
            if "pm-redirects.stripe.com" in original_host and not paypal_protocol.extract_ba_token(url):
                self.assertNotEqual(resolved, url, "Stripe redirect smoke did not advance beyond the saved pm-redirect URL")


if __name__ == "__main__":
    unittest.main()
