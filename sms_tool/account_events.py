"""Domain events emitted after account persistence.

Storage publishes facts through this module without importing a mailbox
provider. Provider-specific side effects stay behind the event dispatcher.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def notify_account_deactivated(data: Mapping[str, Any]) -> None:
    """Notify provider history after a terminal account deactivation."""
    provider = str(data.get("mailbox_provider") or "").strip().lower()
    if provider != "remail":
        return
    try:
        from .providers.mailbox_remail import record_dead_remail_account

        record_dead_remail_account(data, reason="account_deactivated")
    except Exception as exc:
        # Persistence has already succeeded; provider history is best effort.
        print(f"[!] Failed to update ReMail dead-account history: {exc}")

