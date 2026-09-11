"""Terminal account-state vocabulary (single owner).

"is this account terminally dead" used to be re-implemented in three places --
``account_cleanup``'s removable set, the deactivation text markers in
``account_scan`` / ``account_recovery``, and the C# ``BackendTextMarkers``
display copy. This module is the Python reference for both vocabularies:

* ``TERMINAL_ACCOUNT_STATUSES`` -- explicit statuses that permit removal.
  Transport failures and other unknown probe results stay eligible for
  recheck (architecture.md "Terminal Account Cleanup").
* ``ACCOUNT_DEACTIVATED_MARKERS`` -- free-text markers proving deactivation.
  ``store/normalize.py`` re-exports the same tuple and is pinned to the C#
  copy by tests/test_backend_text_markers.py; a drift test below keeps this
  module and ``normalize`` from diverging.

Dependency-free by design so every layer may import it.
"""

from __future__ import annotations

from typing import Any

# ``token_revoked`` is the explicit 掉号 verdict written by
# ``account_recovery._persist_token_revoked_drop`` after the recovery chain
# confirmed there is no relogin material. ``account_scan`` additionally infers
# ``at_invalid`` from scan failures -- those rows stay REMOVABLE-NO: an
# error-text token failure without an explicit terminal verdict remains
# eligible for recheck.
TERMINAL_ACCOUNT_STATUSES = frozenset({
    "account_deactivated",
    "deactivated",
    "dropped",
    "token_revoked",
})

ACCOUNT_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "account_deatived",  # historic typo variant that really occurred upstream
    "deleted or deactivated",
    "account has been deleted",
    "account has been deactivated",
)


def is_terminal_account_status(status: Any) -> bool:
    """True when ``status`` is an explicit terminal account status."""
    return str(status or "").strip().lower() in TERMINAL_ACCOUNT_STATUSES


def text_has_account_deactivated(text: Any) -> bool:
    """True when free text carries a deactivation marker."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ACCOUNT_DEACTIVATED_MARKERS)


__all__ = [
    "ACCOUNT_DEACTIVATED_MARKERS",
    "TERMINAL_ACCOUNT_STATUSES",
    "is_terminal_account_status",
    "text_has_account_deactivated",
]
