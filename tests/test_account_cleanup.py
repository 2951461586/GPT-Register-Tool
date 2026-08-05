from sms_tool.account_cleanup import account_cleanup_reason, select_removable_accounts


def test_cleanup_keeps_unknown_transport_failure():
    account = {"email": "a@example.com", "access_token": "at", "error": "proxy timeout"}
    assert account_cleanup_reason(account) == ""


def test_cleanup_selects_missing_and_terminal_tokens():
    accounts = [
        {"email": "missing@example.com", "access_token": ""},
        {"email": "deactivated@example.com", "access_token": "at", "status": "account_deactivated"},
        {"email": "invalid@example.com", "access_token": "at", "error": "access_token expired (401)"},
        {"email": "active@example.com", "access_token": "at", "status": "registered"},
    ]
    selected = select_removable_accounts(accounts)
    assert [(row["email"], row["cleanup_reason"]) for row in selected] == [
        ("missing@example.com", "missing_access_token"),
        ("deactivated@example.com", "account_deactivated"),
        ("invalid@example.com", "token_invalid"),
    ]
