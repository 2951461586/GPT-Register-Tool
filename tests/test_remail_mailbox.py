import argparse
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import mailbox as mailbox_router
from sms_tool import cli
from sms_tool import mailbox_remail
from sms_tool import mailbox_parsers
from sms_tool.mailbox_types import MailboxAccount


class FakeResponse:
    def __init__(self, body, status_code=200, text=""):
        self._body = body
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._body


class RegistrationPhonePoolTests(unittest.TestCase):
    def test_explicit_sms_registration_builds_smsbower_pool(self):
        phone_pool = SimpleNamespace(phones=[SimpleNamespace(provider="smsbower")])
        args = argparse.Namespace(
            no_phone_reuse=False,
            registration_at_only=False,
            phone_reuse=True,
            max_reuse_count=1,
            phone_send_cooldown=45,
            phone_source="smsbower",
        )

        with patch("sms_tool.phone_reuse.has_phone_reuse_config", return_value=True), \
             patch("sms_tool.phone_reuse.create_phone_pool", return_value=phone_pool) as create, \
             patch("sms_tool.phone_reuse.print_phone_pool_status") as print_status:
            result = cli._registration_phone_pool(args)

        self.assertIs(result, phone_pool)
        create.assert_called_once_with(
            max_reuse_count=1,
            send_cooldown_seconds=45,
            source_override="smsbower",
        )
        print_status.assert_called_once_with(phone_pool)

    def test_target_at200_forwards_phone_pool_to_registration_batch(self):
        phone_pool = SimpleNamespace(phones=[SimpleNamespace(provider="smsbower")])
        mailbox = MailboxAccount(email="user@example.com", provider="remail", token="service-token")
        args = argparse.Namespace(
            buy_remail_mailbox=True,
            remail_service_mode="purchase",
            target_at200=1,
            max_mailbox_purchases=1,
            max_remail_cost=0,
            registration_batch_id="remail_long_term_test",
            count=1,
            proxy="",
            workers=1,
            registration_at_only=False,
            paypal_generation_type="hosted_long_url",
            registration_mode="passwordless",
        )

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cli, "_registration_phone_pool", return_value=phone_pool), \
             patch.object(cli, "_load_mailbox_pool", return_value=[mailbox]), \
             patch.object(cli, "_proxy_pool_values", return_value=[]), \
             patch.object(cli, "_payment_method", return_value="paypal"), \
             patch.object(cli, "run_batch", return_value=[{"success": True}]) as run_batch, \
             patch.object(cli, "_save_registration_results", return_value={"success": 1, "quality": {}}), \
             patch.object(cli, "runtime_file", return_value=Path(tmp) / "report.json"), \
             redirect_stdout(io.StringIO()):
            cli._run_target_at200(args, Path(tmp))

        self.assertIs(run_batch.call_args.kwargs["phone_pool"], phone_pool)
        self.assertFalse(run_batch.call_args.kwargs["codex_oauth"])


def order_payload(index=1, mode="code"):
    return {
        "id": index,
        "orderNo": f"R{index}",
        "deliveryEmail": f"user{index}@outlook.com",
        "serviceToken": f"st-token-{index}",
        "serviceMode": mode,
        "payAmount": "0.80",
        "status": "active",
    }


class ReMailOrderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "enabled": True,
            "base_url": "https://remail.example",
            "api_key": "rk-secret-key",
            "project_id": 2,
            "product_id": 5,
            "service_mode": "code",
            "supply": "private_first",
            "email_suffix": "outlook.com",
        }

    def test_create_code_order_uses_bearer_idempotency_and_parses_account(self):
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse(order_payload())) as post:
            account = mailbox_remail._create_remail_order()

        self.assertEqual(account.provider, "remail")
        self.assertEqual(account.source, "remail_code")
        self.assertEqual(account.email, "user1@outlook.com")
        self.assertEqual(account.order_no, "R1")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer rk-secret-key")
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])
        self.assertEqual(kwargs["params"], {"serviceMode": "code", "supply": "private_first"})
        self.assertEqual(kwargs["json"], {"projectId": 2, "emailSuffix": "outlook.com"})

    def test_create_order_retries_transient_5xx_with_stable_idempotency_key(self):
        responses = [
            FakeResponse({"error": "bad gateway"}, 502),
            FakeResponse(order_payload()),
        ]
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.time, "sleep", return_value=None), \
             patch.object(mailbox_remail.http_requests, "post", side_effect=responses) as post, \
             redirect_stdout(io.StringIO()):
            account = mailbox_remail._create_remail_order()

        self.assertEqual(account.order_no, "R1")
        self.assertEqual(post.call_count, 2)
        keys = {call.kwargs["headers"]["Idempotency-Key"] for call in post.call_args_list}
        self.assertEqual(len(keys), 1)  # retry reuses the same key, never double-charges

    def test_create_order_does_not_retry_non_retryable_4xx(self):
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.time, "sleep", return_value=None), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse({"error": "bad_request"}, 400)) as post, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(mailbox_remail.ReMailHttpError):
                mailbox_remail._create_remail_order()

        self.assertEqual(post.call_count, 1)

    def test_dead_history_registry_filters_email_and_never_stores_service_token(self):
        dead = MailboxAccount(
            email="dead@example.com",
            provider="remail",
            order_no="R-DEAD",
            purchase_id="99",
            token="st-secret",
        )
        live = MailboxAccount(email="live@example.com", provider="remail", order_no="R-LIVE", token="st-live")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(mailbox_remail, "_dead_remail_registry_path", return_value=Path(tmp) / "dead.json"), \
             patch("sms_tool.storage.list_terminal_remail_accounts", return_value=[]):
            self.assertTrue(mailbox_remail.record_dead_remail_account(dead))
            filtered = mailbox_remail.filter_dead_remail_mailboxes([dead, live])
            payload = (Path(tmp) / "dead.json").read_text(encoding="utf-8")

        self.assertEqual([item.email for item in filtered], ["live@example.com"])
        self.assertNotIn("st-secret", payload)

    def test_dead_history_filter_includes_terminal_database_records(self):
        mailbox = MailboxAccount(email="dead@example.com", provider="remail", order_no="R-DEAD")
        with patch.object(mailbox_remail, "_read_dead_remail_registry", return_value=[]), \
             patch("sms_tool.storage.list_terminal_remail_accounts", return_value=[{
                 "email": "dead@example.com",
                 "order_no": "R-DEAD",
                 "purchase_id": "",
             }]):
            filtered = mailbox_remail.filter_dead_remail_mailboxes([mailbox])

        self.assertEqual(filtered, [])

    def test_purchase_batch_returns_only_successful_orders(self):
        response = [
            {"index": 0, "status": "succeeded", "order": order_payload(1, "purchase")},
            {"index": 1, "status": "failed", "error": {"code": "insufficient_inventory", "message": "empty"}},
        ]
        args = argparse.Namespace(count=2)
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse(response)) as post, \
             redirect_stdout(io.StringIO()):
            accounts = mailbox_remail._create_remail_mailboxes(args, service_mode="purchase")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].source, "remail_purchase")
        self.assertEqual(post.call_args.kwargs["json"]["quantity"], 2)
        self.assertEqual(post.call_args.kwargs["params"]["serviceMode"], "purchase")

    def test_large_purchase_batch_scales_request_timeout(self):
        response = [{"index": 0, "status": "succeeded", "order": order_payload(1, "purchase")}]
        args = argparse.Namespace(count=100)
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse(response)) as post:
            mailbox_remail._create_remail_mailboxes(args, service_mode="purchase")

        self.assertEqual(post.call_args.kwargs["timeout"], 200)

    def test_purchase_batch_recovers_exact_recent_orders_after_502(self):
        created_at = "1970-01-01T00:16:40+00:00"
        summaries = []
        details = {}
        for index in (1, 2):
            detail = order_payload(index, "purchase")
            detail.update({"createdAt": created_at, "projectId": 2, "projectProductId": 5})
            summaries.append({key: value for key, value in detail.items() if key != "serviceToken"})
            details[detail["orderNo"]] = detail

        def request(method, path, **_kwargs):
            if method == "POST":
                raise mailbox_remail.ReMailHttpError(502, {"retryable": True})
            if path == "/v1/open/orders":
                return {"items": summaries}
            return details[path.rsplit("/", 1)[-1]]

        args = argparse.Namespace(count=2)
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail, "_remail_request", side_effect=request) as call, \
             patch.object(mailbox_remail.time, "time", return_value=1_000), \
             redirect_stdout(io.StringIO()):
            accounts = mailbox_remail._create_remail_mailboxes(args, service_mode="purchase")

        self.assertEqual([account.order_no for account in accounts], ["R1", "R2"])
        self.assertEqual(call.call_count, 4)

    def test_purchase_batch_does_not_recover_non_ambiguous_400(self):
        args = argparse.Namespace(count=2)
        error = mailbox_remail.ReMailHttpError(400, {"error": "invalid_request"})
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail, "_remail_request", side_effect=error) as call:
            with self.assertRaises(mailbox_remail.ReMailHttpError):
                mailbox_remail._create_remail_mailboxes(args, service_mode="purchase")

        self.assertEqual(call.call_count, 1)

    def test_parse_recovery_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recovered-remail.txt"
            path.write_text(
                "remail://user1@outlook.com---st-token-1---R1---101\n",
                encoding="utf-8",
            )
            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider, "remail")
        self.assertEqual(records[0].email, "user1@outlook.com")
        self.assertEqual(records[0].token, "st-token-1")
        self.assertEqual(records[0].order_no, "R1")
        self.assertEqual(records[0].purchase_id, "101")

    def test_http_error_redacts_api_key_and_service_token(self):
        token = "st-private-token"
        body = {"message": f"bad rk-secret-key {token}"}
        output = io.StringIO()
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "get", return_value=FakeResponse(body, 401)):
            with self.assertRaises(RuntimeError) as caught, redirect_stdout(output):
                mailbox_remail._remail_request("GET", "/v1/pickup", secrets=(token,))
        rendered = str(caught.exception) + output.getvalue()
        self.assertNotIn("rk-secret-key", rendered)
        self.assertNotIn(token, rendered)
        self.assertIn("[REDACTED]", rendered)


class ReMailPickupTests(unittest.TestCase):
    def setUp(self):
        self.account = MailboxAccount(
            email="alias@example.com",
            provider="remail",
            token="st-private-token",
            order_no="R1",
            seen_message_id="10",
        )

    def test_pickup_normalizes_summary_and_fetches_detail_when_needed(self):
        summary = {
            "items": [
                {
                    "id": 11,
                    "sender": "noreply@tm.openai.com",
                    "recipient": "alias@example.com",
                    "receivedAt": "2026-07-21T08:00:00Z",
                    "subject": "Your login code",
                    "bodyPreview": "Open the message to continue",
                    "verificationCode": "",
                }
            ]
        }
        detail = dict(summary["items"][0], body="Your verification code is 654321")
        responses = [FakeResponse(summary), FakeResponse(detail)]
        with patch.object(mailbox_remail.http_requests, "get", side_effect=responses) as get:
            messages = mailbox_remail._fetch_remail_messages(self.account, proxy="http://127.0.0.1:7897")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "11")
        self.assertEqual(messages[0]["from"], "noreply@tm.openai.com")
        self.assertEqual(messages[0]["toRecipients"][0]["emailAddress"]["address"], "alias@example.com")
        self.assertIn("654321", messages[0]["body"]["content"])
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("Authorization", get.call_args_list[0].kwargs["headers"])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["token"], "st-private-token")
        self.assertEqual(get.call_args_list[0].kwargs["proxies"]["https"], "http://127.0.0.1:7897")

    def test_candidate_filters_snapshot_time_recipient_and_excluded_code(self):
        issued_after = int(time.time()) - 10

        def message(message_id, code, recipient="alias@example.com", offset=0):
            received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued_after + offset))
            return mailbox_remail._normalize_remail_message({
                "id": message_id,
                "sender": "noreply@tm.openai.com",
                "recipient": recipient,
                "receivedAt": received,
                "subject": "Your verification code",
                "bodyPreview": f"Your verification code is {code}",
                "verificationCode": code,
            })

        messages = [
            message(10, "111111", offset=5),
            message(11, "222222", offset=-5),
            message(12, "333333", recipient="other@example.com", offset=6),
            message(13, "444444", offset=7),
            message(14, "555555", offset=8),
        ]
        candidate = mailbox_remail._latest_remail_otp_candidate(
            self.account,
            messages,
            issued_after_unix=issued_after,
            excluded_otps={"555555"},
        )
        self.assertEqual(candidate["otp"], "444444")
        self.assertEqual(candidate["id"], "13")

    def test_structured_code_accepts_localized_subject_from_exact_openai_sender(self):
        item = {
            "sender": "ChatGPT <otp@tm1.openai.com>",
            "recipient": self.account.email,
            "subject": "ChatGPT localized subject",
            "verificationCode": "654321",
        }

        self.assertEqual(
            mailbox_remail._trusted_structured_remail_code(self.account, item),
            "654321",
        )

    def test_structured_code_rejects_wrong_recipient_or_untrusted_tm1_sender(self):
        item = {
            "sender": "Attacker <alerts@tm1.openai.com>",
            "recipient": self.account.email,
            "subject": "Your verification code",
            "verificationCode": "654321",
        }
        self.assertEqual(mailbox_remail._trusted_structured_remail_code(self.account, item), "")
        item.update({"sender": "ChatGPT <otp@tm1.openai.com>", "recipient": "other@example.com"})
        self.assertEqual(mailbox_remail._trusted_structured_remail_code(self.account, item), "")

    def test_router_uses_remail_polling_and_applies_clock_skew_grace(self):
        issued_after = 1_000
        with patch.object(mailbox_router, "_email_cfg", return_value={
            "remail_otp_issued_after_grace_seconds": 90,
        }), patch.object(
            mailbox_remail,
            "_poll_remail_otp",
            return_value="654321",
        ) as poll:
            code = mailbox_router._poll_email_otp(
                self.account,
                subject_keyword="verification code|login code",
                timeout=0,
                issued_after_unix=issued_after,
                proxy="http://registration-proxy.example:8080",
            )

        self.assertEqual(code, "654321")
        poll.assert_called_once()
        kwargs = poll.call_args.kwargs
        self.assertEqual(kwargs["issued_after_unix"], issued_after - 90)
        self.assertEqual(kwargs["proxy"], mailbox_router._configured_mailbox_proxy())

    def test_router_keeps_explicit_remail_provider_when_password_is_present(self):
        account = MailboxAccount(
            email="user@outlook.com",
            password="registration-password",
            provider="remail",
            token="service-token",
        )
        with patch.object(mailbox_router, "_email_cfg", return_value={
            "chongzhi": {"enabled": True},
        }), patch.object(
            mailbox_router,
            "_poll_chongzhi_otp",
        ) as chongzhi_poll, patch.object(
            mailbox_remail,
            "_poll_remail_otp",
            return_value="654321",
        ) as remail_poll:
            code = mailbox_router._poll_email_otp(account, timeout=0)

        self.assertEqual(code, "654321")
        remail_poll.assert_called_once()
        chongzhi_poll.assert_not_called()

    def test_inbox_mode_fetches_detail_even_when_summary_has_code(self):
        summary = {
            "items": [{
                "id": 12,
                "sender": "noreply@tm.openai.com",
                "recipient": "alias@example.com",
                "receivedAt": "2026-07-21T08:00:00Z",
                "subject": "Your login code",
                "bodyPreview": "Your code is 654321",
                "verificationCode": "654321",
            }]
        }
        detail = dict(summary["items"][0], body="Full ReMail message body")
        with patch.object(mailbox_remail.http_requests, "get", side_effect=[FakeResponse(summary), FakeResponse(detail)]) as get:
            messages = mailbox_remail._fetch_remail_messages(self.account, include_body=True)

        self.assertEqual(get.call_count, 2)
        self.assertEqual(messages[0]["body"]["content"], "Full ReMail message body")

    def test_pickup_401_refreshes_service_token_from_order_and_retries_once(self):
        refreshed_order = order_payload()
        refreshed_order["deliveryEmail"] = self.account.email
        refreshed_order["serviceToken"] = "st-current-token"
        responses = [
            FakeResponse({"message": "Credential is invalid or expired."}, 401),
            FakeResponse(refreshed_order),
            FakeResponse({"items": []}),
        ]
        cfg = {"api_key": "rk-secret-key", "base_url": "https://remail.example"}
        with patch.object(mailbox_remail, "_remail_cfg", return_value=cfg), \
             patch.object(mailbox_remail.http_requests, "get", side_effect=responses) as get:
            messages = mailbox_remail._fetch_remail_messages(self.account)

        self.assertEqual(messages, [])
        self.assertEqual(self.account.token, "st-current-token")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[1].args[0], "https://remail.example/v1/open/orders/R1")
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer rk-secret-key")
        self.assertEqual(get.call_args_list[2].kwargs["params"]["token"], "st-current-token")

    def test_expired_code_order_reports_that_api_key_cannot_read_old_inbox(self):
        expired_order = order_payload()
        expired_order.update({
            "deliveryEmail": self.account.email,
            "serviceToken": "",
            "status": "completed",
            "receiveUntil": "2026-07-22T18:08:59Z",
        })
        cfg = {"api_key": "rk-secret-key", "base_url": "https://remail.example"}
        with patch.object(mailbox_remail, "_remail_cfg", return_value=cfg), \
             patch.object(mailbox_remail.http_requests, "get", side_effect=[
                 FakeResponse({"message": "Credential is invalid or expired."}, 401),
                 FakeResponse(expired_order),
             ]):
            with self.assertRaisesRegex(RuntimeError, "2026-07-22T18:08:59Z") as caught:
                mailbox_remail._fetch_remail_messages(self.account)

        rendered = str(caught.exception)
        self.assertIn("purchase", rendered)
        self.assertNotIn("rk-secret-key", rendered)
        self.assertNotIn("st-private-token", rendered)

    def test_view_inbox_uses_explicit_service_token_and_returns_full_body(self):
        args = argparse.Namespace(
            email="alias@example.com",
            session_file=None,
            chatai_mailbox_file=None,
            mailbox_file=None,
            remail_token="st-private-token",
            email_password=None,
            email_refresh_token=None,
            email_access_token=None,
            inbox_limit=20,
            proxy=None,
        )
        messages = [{
            "id": "12",
            "receivedDateTime": "2026-07-21T08:00:00Z",
            "from": "noreply@tm.openai.com",
            "toRecipients": [{"emailAddress": {"address": "alias@example.com"}}],
            "subject": "Your login code",
            "bodyPreview": "Your code is 654321",
            "body": {"content": "Full ReMail message body"},
            "verificationCode": "654321",
        }]
        output = io.StringIO()
        with patch("sms_tool.mailbox._fetch_mailbox_messages", return_value=messages) as fetch, redirect_stdout(output):
            cli._view_inbox(args)

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "remail")
        self.assertEqual(payload["messages"][0]["body"], "Full ReMail message body")
        self.assertEqual(payload["messages"][0]["verificationCode"], "654321")
        self.assertEqual(fetch.call_args.kwargs["include_body"], True)

    def test_view_inbox_persists_refreshed_service_token_to_session_and_database(self):
        args = argparse.Namespace(
            email="alias@example.com",
            session_file=None,
            chatai_mailbox_file=None,
            mailbox_file=None,
            remail_token=None,
            email_password=None,
            email_refresh_token=None,
            email_access_token=None,
            inbox_limit=20,
            proxy=None,
        )
        data = {
            "email": "alias@example.com",
            "mailbox": {
                "email": "alias@example.com",
                "provider": "remail",
                "token": "st-old-token",
                "order_no": "R1",
                "purchase_id": "1",
            },
        }

        def fetch(mailbox, **_kwargs):
            self.assertEqual(mailbox.order_no, "R1")
            self.assertEqual(mailbox.purchase_id, "1")
            mailbox.token = "st-current-token"
            return []

        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            session_path.write_text(json.dumps(data), encoding="utf-8")
            with patch("sms_tool.session_refresh._load_seed_session", return_value=(data, str(session_path))), \
                 patch("sms_tool.mailbox._fetch_mailbox_messages", side_effect=fetch), \
                 patch.object(cli, "upsert_account", return_value=True) as upsert, \
                 redirect_stdout(io.StringIO()):
                cli._view_inbox(args)

            saved = json.loads(session_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["mailbox"]["token"], "st-current-token")
        self.assertEqual(upsert.call_args.args[0]["mailbox"]["token"], "st-current-token")


class ReMailRouterTests(unittest.TestCase):
    def test_load_purchase_pool_and_default_auto_create_route_to_remail(self):
        expected = MailboxAccount(email="user@example.com", provider="remail", token="st")
        args = argparse.Namespace(buy_remail_mailbox=True, buy_cfworker_mailbox=False, remail_service_mode=None)
        with patch.object(mailbox_router.mailbox_remail, "_create_remail_mailboxes", return_value=[expected]) as create:
            self.assertEqual(mailbox_router._load_mailbox_pool(args), [expected])
            create.assert_called_once_with(args, service_mode="purchase")

        with patch.object(mailbox_router, "_remail_enabled", return_value=True), \
             patch.object(mailbox_router.mailbox_remail, "_create_remail_order", return_value=expected) as create:
            self.assertIs(mailbox_router._ensure_mailbox_account(), expected)
            create.assert_called_once_with(service_mode="code")

    def test_explicit_code_mode_creates_remail_pool_for_desktop_one_click(self):
        expected = MailboxAccount(email="user@example.com", provider="remail", token="st")
        args = argparse.Namespace(
            buy_remail_mailbox=False,
            buy_cfworker_mailbox=False,
            remail_service_mode="code",
        )
        with patch.object(mailbox_router.mailbox_remail, "_create_remail_mailboxes", return_value=[expected]) as create:
            self.assertEqual(mailbox_router._load_mailbox_pool(args), [expected])
            create.assert_called_once_with(args, service_mode="code")

    def test_direct_service_token_requires_email_and_uses_remail_provider(self):
        cfg = {"service_token": "st-config", "delivery_email": "saved@example.com", "order_no": "R9"}
        with patch.object(mailbox_router.mailbox_remail, "_remail_cfg", return_value=cfg), \
             patch.object(mailbox_router, "_gmail_mailbox_from_config", return_value=None):
            account = mailbox_router._mailbox_from_config(argparse.Namespace(remail_token=None, email=None))
        self.assertEqual(account.provider, "remail")
        self.assertEqual(account.email, "saved@example.com")
        self.assertEqual(account.token, "st-config")

    def test_service_token_can_be_passed_without_process_arguments(self):
        args = argparse.Namespace(remail_token=None, email="saved@example.com")
        with patch.dict("os.environ", {"REMAIL_SERVICE_TOKEN": "st-environment"}), \
             patch.object(mailbox_router.mailbox_remail, "_remail_cfg", return_value={}), \
             patch.object(mailbox_router, "_gmail_mailbox_from_config", return_value=None):
            account = mailbox_router._mailbox_from_config(args)

        self.assertEqual(account.provider, "remail")
        self.assertEqual(account.email, "saved@example.com")
        self.assertEqual(account.token, "st-environment")


if __name__ == "__main__":
    unittest.main()
