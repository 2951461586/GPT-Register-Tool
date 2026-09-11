"""Account-removal rules shared by operator cleanup and desktop tooling.

Only terminal account states are removable. Transport failures and other
unknown probe results stay in the account pool for a later recheck.
``token_revoked`` is the explicit 掉号 verdict written by
``account_recovery._persist_token_revoked_drop`` after the recovery chain
confirmed there is no relogin material -- without it here those rows were
permanently dead yet survived every cleanup pass.
"""

from __future__ import annotations

from typing import Any, Iterable


_TERMINAL_STATUSES = {
    "account_deactivated",
    "deactivated",
    "dropped",
    "token_revoked",
}


def account_cleanup_reason(account: dict[str, Any]) -> str:
    """Return a terminal removal reason, or ``""`` when the row is retained."""
    if not isinstance(account, dict):
        return ""
    status = str(account.get("status") or "").strip().lower()
    if status in _TERMINAL_STATUSES:
        return status
    terminal = account.get("terminal_failure")
    if isinstance(terminal, dict):
        code = str(terminal.get("code") or "").strip().lower()
        if code in _TERMINAL_STATUSES:
            return code
    return ""


def select_removable_accounts(accounts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select terminal rows without mutating the supplied records."""
    selected: list[dict[str, Any]] = []
    for account in accounts:
        reason = account_cleanup_reason(account)
        if reason:
            row = dict(account)
            row["cleanup_reason"] = reason
            selected.append(row)
    return selected
