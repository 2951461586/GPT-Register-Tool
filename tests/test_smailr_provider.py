from unittest.mock import patch

from curl_cffi.requests.exceptions import SSLError

from sms_tool import mailbox_smailr
from sms_tool.mailbox_parsers import _parse_mailbox_token_file
from sms_tool.providers.smailr_mailbox import SmailrClient, SmailrError, fetch_messages, poll_otp, _normalize_message


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


def test_smailr_fetches_detail_when_list_body_is_only_a_preview():
    calls = []
    preview = "ChatGPT verification message".ljust(200, ".")

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "/mailboxes/mb-1/mails?" in url:
            return FakeResponse({"data": [{
                "id": "mail-1",
                "subject": "ChatGPT verification code",
                "body_text": preview,
                "from_addr": "noreply@tm.openai.com",
                "to_addrs": ["otp@smailr.com"],
            }]})
        return FakeResponse({"data": {
            "id": "mail-1",
            "body_html": "Your verification code is 123456",
        }})

    with patch("sms_tool.providers.smailr_mailbox.curl_requests.request", side_effect=request):
        messages = fetch_messages(SmailrClient("nm_test"), "mb-1", "otp@smailr.com", limit=1)

    assert len(calls) == 2
    assert "123456" in messages[0]["body"]["content"]
    assert messages[0]["from"]["emailAddress"]["address"] == "noreply@tm.openai.com"
    assert messages[0]["toRecipients"][0]["emailAddress"]["address"] == "otp@smailr.com"


def test_smailr_poll_otp_uses_shared_mailbox_poll_module():
    client = SmailrClient("nm_test")
    with patch(
        "sms_tool.mailbox_poll._poll_otp_with_settle",
        return_value="123456",
    ) as shared_poll:
        result = poll_otp(client, "mb-1", "otp@smailr.com", timeout=30)

    assert result == "123456"
    shared_poll.assert_called_once()


def test_smailr_poll_otp_falls_back_when_provider_subject_is_mojibake():
    client = SmailrClient("nm_test")
    message = {
        "id": "mail-1",
        "receivedDateTime": "2026-08-15T00:00:00Z",
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
        "subject": "ChatGPT garbled-subject",
        "bodyPreview": "Your verification code is 123456",
        "body": {"content": "Your verification code is 123456"},
        "toRecipients": [{"emailAddress": {"address": "otp@smailr.com"}}],
    }

    def settle(fetch_candidate, **_kwargs):
        candidate = fetch_candidate()
        return candidate.get("otp") if candidate else None

    with patch(
        "sms_tool.providers.smailr_mailbox.fetch_messages",
        return_value=[message],
    ), patch(
        "sms_tool.mailbox_poll._poll_otp_with_settle",
        side_effect=settle,
    ):
        result = poll_otp(
            client,
            "mb-1",
            "otp@smailr.com",
            subject_keyword="verification code",
            timeout=30,
        )

    assert result == "123456"


def test_smailr_retries_tls_handshake_failure_before_create():
    tls_error = SSLError("SSL_connect closed abruptly", 35)
    response = FakeResponse({"data": {"id": "mb-1", "email": "otp@smailr.com"}}, 201)

    with patch(
        "sms_tool.providers.smailr_mailbox.curl_requests.request",
        side_effect=[tls_error, response],
    ) as request, patch("sms_tool.providers.smailr_mailbox.time.sleep") as sleep:
        client = SmailrClient(
            "nm_test",
            retry_attempts=3,
            retry_backoff_seconds=0.25,
        )
        created = client.create_mailbox("otp")

    assert created["id"] == "mb-1"
    assert request.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_smailr_does_not_retry_permanent_http_failure():
    with patch(
        "sms_tool.providers.smailr_mailbox.curl_requests.request",
        return_value=FakeResponse({"error": "invalid api key"}, 401),
    ) as request, patch("sms_tool.providers.smailr_mailbox.time.sleep") as sleep:
        client = SmailrClient(
            "nm_test",
            retry_attempts=3,
            retry_backoff_seconds=0.25,
        )
        try:
            client.create_mailbox("otp")
        except SmailrError as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("expected SmailrError")

    assert request.call_count == 1
    sleep.assert_not_called()


def test_smailr_default_domain_uses_documented_optional_domain_id():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse({"data": {"id": "mb-1", "email": "otp@smailr.com"}}, 201)

    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={"default_domain": "smailr.com"}), \
         patch("sms_tool.providers.smailr_mailbox.curl_requests.request", side_effect=request):
        accounts = mailbox_smailr.create_smailr_mailboxes(
            1,
            local_part="otp",
            domain="smailr.com",
            api_key="nm_test",
            base_url="https://smailr.com",
        )

    assert accounts[0].email == "otp@smailr.com"
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", "https://smailr.com/api/v1/mailboxes")
    assert calls[0][2]["json"] == {"local_part": "otp"}


def test_smailr_non_default_domain_uses_configured_domain_id():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse({"data": {"id": "mb-1", "email": "otp@loc.cc"}}, 201)

    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={
        "default_domain": "smailr.com",
        "domain_ids": {"loc.cc": "domain-loc"},
    }), patch("sms_tool.providers.smailr_mailbox.curl_requests.request", side_effect=request):
        accounts = mailbox_smailr.create_smailr_mailboxes(
            1,
            local_part="otp",
            domain="loc.cc",
            api_key="nm_test",
            base_url="https://smailr.com",
        )

    assert accounts[0].email == "otp@loc.cc"
    assert calls[0][2]["json"] == {"local_part": "otp", "domain_id": "domain-loc"}


def test_smailr_non_default_domain_requires_configured_domain_id_when_reuse_is_disabled():
    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={
        "default_domain": "smailr.com",
        "reuse_existing_on_level_error": False,
    }):
        try:
            mailbox_smailr.create_smailr_mailboxes(
                1,
                domain="loc.cc",
                api_key="nm_test",
            )
        except RuntimeError as exc:
            assert "domain_ids.loc.cc" in str(exc)
        else:
            raise AssertionError("expected missing Smailr domain ID to be rejected")


def test_smailr_reuses_empty_existing_mailbox_when_server_default_requires_higher_level():
    level_error = SmailrError(
        "domain level restricted",
        status_code=403,
        body={"error": "该域名要求用户等级 4 及以上，您当前等级为 1"},
    )
    existing = {
        "id": "mb-existing",
        "address": "available@smailr.com",
        "mail_count": 0,
        "is_archived": False,
        "receiveEnabled": True,
    }

    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={
        "default_domain": "smailr.com",
        "reuse_existing_on_level_error": True,
    }), patch(
        "sms_tool.providers.smailr_mailbox.SmailrClient.create_mailbox",
        side_effect=level_error,
    ), patch(
        "sms_tool.providers.smailr_mailbox.SmailrClient.list_mailboxes",
        return_value=[existing],
    ), patch("sms_tool.storage.get_account_record", return_value={}):
        accounts = mailbox_smailr.create_smailr_mailboxes(
            1,
            domain="smailr.com",
            api_key="nm_test",
        )

    assert len(accounts) == 1
    assert accounts[0].email == "available@smailr.com"
    assert accounts[0].token == "mb-existing"
    assert '"reused_existing": true' in accounts[0].source


def test_smailr_reuses_requested_non_default_domain_without_domain_id():
    existing = {
        "id": "mb-existing",
        "address": "available@smailr.com",
        "mail_count": 0,
        "is_archived": False,
        "receiveEnabled": True,
    }

    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={
        "default_domain": "nodeloc.cc",
        "reuse_existing_on_level_error": True,
    }), patch(
        "sms_tool.providers.smailr_mailbox.SmailrClient.create_mailbox",
    ) as create, patch(
        "sms_tool.providers.smailr_mailbox.SmailrClient.list_mailboxes",
        return_value=[existing],
    ), patch("sms_tool.storage.get_account_record", return_value={}):
        accounts = mailbox_smailr.create_smailr_mailboxes(
            1,
            domain="smailr.com",
            api_key="nm_test",
        )

    create.assert_not_called()
    assert accounts[0].email == "available@smailr.com"


def test_smailr_rejects_domains_outside_lv1_provider_set():
    with patch.object(mailbox_smailr, "_smailr_cfg", return_value={"default_domain": "smailr.com"}):
        try:
            mailbox_smailr.create_smailr_mailboxes(
                1,
                domain="example.com",
                api_key="nm_test",
            )
        except ValueError as exc:
            assert "smailr.com, loc.cc, mail.nodeloc.cc, nodeloc.cc" in str(exc)
        else:
            raise AssertionError("expected unsupported Smailr domain to be rejected")


def test_smailr_mailbox_file_requires_and_preserves_mailbox_id(tmp_path):
    path = tmp_path / "mailboxes.txt"
    path.write_text("smailr://otp@smailr.com---mb-1\n", encoding="utf-8")
    records = _parse_mailbox_token_file(path)
    assert len(records) == 1
    assert records[0].provider == "smailr"
    assert records[0].token == "mb-1"
