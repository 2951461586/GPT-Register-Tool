import unittest
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module
from sms_tool import mailbox_cfworker as mailbox_cfworker_module
from sms_tool.mailbox import MailboxAccount
from sms_tool.providers import cfworker_mailbox
from sms_tool.providers.cfworker_mailbox import CFWorkerMailboxClient


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "code": 200,
            "data": {
                "page": 1,
                "pageSize": 20,
                "total": 2,
                "items": [
                    {
                        "message_id": "m1",
                        "from_address": "noreply@tm.openai.com",
                        "to_address": "target@edu.liziai.cloud",
                        "subject": "Your temporary ChatGPT verification code",
                        "extracted_json": '[{"value":"123456"}]',
                        "received_at": 1779588674891,
                    },
                    {
                        "message_id": "m2",
                        "from_address": "noreply@tm.openai.com",
                        "to_address": "other@edu.liziai.cloud",
                        "subject": "Your temporary ChatGPT verification code",
                        "extracted_json": '[{"value":"654321"}]',
                        "received_at": 1779588674891,
                    },
                ],
            },
        }


class EmptyAdminResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"data": {"items": [], "pageSize": 20, "total": 0}}


class TargetEndpointResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "messages": [
                {
                    "message_id": "m3",
                    "from_address": "noreply@tm.openai.com",
                    "to_address": "target@edu.liziai.cloud",
                    "subject": "Your temporary ChatGPT verification code",
                    "extracted_json": '[{"value":"333333"}]',
                    "received_at": 1779588674891,
                }
            ]
        }


class MissingRecipientEndpointResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "messages": [
                {
                    "message_id": "global-latest",
                    "from_address": "noreply@tm.openai.com",
                    "subject": "Your temporary ChatGPT verification code",
                    "extracted_json": '[{"value":"202123"}]',
                    "received_at": 1779588674891,
                }
            ]
        }


class RawTextEndpointResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "messages": [
                {
                    "message_id": "m4",
                    "from_address": "noreply@tm.openai.com",
                    "to_address": "target@edu.liziai.cloud",
                    "subject": "你的临时 OpenAI 登录代码",
                    "extracted_json": "[]",
                    "raw_text": "你的临时 OpenAI 登录代码是 444444。",
                    "received_at": 1779588674891,
                }
            ]
        }


class AdminAllResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "messages": [
                {
                    "id": "worker-row-id",
                    "message_id": "smtp-message-id",
                    "to_address": "target@liziai.cloud",
                    "from_address": "noreply@tm.openai.com",
                    "subject": "=?UTF-8?B?5L2g55qEIENoYXRHUFQg5Li05pe26aqM6K+B56CB?=",
                    "bodyPreview": "Received: by cloudflare-email.net",
                    "receivedDateTime": "2026-07-15T07:49:27.297Z",
                }
            ]
        }


class CreateMailboxResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True, "data": {"emails": ["oai-test@liziai.cloud"]}}


class EmptyCreateMailboxResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True, "data": {"emails": []}}


class AdminDetailResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "ok": True,
            "message": {
                "id": "worker-row-id",
                "to_address": "target@liziai.cloud",
                "subject": "=?UTF-8?B?5L2g55qEIENoYXRHUFQg5Li05pe26aqM6K+B56CB?=",
                "body": (
                    "From: ChatGPT <noreply@tm.openai.com>\r\n"
                    "Subject: =?UTF-8?B?5L2g55qEIENoYXRHUFQg5Li05pe26aqM6K+B56CB?=\r\n"
                    "MIME-Version: 1.0\r\n"
                    "Content-Transfer-Encoding: quoted-printable\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n\r\n"
                    "<html><head><style>.code{font-size:24px}</style></head>"
                    "<body><p>=E4=BD=A0=E7=9A=84=E9=AA=8C=E8=AF=81=E7=A0=81=E6=98=AF "
                    "<strong>302959</strong></p></body></html>"
                ),
            },
        }


class CFWorkerMailboxClientTests(unittest.TestCase):
    def test_create_mailboxes_returns_addresses_from_worker_response(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="admin")

        with patch.object(cfworker_mailbox.curl_requests, "post", return_value=CreateMailboxResponse()) as post:
            emails = client.create_mailboxes(count=1, domain="liziai.cloud")

        self.assertEqual(emails, ["oai-test@liziai.cloud"])
        self.assertEqual(client.last_create_diagnostic["returned"], 1)
        self.assertIn("/api/mailboxes", post.call_args.args[0])

    def test_create_mailboxes_never_synthesizes_addresses_after_provider_failures(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="admin")

        with patch.object(cfworker_mailbox.curl_requests, "post", return_value=EmptyCreateMailboxResponse()), \
             patch.object(cfworker_mailbox.curl_requests, "get", return_value=EmptyCreateMailboxResponse()):
            with self.assertRaisesRegex(RuntimeError, "cfworker mailbox creation failed"):
                client.create_mailboxes(count=1, domain="liziai.cloud")

        self.assertFalse(client.last_create_diagnostic["ok"])
        self.assertEqual(client.last_create_diagnostic["returned"], 0)

    def test_admin_all_hydrates_detail_and_decodes_chinese_mime_message(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="admin")

        with patch.object(
            cfworker_mailbox.curl_requests,
            "get",
            side_effect=[AdminAllResponse(), AdminDetailResponse()],
        ) as get:
            messages = client._fetch_admin_messages("target@liziai.cloud", limit=5)

        normalized = cfworker_mailbox._normalize_message(messages[0], email="target@liziai.cloud")
        self.assertEqual(normalized["subject"], "你的 ChatGPT 临时验证码")
        self.assertIn("你的验证码是 302959", normalized["body"]["content"])
        self.assertNotIn("font-size", normalized["body"]["content"])
        self.assertIn("/admin/all?limit=100", get.call_args_list[0].args[0])
        self.assertIn("/admin/msg?id=worker-row-id&email=target%40liziai.cloud", get.call_args_list[1].args[0])

    def test_mixed_plain_and_encoded_rfc2047_subject_is_decoded(self):
        subject = "OpenAI - Access =?UTF-8?Q?Deactivated=0A=0A=5BC-q4RKCjo6DIKv=5D?="

        self.assertEqual(cfworker_mailbox._decode_header_value(subject), "OpenAI - Access Deactivated\n\n[C-q4RKCjo6DIKv]")

    def test_cfworker_client_uses_configurable_timeout(self):
        with patch.object(mailbox_cfworker_module, "CFWorkerMailboxClient") as client:
            mailbox_cfworker_module._cfworker_client(
                {
                    "cfworker_url": "https://worker.example",
                    "cfworker_timeout_seconds": 30,
                },
                proxy="socks5h://127.0.0.1:7897",
            )

        self.assertEqual(client.call_args.kwargs["timeout"], 30)
        self.assertEqual(client.call_args.kwargs["proxy"], "socks5h://127.0.0.1:7897")

    def test_admin_email_list_uses_proxy_filters_recipient_and_exposes_otp_body(self):
        proxy = "socks5h://127.0.0.1:7897"
        client = CFWorkerMailboxClient(
            "https://worker.example",
            admin_token="admin",
            cf_api_token="cf",
            proxy=proxy,
        )

        with patch.object(cfworker_mailbox.curl_requests, "get", return_value=FakeResponse()) as get:
            messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertEqual(get.call_args.kwargs["proxies"], {"http": proxy, "https": proxy})
        self.assertEqual(len(messages), 1)
        self.assertIn("123456", messages[0]["bodyPreview"])
        recipients = messages[0]["toRecipients"]
        self.assertEqual(recipients[0]["emailAddress"]["address"], "target@edu.liziai.cloud")
        self.assertEqual(messages[0]["receivedDateTime"], "2026-05-24T02:11:14Z")

    def test_target_endpoint_falls_back_to_address_alias_when_first_target_endpoint_is_empty(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="admin")

        with patch.object(
            cfworker_mailbox.curl_requests,
            "get",
            side_effect=[EmptyAdminResponse(), TargetEndpointResponse()],
        ) as get:
            messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertIn("/api/messages?email=target%40edu.liziai.cloud", get.call_args_list[0].args[0])
        self.assertIn("/api/messages?address=target%40edu.liziai.cloud", get.call_args_list[1].args[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("333333", messages[0]["bodyPreview"])
        self.assertEqual(messages[0]["toRecipients"][0]["emailAddress"]["address"], "target@edu.liziai.cloud")

    def test_target_endpoint_alias_is_used_when_first_target_request_times_out(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="admin")

        with patch.object(
            cfworker_mailbox.curl_requests,
            "get",
            side_effect=[RuntimeError("target timeout"), TargetEndpointResponse()],
        ) as get:
            messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertIn("/api/messages?email=target%40edu.liziai.cloud", get.call_args_list[0].args[0])
        self.assertIn("/api/messages?address=target%40edu.liziai.cloud", get.call_args_list[1].args[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("333333", messages[0]["bodyPreview"])

    def test_missing_recipient_messages_are_not_assumed_to_match_target_mailbox(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="")

        with patch.object(cfworker_mailbox.curl_requests, "get", return_value=MissingRecipientEndpointResponse()):
            with self.assertRaises(RuntimeError):
                client.fetch_messages("target@edu.liziai.cloud", limit=5)

    def test_normalized_message_does_not_synthesize_target_recipient(self):
        msg = cfworker_mailbox._normalize_message(
            {
                "message_id": "global-latest",
                "subject": "Your temporary ChatGPT verification code",
                "extracted_json": '[{"value":"202123"}]',
            },
            email="target@edu.liziai.cloud",
        )

        self.assertEqual(msg["toRecipients"], [])

    def test_extracted_json_otp_takes_priority_over_html_numbers(self):
        msg = cfworker_mailbox._normalize_message(
            {
                "message_id": "m-real-code",
                "to_address": "target@edu.liziai.cloud",
                "subject": "Your temporary ChatGPT verification code",
                "extracted_json": '[{"value":"971234"}]',
                "raw_html": "<html><body>tracking 202123, code 971234</body></html>",
            },
            email="target@edu.liziai.cloud",
        )

        self.assertTrue(msg["bodyPreview"].startswith('[{"value":"971234"}]'))
        candidate = mailbox_module._email_otp_candidate(
            MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker"),
            msg,
            keyword="verification code",
            issued_after_unix=0,
        )
        self.assertEqual(candidate["otp"], "971234")

    def test_rfc822_body_otp_beats_decoy_extracted_json(self):
        msg = cfworker_mailbox._normalize_message(
            {
                "message_id": "m-rfc822-code",
                "to_address": "target@liziai.cloud",
                "from_address": "bounces@em7877.tm.openai.com",
                "subject": "Your temporary ChatGPT verification code",
                "extracted_json": '[{"value":"308662"}]',
                "body": (
                    "From: OpenAI <noreply@tm.openai.com>\r\n"
                    "Subject: Your temporary ChatGPT verification code\r\n"
                    "MIME-Version: 1.0\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n\r\n"
                    "<html><body><div style=\"color:#353740\">"
                    + (" " * 120)
                    + "</div><strong>333350</strong></body></html>"
                ),
            },
            email="target@liziai.cloud",
        )

        candidate = mailbox_module._email_otp_candidate(
            MailboxAccount(email="target@liziai.cloud", provider="cfworker"),
            msg,
            keyword="verification code",
            issued_after_unix=0,
        )

        self.assertEqual(candidate["otp"], "333350")

    def test_html_body_otp_beats_header_number_in_extracted_json(self):
        msg = cfworker_mailbox._normalize_message(
            {
                "message_id": "m-html-code",
                "to_address": "target@liziai.cloud",
                "from_address": "bounce+bda784@tm1.openai.com",
                "subject": "Your temporary ChatGPT verification code",
                "extracted_json": '[{"value":"682375"}]',
                "body": (
                    '<html><head><style>.top{color:#202123}</style></head><body>'
                    '<p>Enter this temporary verification code to continue:</p>'
                    '<strong>096114</strong></body></html>'
                ),
            },
            email="target@liziai.cloud",
        )

        candidate = mailbox_module._email_otp_candidate(
            MailboxAccount(email="target@liziai.cloud", provider="cfworker"),
            msg,
            keyword="verification code",
            issued_after_unix=0,
        )

        self.assertEqual(candidate["otp"], "096114")

    def test_plain_text_detail_strips_leading_css_and_uses_visible_otp(self):
        msg = cfworker_mailbox._normalize_message(
            {
                "message_id": "m-css-prefix",
                "to_address": "target@liziai.cloud",
                "from_address": "bounce+6261d9@tm1.openai.com",
                "subject": "Your temporary ChatGPT verification code",
                "extracted_json": '[{"value":"682375"}]',
                "body": (
                    "#bodyCell { padding: 20px; } #bodyTable { width: 560px; } "
                    "@media only screen and (max-width: 480px) { "
                    "#bodyCell, #bodyTable, body { width: 100% !important; } "
                    "a, blockquote, body, li, p, table, td { -webkit-text-size-adjust: none !important; } "
                    "body { min-width: 100% !important; } } "
                    "Enter this temporary verification code to continue: 388302 "
                    "Please ignore this email if this wasn’t you trying to create a ChatGPT account. "
                    "Best, The ChatGPT team ChatGPT Help center"
                ),
            },
            email="target@liziai.cloud",
        )

        self.assertTrue(msg["bodyPreview"].startswith("Enter this temporary verification code"))
        self.assertNotIn("#bodyCell", msg["bodyPreview"])
        candidate = mailbox_module._email_otp_candidate(
            MailboxAccount(email="target@liziai.cloud", provider="cfworker"),
            msg,
            keyword="verification code",
            issued_after_unix=0,
        )
        self.assertEqual(candidate["otp"], "388302")

    def test_cfworker_json_remark_does_not_make_otp_look_like_css_unit(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")
        msg = {
            "id": "json-remark-code",
            "receivedDateTime": "2026-07-04T06:00:20Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": '[{"rule_id":14,"value":"453831","remark":"ChatGPT OTP"}]',
            "body": {"content": '[{"rule_id":14,"value":"453831","remark":"ChatGPT OTP"}]'},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }

        candidate = mailbox_module._email_otp_candidate(
            mailbox,
            msg,
            keyword="verification code",
            issued_after_unix=0,
        )

        self.assertEqual(candidate["otp"], "453831")

    def test_cfworker_otp_poll_waits_for_newer_duplicate_code(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")
        old = {
            "id": "old",
            "receivedDateTime": "2026-05-24T02:47:01Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": '[{"value":"111111"}]',
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }
        new = {
            "id": "new",
            "receivedDateTime": "2026-05-24T02:47:03Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": '[{"value":"222222"}]',
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }

        with patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_otp_settle_seconds": 0.01, "otp_poll_interval": 0.01}):
            with patch.object(mailbox_module, "_fetch_mailbox_messages", side_effect=[[old], [new], [new]]):
                code = mailbox_module._poll_email_otp(mailbox, timeout=1)

        self.assertEqual(code, "222222")

    def test_cfworker_otp_poll_skips_rejected_newer_code(self):
        mailbox = MailboxAccount(email="target@liziai.cloud", provider="cfworker")
        messages = [
            {
                "id": "newer-rejected",
                "receivedDateTime": "2026-07-16T07:22:44Z",
                "subject": "Your temporary ChatGPT verification code",
                "bodyPreview": '[{"value":"682375"}]',
                "body": {"content": ""},
                "toRecipients": [{"emailAddress": {"address": "target@liziai.cloud"}}],
            },
            {
                "id": "older-active",
                "receivedDateTime": "2026-07-16T07:21:57Z",
                "subject": "Your temporary ChatGPT verification code",
                "bodyPreview": '[{"value":"659948"}]',
                "body": {"content": ""},
                "toRecipients": [{"emailAddress": {"address": "target@liziai.cloud"}}],
            },
        ]

        with patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_otp_settle_seconds": 0, "otp_poll_interval": 0.01}):
            with patch.object(mailbox_module, "_fetch_mailbox_messages", return_value=messages):
                code = mailbox_module._poll_email_otp(
                    mailbox,
                    timeout=1,
                    excluded_otps={"682375"},
                )

        self.assertEqual(code, "659948")

    def test_cfworker_seen_message_is_accepted_when_it_is_inside_current_otp_window(self):
        mailbox = MailboxAccount(
            email="target@edu.liziai.cloud",
            provider="cfworker",
            seen_message_id="pre-sent",
        )
        message = {
            "id": "pre-sent",
            "receivedDateTime": "2026-07-14T01:28:23Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": "Your verification code is 866835",
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }

        candidate = mailbox_cfworker_module._latest_cfworker_otp_candidate(
            mailbox,
            keyword="verification code",
            issued_after_unix=1783992490,
            seen_message_id=mailbox.seen_message_id,
            fetch_messages_func=lambda _mailbox, **_kwargs: [message],
        )

        self.assertEqual(candidate["otp"], "866835")

    def test_email_otp_candidate_accepts_code_in_subject(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")
        msg = {
            "id": "subject-only",
            "receivedDateTime": "2026-05-25T13:58:10Z",
            "subject": "Your OpenAI code is 333333",
            "bodyPreview": "",
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }

        candidate = mailbox_module._email_otp_candidate(mailbox, msg, issued_after_unix=0)

        self.assertEqual(candidate["otp"], "333333")

    def test_target_endpoint_exposes_otp_from_raw_text_when_extracted_json_is_empty(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="")

        with patch.object(cfworker_mailbox.curl_requests, "get", return_value=RawTextEndpointResponse()):
            messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertEqual(len(messages), 1)
        self.assertIn("444444", messages[0]["bodyPreview"])

    def test_fetch_uses_urllib_fallback_when_curl_request_fails(self):
        client = CFWorkerMailboxClient("https://worker.example", admin_token="")

        with patch.object(cfworker_mailbox.curl_requests, "get", side_effect=RuntimeError("curl timeout")):
            with patch.object(client, "_request_urllib", return_value={"ok": True, "data": RawTextEndpointResponse().json()}):
                messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertEqual(len(messages), 1)
        self.assertIn("444444", messages[0]["bodyPreview"])

    def test_cfworker_fetch_falls_back_to_direct_when_configured(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")

        with patch.object(mailbox_module, "CFG", {}), \
             patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_poll_proxy": True, "cfworker_direct_fallback": True}):
            with patch.object(mailbox_module, "_cfworker_client") as client_factory:
                proxy_client = type("ProxyClient", (), {})()
                direct_client = type("DirectClient", (), {})()
                proxy_client.fetch_messages = lambda email, limit=25: (_ for _ in ()).throw(RuntimeError("proxy timeout"))
                direct_client.fetch_messages = lambda email, limit=25: [{"id": "m1"}]
                client_factory.side_effect = [proxy_client, direct_client]

                messages = mailbox_module._fetch_mailbox_messages(mailbox, limit=1, proxy="socks5h://127.0.0.1:7897")

        self.assertEqual(messages, [{"id": "m1"}])
        self.assertEqual(client_factory.call_args_list[0].kwargs["proxy"], "socks5h://127.0.0.1:7897")
        self.assertIsNone(client_factory.call_args_list[1].kwargs["proxy"])

    def test_cfworker_fetch_does_not_fall_back_to_direct_by_default(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")

        with patch.object(mailbox_module, "CFG", {}), \
             patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_poll_proxy": True}):
            with patch.object(mailbox_module, "_cfworker_client") as client_factory:
                proxy_client = type("ProxyClient", (), {})()
                proxy_client.fetch_messages = lambda email, limit=25: (_ for _ in ()).throw(RuntimeError("proxy timeout"))
                client_factory.return_value = proxy_client

                with self.assertRaises(RuntimeError):
                    mailbox_module._fetch_mailbox_messages(mailbox, limit=1, proxy="socks5h://127.0.0.1:7897")

        client_factory.assert_called_once()
        self.assertEqual(client_factory.call_args.kwargs["proxy"], "socks5h://127.0.0.1:7897")

    def test_cfworker_fetch_can_skip_proxy_when_configured(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")

        with patch.object(mailbox_module, "CFG", {}), \
             patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_poll_proxy": False}):
            with patch.object(mailbox_module, "_cfworker_client") as client_factory:
                direct_client = type("DirectClient", (), {})()
                direct_client.fetch_messages = lambda email, limit=25: [{"id": "m1"}]
                client_factory.return_value = direct_client

                messages = mailbox_module._fetch_mailbox_messages(mailbox, limit=1, proxy="socks5h://127.0.0.1:7897")

        self.assertEqual(messages, [{"id": "m1"}])
        client_factory.assert_called_once()
        self.assertIsNone(client_factory.call_args.kwargs["proxy"])


if __name__ == "__main__":
    unittest.main()
