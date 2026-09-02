import pytest

from sms_tool.config import RuntimeConfig
from sms_tool.mailbox_service import MailboxService
from sms_tool.mailbox_strategies import (
    DEFAULT_MAILBOX_PROVIDERS,
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


def test_graph_is_registered_as_a_fallback_not_as_an_exclusion_list():
    """`_graph_matcher` used to enumerate every other provider by name.

    Adding a provider meant remembering to edit that set, and forgetting was
    silent: Graph claimed the mailbox and the new provider never ran. The
    catch-all is now expressed as `fallback=True` instead.
    """
    registry = DEFAULT_MAILBOX_PROVIDERS.clone()
    graph = next(a for a in registry._adapters if a.name == "graph_api")
    assert graph.fallback is True
    assert graph.matches(MailboxAccount("user@example.com", provider="cfworker"), {}) is True


def test_a_newly_registered_provider_wins_without_touching_graph():
    registry = DEFAULT_MAILBOX_PROVIDERS.clone()
    registry.register_fetcher(
        "brand_new",
        lambda mailbox, _config: mailbox.provider == "brand_new",
        lambda mailbox, **_kwargs: ["from-brand-new"],
    )
    resolved = registry.resolve_fetcher(
        MailboxAccount("user@example.com", provider="brand_new"), {})
    assert resolved is not None
    assert resolved.name == "brand_new"


def test_graph_still_catches_providers_nobody_claims():
    registry = DEFAULT_MAILBOX_PROVIDERS.clone()
    resolved = registry.resolve_fetcher(
        MailboxAccount("user@example.com", provider="nobody_implements_this"), {})
    assert resolved is not None
    assert resolved.name == "graph_api"


def test_a_fallback_never_wins_just_because_it_was_registered_first():
    registry = MailboxProviderRegistry()
    registry.register_fetcher("catch_all", lambda *_: True, lambda *_a, **_k: ["fallback"],
                              fallback=True)
    registry.register_fetcher("specific", lambda *_: True, lambda *_a, **_k: ["specific"])
    resolved = registry.resolve_fetcher(MailboxAccount("user@example.com"), {})
    assert resolved is not None
    assert resolved.name == "specific"


def test_the_real_graph_adapter_loses_when_registered_first():
    """The fallback flag, not registration order, is what keeps Graph last.

    Graph happens to be registered last today, so order alone would produce the
    right answer by accident and `fallback=True` would be decoration. Register
    it first here: if resolution ignores the flag, Graph swallows the mailbox.
    """
    graph = next(a for a in DEFAULT_MAILBOX_PROVIDERS._adapters if a.name == "graph_api")
    registry = MailboxProviderRegistry()
    registry.register(graph)
    registry.register_fetcher(
        "cfworker",
        lambda mailbox, _config: mailbox.provider == "cfworker",
        lambda mailbox, **_kwargs: ["from-cfworker"],
    )
    resolved = registry.resolve_fetcher(
        MailboxAccount("user@example.com", provider="cfworker"), {})
    assert resolved is not None
    assert resolved.name == "cfworker"


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
