"""Terminal account-state vocabulary: single-owner guarantees.

``sms_tool/accounts/account_terminal.py`` owns the removable-status set and the
deactivation text markers. ``store/normalize.py`` keeps a source-literal copy
of the marker tuple only because the cross-language AST parser
(test_backend_text_markers.py) cannot see through imports -- this module
keeps the two from drifting.
"""

from sms_tool.accounts import account_terminal
from sms_tool.accounts.account_cleanup import account_cleanup_reason
from sms_tool.store.normalize import ACCOUNT_DEACTIVATED_MARKERS as NORMALIZE_MARKERS


def test_terminal_status_set_covers_all_explicit_verdicts():
    # account_recovery writes these verdicts; cleanup must be able to remove
    # every one of them, and nothing outside this set may be removable.
    assert account_terminal.TERMINAL_ACCOUNT_STATUSES == {
        "account_deactivated",
        "deactivated",
        "dropped",
        "token_revoked",
    }
    assert account_terminal.is_terminal_account_status("TOKEN_REVOKED ")
    assert not account_terminal.is_terminal_account_status("at_invalid")
    assert not account_terminal.is_terminal_account_status(None)


def test_deactivation_markers_match_the_csharp_pinned_copy():
    # store/normalize's literal is AST-pinned to C# BackendTextMarkers;
    # equality here extends that pin to this module.
    assert account_terminal.ACCOUNT_DEACTIVATED_MARKERS == NORMALIZE_MARKERS


def test_text_has_account_deactivated_matches_marker_semantics():
    assert account_terminal.text_has_account_deactivated("Account has been deactivated")
    # The historic misspelling is live data in old session files.
    assert account_terminal.text_has_account_deactivated("account_deatived")
    assert not account_terminal.text_has_account_deactivated("temporary network failure")


def test_cleanup_removable_set_is_the_shared_vocabulary():
    assert account_cleanup_reason({"email": "a@x", "terminal_failure": {"code": "token_revoked"}}) == "token_revoked"
    assert account_cleanup_reason({"email": "a@x", "status": "registered"}) == ""
