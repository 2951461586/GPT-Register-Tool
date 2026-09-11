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


def test_liveness_snapshot_prune_keeps_newest_and_excludes_current(tmp_path, monkeypatch):
    import os

    from sms_tool.accounts.account_recovery import _prune_liveness_snapshots

    for i in range(25):
        p = tmp_path / f"run{i:03d}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))

    removed = _prune_liveness_snapshots(tmp_path, keep=20, exclude="run024.json")

    remaining = sorted(p.name for p in tmp_path.glob("*.json"))
    assert removed == 4
    assert len(remaining) == 21  # 20 kept + the excluded current snapshot
    assert "run024.json" in remaining
    assert "run000.json" not in remaining  # oldest pruned


def test_queue_browser_fallback_waits_instead_of_skipping():
    # The 1.0s acquire re-created the "concurrency_limited" pathology that
    # account_recovery already fixed; pin the bounded wait against a revert.
    from sms_tool.accounts import account_health_queue as queue

    assert queue._BROWSER_FALLBACK_WAIT_SECONDS >= 30
