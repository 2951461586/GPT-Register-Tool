from sms_tool.accounts.account_cleanup import account_cleanup_reason, select_removable_accounts


def test_cleanup_keeps_unknown_transport_failure():
    account = {"email": "a@example.com", "access_token": "at", "error": "proxy timeout"}
    assert account_cleanup_reason(account) == ""


def test_cleanup_selects_only_explicit_terminal_states():
    accounts = [
        {"email": "missing@example.com", "access_token": ""},
        {"email": "deactivated@example.com", "access_token": "at", "status": "account_deactivated"},
        {"email": "invalid@example.com", "access_token": "at", "error": "access_token expired (401)"},
        {"email": "active@example.com", "access_token": "at", "status": "registered"},
    ]
    selected = select_removable_accounts(accounts)
    assert [(row["email"], row["cleanup_reason"]) for row in selected] == [
        ("deactivated@example.com", "account_deactivated"),
    ]


def test_cleanup_selects_token_revoked_terminal_verdict():
    # 掉号: _persist_token_revoked_drop stamps terminal_failure.code=token_revoked
    # after recovery confirmed there is no relogin material. Without it in the
    # terminal set these rows survived every cleanup pass forever.
    account = {
        "email": "revoked@example.com",
        "access_token": "",
        "status": "at_invalid",
        "error": "token_revoked_unrecoverable",
        "terminal_failure": {"code": "token_revoked", "reason": "token_invalid_no_relogin_material"},
    }
    assert account_cleanup_reason(account) == "token_revoked"
    assert [row["email"] for row in select_removable_accounts([account])] == ["revoked@example.com"]


def test_cleanup_still_keeps_error_text_only_token_failures():
    # Error-text token failures without an explicit terminal verdict stay
    # eligible for recheck (architecture.md Terminal Account Cleanup rule).
    account = {"email": "maybe@example.com", "access_token": "at", "status": "at_invalid", "error": "401"}
    assert account_cleanup_reason(account) == ""
