"""Injected mailbox application service over provider adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any

from .config import ConfigInput, RuntimeConfig, resolve_runtime_config, runtime_config_scope
from .mailbox_strategies import DEFAULT_MAILBOX_PROVIDERS, MailboxProviderRegistry
from .mailbox_errors import MailboxEndpointUnavailableError
from .mailbox_quarantine import (
    raise_if_mailbox_quarantined, record_mailbox_auth_invalid,
    record_mailbox_endpoint_unavailable,
)


_LOGGER = logging.getLogger(__name__)


@contextmanager
def _mailbox_access(mailbox: Any):
    raise_if_mailbox_quarantined(mailbox)
    try:
        yield
    except MailboxEndpointUnavailableError:
        record_mailbox_endpoint_unavailable(mailbox)
        raise
    except Exception as exc:
        if getattr(exc, "code", "") == "mailbox_auth_invalid":
            record_mailbox_auth_invalid(mailbox)
        raise


@dataclass(frozen=True)
class MailboxService:
    config: RuntimeConfig
    providers: MailboxProviderRegistry

    @classmethod
    def create(
        cls,
        config: ConfigInput = None,
        providers: MailboxProviderRegistry | None = None,
    ) -> "MailboxService":
        return cls(resolve_runtime_config(config, workflow="mailbox"), providers or DEFAULT_MAILBOX_PROVIDERS)

    def fetch_messages(
        self,
        mailbox: Any,
        *,
        limit: int = 25,
        proxy: str | None = None,
        include_body: bool = False,
    ) -> list[Any]:
        from .mailbox import _email_cfg, _resolve_mailbox_proxy

        with runtime_config_scope(self.config, workflow="mailbox"), _mailbox_access(mailbox):
            config = _email_cfg(self.config)
            resolved_proxy = _resolve_mailbox_proxy(proxy, self.config)
            adapter = self.providers.resolve_fetcher(mailbox, config)
            if adapter is None:
                raise RuntimeError("no mailbox message fetcher resolved")
            return adapter.fetch_messages(
                mailbox,
                limit=limit,
                proxy=resolved_proxy,
                include_body=include_body,
                email_cfg=config,
                runtime_config=self.config,
                registry=self.providers,
            )

    def poll_otp(
        self,
        mailbox: Any,
        *,
        subject_keyword: str = "",
        timeout: int = 300,
        issued_after_unix: int = 0,
        proxy: str | None = None,
        excluded_otps: set[str] | None = None,
    ) -> str | None:
        from .mailbox import (
            _email_cfg,
            _mailbox_proxy_candidates,
            _provider_otp_issued_after,
            _resolve_mailbox_proxy,
        )

        with runtime_config_scope(self.config, workflow="mailbox"), _mailbox_access(mailbox):
            config = _email_cfg(self.config)
            issued_after_unix = _provider_otp_issued_after(mailbox, issued_after_unix, self.config)
            proxy_candidates = _mailbox_proxy_candidates(proxy, self.config)
            resolved_proxy = proxy_candidates[0] if proxy_candidates else _resolve_mailbox_proxy(proxy, self.config)
            adapter = self.providers.resolve_poller(mailbox, config)
            if adapter is None:
                raise RuntimeError("no mailbox OTP poller resolved")
            # This is the path registration actually uses -- `mailbox._poll_email_otp`
            # is NOT on it (the handler passes `poll_otp_fn=self.poll_otp`), so an
            # observability hook placed there never fires for a stuck run.
            # `otp_issued_after_unix` is logged because it is the whole story when
            # a real OTP mail is rejected as "too old": candidates with a timestamp
            # below it are dropped by `_email_otp_candidate`, and 2026-09-11 showed
            # OpenAI stamping the mail BEFORE the local send request, so a correct
            # code sat in the inbox while the poll drained the full 300s window.
            _LOGGER.info(
                "Mailbox OTP poll dispatch provider=%s adapter=%s timeout=%ss issued_after=%s proxy_candidates=%d",
                str(getattr(mailbox, "provider", "") or "") or "unknown",
                type(adapter).__name__,
                timeout,
                int(issued_after_unix or 0),
                len(proxy_candidates or []),
                extra={
                    "event": "mailbox_otp_poll_dispatch",
                    "provider": str(getattr(mailbox, "provider", "") or ""),
                    "poller": type(adapter).__name__,
                    "otp_timeout_s": timeout,
                    "otp_issued_after_unix": int(issued_after_unix or 0),
                    "proxy_candidate_count": len(proxy_candidates or []),
                },
            )
            return adapter.poll_otp(
                mailbox,
                subject_keyword=subject_keyword,
                timeout=timeout,
                issued_after_unix=issued_after_unix,
                proxy=resolved_proxy,
                proxy_candidates=proxy_candidates,
                excluded_otps=excluded_otps,
                runtime_config=self.config,
                registry=self.providers,
            )
