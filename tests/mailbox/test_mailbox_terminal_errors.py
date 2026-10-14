import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sms_tool import mailbox_quarantine as quarantine
from sms_tool.mailbox_errors import MailboxEndpointUnavailableError
from sms_tool.mailbox_poll import _poll_otp_with_settle
from sms_tool.mailbox_types import MailboxAccount
from sms_tool.providers import mailbox_icloud_url as icloud
from sms_tool.providers.mailbox_graph import MailboxAuthInvalidError


@pytest.fixture
def isolated_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(quarantine, "quarantine_path", lambda: tmp_path / "quarantine.json")


@pytest.mark.parametrize("status", [404, 410])
def test_missing_inbox_stops_polling_and_cools_down(status, monkeypatch, isolated_quarantine):
    mailbox = MailboxAccount(email="test@icloud.com", provider="icloud_url", token="https://mail.example/test")
    request = Mock(return_value=SimpleNamespace(status_code=status, text=""))
    monkeypatch.setattr(icloud, "_request", request)
    for _ in range(2):
        with pytest.raises(MailboxEndpointUnavailableError):
            _poll_otp_with_settle(lambda: icloud.fetch_icloud_url_messages(mailbox), timeout=300)
    assert request.call_count == 1
    # The same credential may be probed again after the bounded cooldown.
    now = quarantine.time.time()
    monkeypatch.setattr(quarantine.time, "time", lambda: now + 301)
    assert not quarantine.is_mailbox_quarantined(mailbox)


def test_invalid_credential_stops_repeated_reads_until_changed(monkeypatch, isolated_quarantine):
    mailbox = MailboxAccount(email="test@icloud.com", provider="icloud_url", token="https://mail.example/test")
    request = Mock(return_value=SimpleNamespace(status_code=200, text="account login failed"))
    monkeypatch.setattr(icloud, "_request", request)
    for _ in range(2):
        with pytest.raises(MailboxAuthInvalidError):
            icloud.fetch_icloud_url_messages(mailbox)
    assert request.call_count == 1
    mailbox.token = "https://mail.example/repaired"
    assert not quarantine.is_mailbox_quarantined(mailbox)


def test_malformed_retry_after_keeps_quarantine_instead_of_raising(isolated_quarantine):
    """A hand-edited/corrupt entry must fail closed, not break every mailbox read."""
    path = quarantine.quarantine_path()
    path.write_text(json.dumps({
        "version": 1,
        "entries": {"abc": {"code": "mailbox_auth_invalid", "retry_after": "soon"}},
    }), encoding="utf-8")
    entries = quarantine._read(path)
    assert "abc" in entries


def test_transient_server_error_is_not_quarantined(monkeypatch, isolated_quarantine):
    mailbox = MailboxAccount(email="test@icloud.com", provider="icloud_url", token="https://mail.example/test")
    monkeypatch.setattr(icloud, "_request", Mock(return_value=SimpleNamespace(status_code=503, text="")))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        icloud.fetch_icloud_url_messages(mailbox)
    assert not quarantine.is_mailbox_quarantined(mailbox)


@pytest.mark.parametrize("error", [MailboxEndpointUnavailableError(), MailboxAuthInvalidError()])
def test_browser_poll_does_not_swallow_terminal_provider_errors(error, monkeypatch):
    from sms_tool.registration_drivers.browser_flow import flow_steps

    service = Mock()
    service.poll_otp.side_effect = error
    monkeypatch.setattr(flow_steps.dom_fields, "_browser_heartbeat", lambda browser, page: page)
    with pytest.raises(type(error)):
        flow_steps._poll_browser_otp(
            service, Mock(), browser=Mock(), page=Mock(), driver_name="camoufox",
            subject_keyword="code", timeout=300, issued_after_unix=0,
            proxy=None, excluded_otps=set(),
        )
    service.poll_otp.assert_called_once()
