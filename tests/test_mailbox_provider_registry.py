import pytest

from sms_tool.config import RuntimeConfig
from sms_tool.mailbox_service import MailboxService
from sms_tool.mailbox_strategies import (
    FunctionMailboxProviderAdapter,
    MailboxProviderRegistry,
    MailboxProviderResolutionError,
)
from sms_tool.mailbox_types import MailboxAccount


def _config():
    return RuntimeConfig.from_mapping({
        "chatgpt": {},
        "email_registration": {"otp_poll_interval": 1},
        "protocol_payments": {"matrix": {"cells": []}},
    })


def test_injected_mailbox_registry_routes_fetch_and_poll_without_global_registration():
    registry = MailboxProviderRegistry()
    registry.register(FunctionMailboxProviderAdapter(
        "fake",
        lambda mailbox, config: mailbox.provider == "fake",
        lambda mailbox, **kwargs: [{"id": "message-1"}],
        lambda mailbox, **kwargs: "123456",
    ))
    service = MailboxService.create(_config(), registry)
    mailbox = MailboxAccount("user@example.com", provider="fake")

    assert service.fetch_messages(mailbox) == [{"id": "message-1"}]
    assert service.poll_otp(mailbox) == "123456"
    assert registry.names() == ("fake",)


def test_frozen_registry_rejects_runtime_mutation():
    registry = MailboxProviderRegistry().freeze()
    with pytest.raises(RuntimeError, match="immutable"):
        registry.register(FunctionMailboxProviderAdapter("fake", lambda *_: True))


def test_matcher_failure_is_typed_instead_of_silently_skipped():
    registry = MailboxProviderRegistry()
    registry.register(FunctionMailboxProviderAdapter(
        "broken",
        lambda *_: (_ for _ in ()).throw(ValueError("secret provider detail")),
        lambda *_args, **_kwargs: [],
    ))
    with pytest.raises(MailboxProviderResolutionError, match="matcher failed: broken: ValueError"):
        registry.resolve_fetcher(MailboxAccount("user@example.com"), {})


def test_mailbox_account_repr_hides_provider_credentials():
    mailbox = MailboxAccount(
        "user@example.com",
        password="password-secret",
        refresh_token="rt_secret",
        token="provider-secret",
    )
    value = repr(mailbox)
    assert "password-secret" not in value
    assert "rt_secret" not in value
    assert "provider-secret" not in value


def test_chongzhi_polling_uses_injected_registry_adapter():
    called = {}
    def fake_poll(mailbox, **kwargs):
        called.update(kwargs)
        return "654321"
    registry = MailboxProviderRegistry()
    registry.register(FunctionMailboxProviderAdapter(
        "chongzhi", lambda mailbox, _config: mailbox.provider == "chongzhi",
        otp_poller=fake_poll,
    ))
    service = MailboxService.create(_config(), registry)
    mailbox = MailboxAccount("user@example.com", password="secret", provider="chongzhi")
    assert service.poll_otp(mailbox, timeout=17) == "654321"
    assert called["timeout"] == 17
