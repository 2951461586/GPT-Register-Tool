"""Provider-independent terminal mailbox errors understood by OTP pollers."""

from __future__ import annotations

from enum import Enum
from typing import Any


class MailboxEndpointUnavailableError(RuntimeError):
    """The mailbox endpoint is missing; retry only after a bounded cooldown."""

    code = "mailbox_endpoint_unavailable"

    def __init__(self, status: int = 404):
        self.status = status
        super().__init__(f"{self.code}: HTTP {status}")


class MailboxErrorDisposition(str, Enum):
    RETRY = "retry"
    AUTH_INVALID = "auth_invalid"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"


def mailbox_error_disposition(error: Any) -> MailboxErrorDisposition:
    """Classify one provider failure for every mailbox polling path."""
    if isinstance(error, MailboxEndpointUnavailableError):
        return MailboxErrorDisposition.ENDPOINT_UNAVAILABLE
    code = str(getattr(error, "code", "") or "").strip().lower()
    name = type(error).__name__.lower()
    text = str(error or "").lower()
    if code in {"mailbox_auth_invalid", "remail_credential_invalid"}:
        return MailboxErrorDisposition.AUTH_INVALID
    if "tokenexpired" in name or "invalid_grant" in text:
        return MailboxErrorDisposition.AUTH_INVALID
    if code == "mailbox_endpoint_unavailable":
        return MailboxErrorDisposition.ENDPOINT_UNAVAILABLE
    return MailboxErrorDisposition.RETRY


def is_terminal_mailbox_error(error: Any) -> bool:
    return mailbox_error_disposition(error) is not MailboxErrorDisposition.RETRY


__all__ = [
    "MailboxEndpointUnavailableError",
    "MailboxErrorDisposition",
    "is_terminal_mailbox_error",
    "mailbox_error_disposition",
]
