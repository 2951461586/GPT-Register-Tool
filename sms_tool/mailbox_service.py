"""Injected mailbox application service over provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ConfigInput, RuntimeConfig, resolve_runtime_config, runtime_config_scope
from .mailbox_strategies import DEFAULT_MAILBOX_PROVIDERS, MailboxProviderRegistry


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

        with runtime_config_scope(self.config, workflow="mailbox"):
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
            _provider_otp_issued_after,
            _resolve_mailbox_proxy,
        )

        with runtime_config_scope(self.config, workflow="mailbox"):
            config = _email_cfg(self.config)
            issued_after_unix = _provider_otp_issued_after(mailbox, issued_after_unix, self.config)
            resolved_proxy = _resolve_mailbox_proxy(proxy, self.config)
            adapter = self.providers.resolve_poller(mailbox, config)
            if adapter is None:
                raise RuntimeError("no mailbox OTP poller resolved")
            return adapter.poll_otp(
                mailbox,
                subject_keyword=subject_keyword,
                timeout=timeout,
                issued_after_unix=issued_after_unix,
                proxy=resolved_proxy,
                excluded_otps=excluded_otps,
                runtime_config=self.config,
                registry=self.providers,
            )
