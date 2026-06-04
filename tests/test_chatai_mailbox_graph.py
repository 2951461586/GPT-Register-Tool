import unittest
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module
from sms_tool.mailbox import MailboxAccount


class FakeTokenResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"access_token": "access-token"}


class FakeMessagesResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"value": []}


class ChataiMailboxGraphTests(unittest.TestCase):
    def test_graph_mailbox_refresh_and_fetch_use_requested_proxy(self):
        mailbox = MailboxAccount(
            email="user@hotmail.com",
            refresh_token="refresh-token",
            token="8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2",
            provider="chatai",
        )

        with patch.object(mailbox_module, "_email_cfg", return_value={}), \
             patch.object(mailbox_module.curl_requests, "post", return_value=FakeTokenResponse()) as post, \
             patch.object(mailbox_module.curl_requests, "get", return_value=FakeMessagesResponse()) as get:
            messages = mailbox_module._fetch_mailbox_messages(
                mailbox,
                limit=5,
                proxy="socks5h://127.0.0.1:7897",
            )

        self.assertEqual(messages, [])
        self.assertEqual(post.call_args.kwargs["proxies"], {
            "http": "socks5h://127.0.0.1:7897",
            "https": "socks5h://127.0.0.1:7897",
        })
        self.assertEqual(get.call_args.kwargs["proxies"], {
            "http": "socks5h://127.0.0.1:7897",
            "https": "socks5h://127.0.0.1:7897",
        })
        self.assertEqual(
            post.call_args.kwargs["data"]["scope"],
            "https://graph.microsoft.com/.default offline_access",
        )


if __name__ == "__main__":
    unittest.main()
