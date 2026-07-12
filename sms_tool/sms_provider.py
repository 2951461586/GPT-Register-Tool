"""Common SMS provider adapter contract used by phone verification flows."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Optional


@dataclass
class SmsProviderResult:
    ok: bool
    provider: str = ""
    activation_id: str = ""
    phone: str = ""
    code: str = ""
    error: str = ""


class SmsProviderAdapter(ABC):
    """Small lifecycle surface shared by static SMS URLs and rental providers."""

    provider_key = "legacy"

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
    return str(getattr(slot, "provider", "") or "legacy").strip().lower() or "legacy"
