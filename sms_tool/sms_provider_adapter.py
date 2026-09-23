"""Common SMS provider adapter contract used by phone verification flows."""

from __future__ import annotations

from abc import ABC
from typing import Optional

from .sms_providers import DEFAULT_PROVIDER


class SmsProviderAdapter(ABC):
    """Small lifecycle surface shared by rentable-number providers."""

    provider_key = DEFAULT_PROVIDER

    def __init__(self, slot):
        self.slot = slot

    @property
    def provider(self) -> str:
        return str(getattr(self.slot, "provider", "") or self.provider_key).strip() or self.provider_key

    def prepare(self) -> bool:
        return True

    def wait_code(self) -> Optional[str]:
        raise NotImplementedError

    def complete(self) -> None:
        return None

    def cancel(self) -> None:
        return None


def provider_name(slot) -> str:
    """Provider key carried by ``slot``, defaulting to the registry default.

    A slot built without an explicit provider is a rental slot for the default
    provider -- the removed static mode used to report ``"legacy"`` here, which
    routed it to an adapter whose ``complete``/``cancel`` were no-ops.
    """
    return str(getattr(slot, "provider", "") or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
