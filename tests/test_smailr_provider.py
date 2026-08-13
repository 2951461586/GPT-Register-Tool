from unittest.mock import patch

from sms_tool.mailbox_parsers import _parse_mailbox_token_file
from sms_tool.providers.smailr_mailbox import SmailrClient, SmailrError, fetch_messages, _normalize_message


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"
        self.text = str(payload)

    def json(self):
        return self._payload


def test_smailr_normalizes_openapi_server_url_and_redacts_errors():
    client = SmailrClient("nm_test_secret", "https://smailr.com/api/v1")
    assert client.base_url == "https://smailr.com"
    assert client._headers()["Authorization"] == "Bearer nm_test_secret"
    with patch("sms_tool.providers.smailr_mailbox.curl_requests.request", return_value=FakeResponse({"error": "nm_test_secret"}, 403)):
        try:
            client.list_mailboxes()
        except SmailrError as exc:
            assert "nm_test_secret" not in str(exc)
            assert "<redacted>" in str(exc)
        else:
            raise AssertionError("expected SmailrError")


def test_smailr_create_and_fetch_nested_responses_and_mail_detail():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return FakeResponse({"data": {"id": "mb-1", "email": "otp@smailr.com"}}, 201)
        if url.endswith("/mails?folder=INBOX&page=1&per_page=25"):
            return FakeResponse({"data": [{"id": "mail-1", "subject": "OpenAI code"}]})
        return FakeResponse({"data": {"id": "mail-1", "body_text": "Your verification code is 729660"}})

    with patch("sms_tool.providers.smailr_mailbox.curl_requests.request", side_effect=request):
        client = SmailrClient("nm_test")
        created = client.create_mailbox("otp")
        assert created["id"] == "mb-1"
        messages = fetch_messages(client, "mb-1", "otp@smailr.com", limit=1)

    assert messages[0]["id"] == "mail-1"
    assert "729660" in messages[0]["body"]["content"]
    assert calls[0][2]["json"] == {"local_part": "otp"}


def test_smailr_mailbox_file_requires_and_preserves_mailbox_id(tmp_path):
    path = tmp_path / "mailboxes.txt"
    path.write_text("smailr://otp@smailr.com---mb-1\n", encoding="utf-8")
    records = _parse_mailbox_token_file(path)
    assert len(records) == 1
    assert records[0].provider == "smailr"
    assert records[0].token == "mb-1"

