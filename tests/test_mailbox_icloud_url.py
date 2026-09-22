import tempfile
import unittest
import base64
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module
from sms_tool import mailbox_icloud_url, mailbox_parsers
from sms_tool.mail_otp import _email_otp_candidate, _extract_otp_from_text, _message_received_ts
from sms_tool.mailbox_types import MailboxAccount
from sms_tool.providers.mailbox_graph import MailboxAuthInvalidError


class _Response:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ICloudUrlMailboxTests(unittest.TestCase):
    def test_token_file_parses_three_and_four_hyphen_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icloud.txt"
            path.write_text(
                "first@icloud.com----https://icloud-api.example/show/secret/first@icloud.com\n"
                "second@icloud.com---http://mail.example/messages/secret/second@icloud.com\n",
                encoding="utf-8",
            )

            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual([record.email for record in records], ["first@icloud.com", "second@icloud.com"])
        self.assertTrue(all(record.provider == "icloud_url" for record in records))
        self.assertTrue(all(record.auth_mode == "otp_url" for record in records))

    def test_real_world_shape_works_through_legacy_mixed_file_route(self):
        line = (
            "target@icloud.com----"
            "https://mail.example/messages/AbCd_0123-credential/target%40icloud.com"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icloud.txt"
            path.write_text("\ufeff" + line + "\r\n", encoding="utf-8")

            records = mailbox_parsers._parse_chatai_mailbox_file(path)
            loaded = mailbox_module._load_mailbox_pool(Namespace(chatai_mailbox_file=str(path)))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider, "icloud_url")
        self.assertEqual(records[0].auth_mode, "otp_url")
        self.assertEqual([record.email for record in loaded], ["target@icloud.com"])
        self.assertEqual(loaded[0].token, records[0].token)

    def test_card_page_is_normalized_for_login_otp_filtering(self):
        page = """
        <head><meta charset="utf-8"><style>.outer{width:999999px}</style></head>
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">你的临时 ChatGPT 登录代码</div>
          <div class="dt">Mon, 03 Aug 2026 06:32:17 +0000 (UTC)</div>
          <div class="bd"><meta name="x"><style>.x{width:123456px}</style><div>你的临时代码：654321</div></div>
        </div>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://icloud-api.example/show/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code")
        self.assertEqual(candidate["otp"], "654321")
        self.assertNotIn("123456", messages[0]["body"]["content"])

    def test_account_login_failure_page_fails_fast_and_quarantines(self):
        page = "<div class='card'><p>账号登录失败，应用专用密码可能失效</p></div>"
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://icloud-api.example/show/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)), \
             patch.object(mailbox_icloud_url, "record_mailbox_auth_invalid") as quarantine:
            with self.assertRaisesRegex(MailboxAuthInvalidError, "mailbox_auth_invalid"):
                mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)
        quarantine.assert_called_once()

    def test_card_page_tolerates_unescaped_sender_address(self):
        page = """
        <div class="card">
          <div class="fr">OpenAI <noreply_at_tm_openai_com@icloud.com></div>
          <div class="su">Your temporary ChatGPT verification code</div>
          <div class="dt">Wed, 05 Aug 2026 08:03:00 +0000</div>
          <div class="bd"><html><body>Your login code is 654321</body></html></div>
        </div>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://icloud-api.example/show/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code")
        self.assertEqual(candidate["otp"], "654321")

    def test_mail_card_article_page_is_normalized_for_otp_polling(self):
        page = """
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">Your temporary ChatGPT verification code</span>
              <span class="date">2026-08-04 14:12:35</span>
            </summary>
            <div class="meta">Sender: OpenAI &lt;noreply@openai.com&gt;</div>
            <pre class="body">Enter this temporary verification code to continue: 654321</pre>
          </details>
        </article>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code|verification code")
        self.assertEqual(candidate["otp"], "654321")
        self.assertIn("noreply@openai.com", messages[0]["from"])

    def test_card_messages_are_normalized_to_newest_first(self):
        page = """
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">Your ChatGPT login code</div>
          <div class="dt">Mon, 03 Aug 2026 06:31:00 +0000</div>
          <div class="bd">Your code is 111111</div>
        </div>
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">Your ChatGPT login code</div>
          <div class="dt">Mon, 03 Aug 2026 06:32:00 +0000</div>
          <div class="bd">Your code is 222222</div>
        </div>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=1)

        self.assertEqual(
            [_email_otp_candidate(mailbox, message, keyword="login code")["otp"] for message in messages],
            ["222222"],
        )

    def test_yangyang_page_uses_list_and_detail_apis(self):
        page = """
        <script>
        var detailBase='/message/';
        var detailSuffix='/secret/target@icloud.com';
        var pageBase='/api/messages/secret/target@icloud.com';
        </script>
        """
        listing = {"items": [{
            "id": 42,
            "mailbox": "JUNK",
            "subject": "你的临时 ChatGPT 登录代码",
            "from_address": "OpenAI",
            "received_at": "2026-08-03 13:53:09",
        }], "has_more": False}
        encoded_body = base64.b64encode("<p>你的临时代码是 456789</p>".encode("utf-8")).decode("ascii")
        detail = {
            "body": "data:text/html;charset=utf-8;base64," + encoded_body,
            "fromAddress": "OpenAI",
            "html": False,
            "receivedAt": "2026-08-03 13:53:09",
            "subject": "你的临时 ChatGPT 登录代码",
        }
        responses = [_Response(text=page), _Response(payload=listing), _Response(payload=detail)]
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="http://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", side_effect=responses):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code")
        self.assertEqual(candidate["otp"], "456789")

    def test_snapshot_uses_yangyang_listing_without_fetching_message_details(self):
        page = """
        <script>
        var detailBase='/message/';
        var detailSuffix='/secret/target@icloud.com';
        var pageBase='/api/messages/secret/target@icloud.com';
        </script>
        """
        listing = {"items": [
            {"id": 41, "subject": "older", "received_at": "2026-08-03 13:52:09"},
            {"id": 42, "subject": "newer", "received_at": "2026-08-03 13:53:09"},
        ]}
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="http://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(
            mailbox_icloud_url.curl_requests,
            "get",
            side_effect=[_Response(text=page), _Response(payload=listing)],
        ) as get:
            message_id = mailbox_module._snapshot_mailbox_message(mailbox)

        self.assertEqual(message_id, "42")
        self.assertEqual(mailbox.seen_message_ids, ("42", "41"))
        self.assertGreater(mailbox.seen_message_received_ts, 0)
        self.assertEqual(get.call_count, 2)

    def test_mailbox_dispatch_and_credentials_use_otp_url_provider(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/show/secret/target@icloud.com",
        )
        self.assertTrue(mailbox_module.mailbox_has_inbox_credentials(mailbox))
        with patch.object(mailbox_icloud_url, "fetch_icloud_url_messages", return_value=[{"id": "m1"}]) as fetch:
            messages = mailbox_module._fetch_mailbox_messages(mailbox, limit=1)
        self.assertEqual(messages, [{"id": "m1"}])
        fetch.assert_called_once()

    def test_poll_applies_icloud_timestamp_grace(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/show/secret/target@icloud.com",
        )
        candidate = {"otp": "654321", "received_ts": 999}
        with (
            patch.object(mailbox_module, "_latest_email_otp_candidate", return_value=candidate) as latest,
            patch.object(mailbox_module, "_email_otp_settle_seconds", return_value=0),
            patch.object(mailbox_module, "_email_cfg", return_value={}),
        ):
            code = mailbox_module._poll_email_otp(
                mailbox,
                subject_keyword="login code",
                timeout=1,
                issued_after_unix=1000,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(latest.call_args.kwargs["issued_after_unix"], 910)

    def test_snapshot_message_and_older_messages_are_not_reused(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
            seen_message_id="snapshot",
        )
        old_messages = [
            self._otp_message("snapshot", "111111", "2026-08-04T10:00:00+00:00"),
            self._otp_message("older", "222222", "2026-08-04T09:59:30+00:00"),
            self._otp_message("older-undated", "555555", ""),
        ]
        mailbox.seen_message_ids = tuple(message["id"] for message in old_messages)
        mailbox.seen_message_received_ts = mailbox_module._message_received_ts(old_messages[0])
        self.assertIsNone(mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=old_messages,
        ))

        new_messages = [
            self._otp_message("new", "333333", "2026-08-04T10:00:10+00:00"),
            *old_messages,
        ]
        candidate = mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=new_messages,
        )
        self.assertEqual(candidate["otp"], "333333")
        self.assertEqual(candidate["id"], "new")

        undated = self._otp_message("new-undated", "444444", "")
        candidate = mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=[*old_messages, undated],
        )
        self.assertEqual(candidate["otp"], "444444")
        self.assertEqual(candidate["id"], "new-undated")

    @staticmethod
    def _otp_message(message_id, code, received_at):
        return {
            "id": message_id,
            "subject": "Your ChatGPT login code",
            "from": "OpenAI <noreply@openai.com>",
            "receivedDateTime": received_at,
            "body": {"content": f"Your verification code is {code}"},
            "toRecipients": [{"emailAddress": {"address": "target@icloud.com"}}],
        }

    def test_request_error_does_not_expose_mailbox_url(self):
        secret_url = "https://mail.example/show/private-token/target@icloud.com"
        with patch.object(mailbox_icloud_url.curl_requests, "get", side_effect=RuntimeError(secret_url)):
            with self.assertRaisesRegex(RuntimeError, "iCloud OTP URL request failed: RuntimeError") as caught:
                mailbox_icloud_url._request(secret_url)
        self.assertNotIn("private-token", str(caught.exception))

    def test_request_asks_for_revalidation_of_the_forwarding_page(self):
        """A cached listing keeps a mid-window mail invisible for the whole budget.

        The OTP wait re-fetches the same forwarding URL every ``otp_poll_interval``
        seconds.  If the body is served from cache, every poll inside the window
        sees the same stale listing and the run times out with the code already
        in the inbox (2026-09-11 triage).  Asserting the request opts out of
        caching pins the fix; asserting the URL is byte-identical pins *why* the
        stronger cache-busting parameter was rejected -- these are signed
        forwarding URLs, so an extra query parameter risks a 403.
        """
        url = "https://mail.example/show/token/target@icloud.com"
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response()) as get:
            mailbox_icloud_url._request(url)

        args, kwargs = get.call_args
        headers = {str(key).lower(): value for key, value in dict(kwargs["headers"]).items()}
        self.assertEqual(headers.get("cache-control"), "no-cache")
        self.assertEqual(headers.get("pragma"), "no-cache")
        self.assertEqual(args[0], url)

    def test_revalidation_headers_are_not_mutated_by_a_request(self):
        """Contract guard, not a mutation target.

        Dropping the ``dict(...)`` copy in ``_request`` is an *equivalent
        mutant* -- nothing on the current path mutates the mapping -- so this
        assertion cannot kill it, and is not claimed to (verified 2026-09-11:
        the mutant survives by design).  It stays because the map is
        module-level and shared by every poll, so a future change that pops a
        header would silently corrupt all later requests instead of failing.
        """
        before = dict(mailbox_icloud_url._NO_CACHE_HEADERS)
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response()):
            mailbox_icloud_url._request("https://mail.example/show/token/target@icloud.com")
        self.assertEqual(mailbox_icloud_url._NO_CACHE_HEADERS, before)

    def test_api_json_channel_without_code_yields_no_messages(self):
        """``ima3.52dfd.top`` 这类渠道回 JSON，不回 HTML。

        实测形状（2026-09-16，batch 25116）：该渠道每次回
        ``{"code":"no_code","message":"暂未收到验证码",...}``。旧实现把它当 HTML
        解析成「0 封邮件」，与「邮箱里确实没邮件」完全无法区分。这里断言
        ``_parse_card_messages`` **未被调用** —— 只断言返回 ``[]`` 无法区分
        「走了 JSON 分支」和「HTML 分支恰好也没解析出卡片」。
        """
        payload = '{"code":"no_code","message":"暂未收到验证码","retryable":true,"success":false}'
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://ima3.example/api/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url, "_parse_card_messages") as card, \
             patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=payload)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(messages, [])
        card.assert_not_called()

    def test_api_json_channel_code_reaches_the_registration_otp_filter(self):
        """JSON 里的 6 位码必须能穿过下游关键词/发件人过滤。

        合成邮件只在「主题含 verification code / login code、发件人不是黑名单」
        时才被注册泳道接受，所以这里直接跑 ``_email_otp_candidate``，而不是只看
        消息条数。
        """
        payload = '{"code":"483920","message":"","retryable":false,"success":true}'
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://ima3.example/api/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=payload)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="verification code|login code")
        self.assertEqual(candidate["otp"], "483920")

    def test_api_json_channel_reads_nested_and_text_only_codes(self):
        """码可能不在顶层 ``code`` 上：嵌套字段与纯文本兜底都要能取到。"""
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://ima3.example/api/secret/target@icloud.com",
        )
        for payload, expected in (
            ('{"code":"ok","data":{"otp":"445566"}}', "445566"),
            ('{"code":"ok","message":"Your code is 778899"}', "778899"),
        ):
            with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=payload)):
                messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)
            candidate = _email_otp_candidate(mailbox, messages[0], keyword="verification code|login code")
            self.assertEqual(candidate["otp"], expected)

    def test_api_json_channel_does_not_read_ids_or_timestamps_as_codes(self):
        """6 位数字不等于验证码：``id`` / ``timestamp`` 这类忽略键必须被挡掉。

        字段名那一层跳过它们还不够 —— 兜底的文本提取器会重新看到整段 JSON，
        所以 ``_api_payload_text`` 要用同一份忽略键集合先把 payload 压干净。
        """
        payload = '{"code":"ok","id":"123456","timestamp":"178952","success":true}'
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://ima3.example/api/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=payload)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(messages, [])

    def test_api_json_channel_snapshot_also_routes_through_the_json_branch(self):
        """快照走的是另一个入口，必须同样分流，否则基线会把 JSON 记成「无邮件」。"""
        payload = '{"code":"no_code","success":false}'
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://ima3.example/api/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url, "_parse_card_messages") as card, \
             patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=payload)):
            messages = mailbox_icloud_url.snapshot_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(messages, [])
        card.assert_not_called()

    def test_html_page_never_enters_the_json_branch(self):
        """HTML 以 ``<`` 开头，``_load_json_payload`` 必须原样放行给卡片解析。"""
        self.assertIsNone(mailbox_icloud_url._load_json_payload("<div class='card'></div>"))
        self.assertIsNone(mailbox_icloud_url._load_json_payload(""))
        self.assertEqual(mailbox_icloud_url._load_json_payload('{"code":"no_code"}'), {"code": "no_code"})


class ICloudUrlSplitContractTests(unittest.TestCase):
    """``split_icloud_url_line`` must drop every field after the URL.

    The supplier ships four-part lines (``email----url----account----2fa``).  Keeping the
    tail put it inside ``MailboxAccount.token`` and therefore inside the request URL.
    On ``api798.com`` the tail lands in the ``auth_code`` query value and the server
    answers ``HTTP 403 错误：授权码无效``; a 2026-08-10 probe read that as "channel dead"
    and 33 usable mailboxes were deleted.  These tests pin the parse-boundary fix.
    """

    QUERY_STYLE = (
        "jags-burly4k+oai02@icloud.com----"
        "https://api798.com/latest?email=jags-burly4k%40icloud.com&auth_code=SSS888----"
        "cyc08286688.----U43AD7FV2SAXY2MDO76PSOGO22OUOWLS"
    )
    PATH_STYLE = (
        "slides-loosest-9i+oai02@icloud.com----"
        "https://icloud-api.top/s/MRtNSlokiiLDRYx5HdUczYam78p9R2WO/slides-loosest-9i+oai02@icloud.com----"
        "cyc08286688.----7E6Q5G2G3TG7I5NVU3WI5WRSIWYNKNCK"
    )

    def test_four_field_line_keeps_only_the_first_two_fields(self):
        email, url = mailbox_icloud_url.split_icloud_url_line(self.QUERY_STYLE)
        self.assertEqual(email, "jags-burly4k+oai02@icloud.com")
        self.assertEqual(
            url, "https://api798.com/latest?email=jags-burly4k%40icloud.com&auth_code=SSS888"
        )
        self.assertNotIn("----", url)
        self.assertNotIn("cyc08286688.", url)

    def test_query_style_tail_never_reaches_the_request_url(self):
        """The decisive regression guard: ``auth_code`` must survive intact."""
        from urllib.parse import parse_qsl, urlsplit

        mailbox = mailbox_parsers._parse_icloud_url_line(self.QUERY_STYLE, "pool.txt", 1)
        request_url = mailbox_icloud_url._with_message_limit(mailbox.token, 25)
        query = dict(parse_qsl(urlsplit(request_url).query))

        self.assertEqual(query["auth_code"], "SSS888")
        self.assertNotIn("----", request_url)
        self.assertEqual(query["n"], "25")

    def test_path_style_tail_is_also_dropped(self):
        email, url = mailbox_icloud_url.split_icloud_url_line(self.PATH_STYLE)
        self.assertEqual(email, "slides-loosest-9i+oai02@icloud.com")
        self.assertEqual(
            url,
            "https://icloud-api.top/s/MRtNSlokiiLDRYx5HdUczYam78p9R2WO/"
            "slides-loosest-9i+oai02@icloud.com",
        )
        self.assertNotIn("----", url)

    def test_two_field_line_is_unchanged(self):
        line = "plain@icloud.com----https://mail.example/messages/secret/plain@icloud.com"
        self.assertEqual(
            mailbox_icloud_url.split_icloud_url_line(line),
            ("plain@icloud.com", "https://mail.example/messages/secret/plain@icloud.com"),
        )

    def test_concatenated_records_keep_the_first_record_intact(self):
        """Two records glued on one line: the first must parse, not be corrupted."""
        line = (
            "first@icloud.com----https://icloud-api.top/s/T/first@icloud.com"
            "--------"
            "second@icloud.com----https://icloud-api.top/s/T2/second@icloud.com"
        )
        email, url = mailbox_icloud_url.split_icloud_url_line(line)
        self.assertEqual(email, "first@icloud.com")
        self.assertEqual(url, "https://icloud-api.top/s/T/first@icloud.com")

    def test_three_hyphen_two_field_line_is_unchanged(self):
        line = "second@icloud.com---http://mail.example/messages/secret/second@icloud.com"
        self.assertEqual(
            mailbox_icloud_url.split_icloud_url_line(line),
            ("second@icloud.com", "http://mail.example/messages/secret/second@icloud.com"),
        )

    def test_a_non_http_second_field_is_still_rejected(self):
        self.assertEqual(
            mailbox_icloud_url.split_icloud_url_line("who@icloud.com----notaurl----tail"),
            ("", ""),
        )

    def test_a_non_icloud_domain_is_still_rejected(self):
        self.assertFalse(
            mailbox_icloud_url.is_icloud_url_line("who@gmail.com----https://h/s/T/who@gmail.com")
        )

    def test_pool_loader_drops_the_tail_for_a_four_field_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icloud.txt"
            path.write_text(self.QUERY_STYLE + "\n" + self.PATH_STYLE + "\n", encoding="utf-8")

            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.provider == "icloud_url" for record in records))
        for record in records:
            self.assertNotIn("----", record.token)
            self.assertNotIn("cyc08286688.", record.token)


class LatestMailJsChannelTests(unittest.TestCase):
    """``api798.com`` 的 ``/latest`` 页面：正文藏在 JS 字符串里，卡片分支必然读成 0 封。

    实测背景（2026-09-16）：批次抽中 ``jags-burly4k+oai02@icloud.com``（provider
    ``api798.com``），OpenAI 于 23:48:03 发码，页面显示同一秒的接收时间与越南语主题，
    验证码 ``494652`` 就嵌在 ``var htmlContent = "…"`` 里 —— 而
    ``fetch_icloud_url_messages`` 返回 0 封，轮询 303s 后 ``email_otp_poll_timeout``。
    同批 17 个 ``icloud-api.top`` 邮箱不受影响。
    """

    LATEST_PAGE = (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"><title>最新邮件</title></head><body>"
        "<div class=\"container\"><h1>最新邮件信息</h1>"
        "<div class=\"info\"><div class=\"label\">接收时间：</div>"
        "<div class=\"time\">2026年09月16日 23:48:03 (北京时间)</div></div>"
        "<div class=\"info\"><div class=\"label\">邮件主题：</div>"
        "<div class=\"subject\">Mã xác minh tạm thời của bạn cho ChatGPT</div></div>"
        "<div class=\"info\"><div class=\"label\">邮件内容：</div>"
        "<iframe class=\"email-frame\" id=\"emailFrame\"></iframe></div></div>"
        "<script>setTimeout(function(){ location.reload(); }, 30000);\n"
        "(function() { var frame = document.getElementById(\"emailFrame\");"
        # 正文必须是**完整邮件**：验证码提取器要求验证码附近有 code/verification/chatgpt
        # 之类的上下文标记（否则 `_looks_fake_otp_context` 会把它当样式数字丢掉），
        # 所以这里保留真实邮件里的 "ChatGPT" 标题与 font-size 样式。
        " var htmlContent = \"<html>\\r\\n  <head><title>M\\u00e3 x\\u00e1c minh</title></head>"
        "\\r\\n  <body><p style=\\\"font-size: 16px;\\\">Your login code</p>"
        "\\r\\n  <p>494652</p></body>\\r\\n</html>\";"
        " frame.srcdoc = htmlContent; })();</script></body></html>"
    )
    EMAIL = "jags-burly4k+oai02@icloud.com"
    URL = "https://api798.example/latest?email=jags-burly4k%40icloud.com&auth_code=SSS888"

    def _mailbox(self):
        return MailboxAccount(email=self.EMAIL, provider="icloud_url", token=self.URL)

    def test_latest_page_yields_the_embedded_js_body(self):
        messages = mailbox_icloud_url._latest_mail_js_message(self.LATEST_PAGE, email=self.EMAIL)

        self.assertIsNotNone(messages)
        self.assertEqual(len(messages), 1)
        body = messages[0]["body"]["content"]
        self.assertIn("<html>", body)
        self.assertIn("494652", body)
        # 转义必须被解开，否则验证码会被埋在 \\uXXXX 与 \\r\\n 里
        self.assertNotIn("\\r\\n", body)
        self.assertIn("Mã xác minh", body)

    def test_latest_mail_otp_is_extractable_by_the_shared_extractor(self):
        messages = mailbox_icloud_url._latest_mail_js_message(self.LATEST_PAGE, email=self.EMAIL)

        self.assertEqual(_extract_otp_from_text(messages[0]["body"]["content"]), "494652")

    def test_latest_mail_subject_is_normalized_to_login_code(self):
        """主题必须被改写成 ``… login code``，否则过不了注册泳道的关键词过滤。"""
        messages = mailbox_icloud_url._latest_mail_js_message(self.LATEST_PAGE, email=self.EMAIL)

        self.assertTrue(messages[0]["subject"].endswith("login code"))
        self.assertIn("ChatGPT", messages[0]["subject"])

    def test_latest_mail_received_at_is_beijing_time(self):
        messages = mailbox_icloud_url._latest_mail_js_message(self.LATEST_PAGE, email=self.EMAIL)

        self.assertEqual(messages[0]["receivedDateTime"], "2026-09-16T23:48:03+08:00")
        # 解析得出时刻才会参与 issued_after 比较，而不是落回「0 ⇒ 不比较」
        self.assertEqual(_message_received_ts(messages[0]), 1789573683)

    def test_the_no_mail_page_is_not_mistaken_for_the_latest_layout(self):
        self.assertIsNone(
            mailbox_icloud_url._latest_mail_js_message(
                "<h1>未找到匹配的邮件</h1>", email=self.EMAIL
            )
        )

    def test_a_card_page_is_not_mistaken_for_the_latest_layout(self):
        self.assertIsNone(
            mailbox_icloud_url._latest_mail_js_message(
                "<div class='card'><div class='su'>x</div></div>", email=self.EMAIL
            )
        )
        self.assertIsNone(mailbox_icloud_url._latest_mail_js_message("", email=self.EMAIL))

    def test_markers_without_the_js_body_still_report_the_subject(self):
        """版式对但没有正文时不返回 None —— 否则「读不出」会伪装成「没邮件」。"""
        page = (
            "<h1>最新邮件信息</h1>"
            "<div class=\"label\">接收时间：</div><div class=\"time\">2026年09月16日 23:48:03 (北京时间)</div>"
            "<div class=\"label\">邮件主题：</div><div class=\"subject\">Subject only</div>"
        )
        messages = mailbox_icloud_url._latest_mail_js_message(page, email=self.EMAIL)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["subject"], "Subject only")
        self.assertEqual(messages[0]["body"]["content"], "")

    def test_js_string_escapes_are_decoded(self):
        self.assertEqual(
            mailbox_icloud_url._decode_js_string('a\\"b\\r\\nM\\u00e3\\/c'), 'a"b\r\nMã/c'
        )
        self.assertEqual(mailbox_icloud_url._decode_js_string("plain"), "plain")

    def test_unparseable_time_does_not_invent_a_timestamp(self):
        self.assertEqual(mailbox_icloud_url._latest_mail_received_at("刚刚"), "")
        self.assertEqual(mailbox_icloud_url._latest_mail_received_at(""), "")

    def test_fetch_dispatches_the_latest_layout_before_the_card_parser(self):
        with patch.object(mailbox_icloud_url, "_parse_card_messages") as card, \
             patch.object(mailbox_icloud_url.curl_requests, "get",
                          return_value=_Response(text=self.LATEST_PAGE)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(self._mailbox(), limit=5)

        card.assert_not_called()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["receivedDateTime"], "2026-09-16T23:48:03+08:00")

    def test_snapshot_also_dispatches_the_latest_layout(self):
        with patch.object(mailbox_icloud_url, "_parse_card_messages") as card, \
             patch.object(mailbox_icloud_url.curl_requests, "get",
                          return_value=_Response(text=self.LATEST_PAGE)):
            messages = mailbox_icloud_url.snapshot_icloud_url_messages(self._mailbox(), limit=5)

        card.assert_not_called()
        self.assertEqual(len(messages), 1)

    def test_a_card_page_still_reaches_the_card_parser(self):
        """新分支不许把 ``icloud-api.top`` 的卡片页截胡。"""
        page = (
            "<div class=\"card\"><div class=\"fr\">OpenAI &lt;noreply@openai.com&gt;</div>"
            "<div class=\"su\">你的临时 ChatGPT 登录代码</div>"
            "<div class=\"dt\">Mon, 03 Aug 2026 06:32:17 +0000 (UTC)</div>"
            "<div class=\"bd\">你的临时代码：654321</div></div>"
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get",
                          return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(self._mailbox(), limit=5)

        self.assertEqual(len(messages), 1)
        self.assertIn("654321", messages[0]["body"]["content"])


if __name__ == "__main__":
    unittest.main()
