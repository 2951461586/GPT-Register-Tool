from sms_tool.accounts.account_health import resolve_account_health_budgets


def test_health_budgets_share_one_default_contract():
    assert resolve_account_health_budgets({}) == {
        "relogin_timeout": 300,
        "batch_timeout": 900,
        "account_timeout": 360,
    }


def test_health_budgets_honor_config_and_explicit_overrides():
    config = {
        "account_health": {
            "relogin_timeout_seconds": 240,
            "batch_timeout_seconds": 800,
            "account_timeout_seconds": 320,
        },
    }
    assert resolve_account_health_budgets(
        config,
        account_timeout=400,
    ) == {
        "relogin_timeout": 240,
        "batch_timeout": 800,
        "account_timeout": 400,
    }
