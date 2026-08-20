"""Mailbox provider strategy registry.

Replaces the if-elif chains in ``_fetch_mailbox_messages`` and
``_poll_email_otp`` with a dispatch table (provider -> handler).

Each provider module exposes a ``ProviderStrategy`` dataclass with:
  * ``can_handle(mailbox, cfg) -> bool``
  * ``fetch_messages(mailbox, *, limit, proxy, email_cfg, ...) -> list``
  * ``poll_otp(mailbox, *, subject_keyword, timeout, issued_after_unix, proxy, ...) -> Optional[str]``

New providers register themselves by appending to ``MESSAGE_FETCHERS`` and
``OTP_POLLERS`` lists in their own module body — no central if-elif grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, TypeVar

# ── Type aliases ────────────────────────────────────────────────────────────────

MailboxT = TypeVar("MailboxT")
MessageFetcher = Callable[[MailboxT], list[Any]]
OtpPoller = Callable[[MailboxT], Optional[str]]


class MailboxProviderAdapter(Protocol):
    name: str

    def matches(self, mailbox: Any, config: Mapping[str, Any]) -> bool: ...

    def fetch_messages(self, mailbox: Any, **kwargs: Any) -> list[Any]: ...

    def poll_otp(self, mailbox: Any, **kwargs: Any) -> Optional[str]: ...

    @property
    def capabilities(self) -> frozenset[str]: ...


class MailboxProviderResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FunctionMailboxProviderAdapter:
    name: str
    matcher: Callable[[Any, Mapping[str, Any]], bool]
    message_fetcher: MessageFetcher | None = None
    otp_poller: OtpPoller | None = None

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(item for item, value in (("fetch", self.message_fetcher), ("poll", self.otp_poller)) if value is not None)

    def matches(self, mailbox: Any, config: Mapping[str, Any]) -> bool:
        return bool(self.matcher(mailbox, config))

    def fetch_messages(self, mailbox: Any, **kwargs: Any) -> list[Any]:
        if self.message_fetcher is None:
            raise NotImplementedError(f"{self.name} does not support message fetch")
        return self.message_fetcher(mailbox, **kwargs)

    def poll_otp(self, mailbox: Any, **kwargs: Any) -> Optional[str]:
        if self.otp_poller is None:
            raise NotImplementedError(f"{self.name} does not support OTP polling")
        return self.otp_poller(mailbox, **kwargs)


class MailboxProviderRegistry:
    def __init__(self) -> None:
        self._adapters: list[MailboxProviderAdapter] = []
        self._frozen = False

    def register(self, adapter: FunctionMailboxProviderAdapter) -> None:
        if self._frozen:
            raise RuntimeError("mailbox provider registry is immutable")
        self._adapters = [item for item in self._adapters if item.name != adapter.name]
        self._adapters.append(adapter)

    def freeze(self) -> "MailboxProviderRegistry":
        self._frozen = True
        return self

    def clone(self) -> "MailboxProviderRegistry":
        registry = MailboxProviderRegistry()
        registry._adapters = list(self._adapters)
        return registry

    def register_fetcher(self, name: str, matcher: Callable[..., bool], fetcher: MessageFetcher) -> None:
        existing = self._find(name)
        self.register(FunctionMailboxProviderAdapter(name, matcher, fetcher, existing.otp_poller if existing else None))

    def register_poller(self, name: str, matcher: Callable[..., bool], poller: OtpPoller) -> None:
        existing = self._find(name)
        self.register(FunctionMailboxProviderAdapter(name, matcher, existing.message_fetcher if existing else None, poller))

    def resolve_fetcher(self, mailbox: Any, config: Mapping[str, Any]) -> FunctionMailboxProviderAdapter | None:
        return self._resolve(mailbox, config, require="fetch")

    def resolve_poller(self, mailbox: Any, config: Mapping[str, Any]) -> FunctionMailboxProviderAdapter | None:
        return self._resolve(mailbox, config, require="poll")

    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)

    def _find(self, name: str) -> FunctionMailboxProviderAdapter | None:
        return next((item for item in self._adapters if item.name == name), None)

    def _resolve(self, mailbox: Any, config: Mapping[str, Any], *, require: str) -> FunctionMailboxProviderAdapter | None:
        for adapter in self._adapters:
            supported = adapter.message_fetcher is not None if require == "fetch" else adapter.otp_poller is not None
            if supported:
                try:
                    if adapter.matches(mailbox, config):
                        return adapter
                except Exception as exc:
                    raise MailboxProviderResolutionError(
                        f"mailbox provider matcher failed: {adapter.name}: {type(exc).__name__}"
                    ) from exc
        return None

# ── Strategy lists ─────────────────────────────────────────────────────────────

# Each entry is a tuple: (name, matcher, fetcher)
# matcher: (mailbox, cfg) -> bool  — does this strategy handle the mailbox?
# fetcher: (mailbox, **kwargs) -> list[str]
DEFAULT_MAILBOX_PROVIDERS = MailboxProviderRegistry()

# Each entry is a tuple: (name, matcher, poller)
# poller: (mailbox, **kwargs) -> Optional[str]


# ── Registration helpers ──────────────────────────────────────────────────────

def register_message_fetcher(
    name: str,
    matcher: Callable[..., bool],
    fetcher: MessageFetcher,
) -> None:
    """Register a provider-specific message fetcher."""
    DEFAULT_MAILBOX_PROVIDERS.register_fetcher(name, matcher, fetcher)


def register_otp_poller(
    name: str,
    matcher: Callable[..., bool],
    poller: OtpPoller,
) -> None:
    """Register a provider-specific OTP poller."""
    DEFAULT_MAILBOX_PROVIDERS.register_poller(name, matcher, poller)


# ── Dispatch ──────────────────────────────────────────────────────────────────

def resolve_message_fetcher(
    mailbox: Any,
    cfg: Mapping[str, Any],
    registry: MailboxProviderRegistry | None = None,
) -> Optional[MessageFetcher]:
    """Return the first registered fetcher that matches *mailbox*, or None."""
    adapter = (registry or DEFAULT_MAILBOX_PROVIDERS).resolve_fetcher(mailbox, cfg)
    return adapter.fetch_messages if adapter else None


def resolve_otp_poller(
    mailbox: Any,
    cfg: Mapping[str, Any],
    registry: MailboxProviderRegistry | None = None,
) -> Optional[OtpPoller]:
    """Return the first registered OTP poller that matches *mailbox*, or None."""
    adapter = (registry or DEFAULT_MAILBOX_PROVIDERS).resolve_poller(mailbox, cfg)
    return adapter.poll_otp if adapter else None


# ── Built-in default: Microsoft Graph API ──────────────────────────────────────


def _graph_matcher(mailbox: Any, cfg: dict) -> bool:
    """Default matcher — only match if no provider-specific strategy applies."""
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    if provider in {"cfworker", "remail", "smailr", "icloud", "icloud_url", "gmail", "chongzhi"}:
        return False
    return True  # Catch-all for plain Graph/IMAP mailboxes


def _graph_fetch_messages(
    mailbox: Any,
    *,
    limit: int = 25,
    proxy: str | None = None,
    include_body: bool = False,
    email_cfg: dict | None = None,
    _fetch_mailbox_messages_local: Callable | None = None,
    **kwargs: Any,
) -> list:
    """Default Microsoft Graph API fetcher (delegates to existing local impl)."""
    if _fetch_mailbox_messages_local is None:
        from .mailbox import _fetch_mailbox_messages_local
    return _fetch_mailbox_messages_local(mailbox, limit=limit, proxy=proxy)


def _graph_poll_otp(
    mailbox: Any,
    *,
    subject_keyword: str = "",
    timeout: int = 300,
    issued_after_unix: int = 0,
    proxy: str | None = None,
    excluded_otps: Any = None,
    **kwargs: Any,
) -> Optional[str]:
    """Graph API fallback OTP poller using settle-stability detection."""
    # Import here to avoid a hard dependency at strategy-module load time.
    from .mailbox import (
        _latest_email_otp_candidate,
        _candidate_is_newer,
        _otp_poll_interval,
        _email_otp_settle_seconds,
    )
    from .mailbox_poll import _poll_otp_with_settle
    from .mailbox import MailboxTokenExpiredError

    keyword = (subject_keyword or "").lower()

    def _fetch_candidate():
        return _latest_email_otp_candidate(
            mailbox, keyword=keyword,
            issued_after_unix=issued_after_unix, proxy=proxy,
        )

    return _poll_otp_with_settle(
        _fetch_candidate,
        timeout=timeout,
        interval=_otp_poll_interval(),
        settle_seconds=_email_otp_settle_seconds(),
        excluded_otps=excluded_otps,
        log_prefix='graph poll',
        is_newer=_candidate_is_newer,
        reraise=(MailboxTokenExpiredError,),
    )


def _chongzhi_matcher(mailbox: Any, cfg: Mapping[str, Any]) -> bool:
    from .mailbox_chongzhi import chongzhi_enabled
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    return provider == "chongzhi" or (
        chongzhi_enabled(dict(cfg)) and bool(str(getattr(mailbox, "password", "") or "").strip())
    )


def _chongzhi_poll_otp(mailbox: Any, **kwargs: Any) -> Optional[str]:
    from .mailbox import _poll_chongzhi_otp
    email = str(getattr(mailbox, "email", "") or "").strip()
    password = str(getattr(mailbox, "password", "") or "").strip()
    if not email or not password:
        raise ValueError("chongzhi mailbox requires email and password")
    return _poll_chongzhi_otp(
        mailbox, email=email, password=password,
        subject_keyword=str(kwargs.get("subject_keyword") or ""),
        timeout=int(kwargs.get("timeout") or 300),
        issued_after_unix=int(kwargs.get("issued_after_unix") or 0),
        proxy=kwargs.get("proxy"),
    )


register_otp_poller("chongzhi", _chongzhi_matcher, _chongzhi_poll_otp)


# Register Graph API as the final fallback
register_message_fetcher("graph_api", _graph_matcher, _graph_fetch_messages)
register_otp_poller("graph_api", _graph_matcher, _graph_poll_otp)
