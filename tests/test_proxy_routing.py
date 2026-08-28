from sms_tool.proxy_routing import proxy_pool_for, select_operation_proxy


def test_registration_lanes_are_independent_from_health_lanes():
    config = {
        "proxy": {
            "browser_registration_pool": ["http://browser.example:8000"],
            "protocol_registration_pool": ["http://protocol.example:8000"],
            "default": "http://legacy.example:8000",
        },
        "account_health": {
            "proxy_pool": ["http://health.example:8000"],
            "proxies": {
                "promotion": ["http://promo.example:8000"],
            },
        },
    }
    assert proxy_pool_for(config, "browser_registration") == ["http://browser.example:8000"]
    assert proxy_pool_for(config, "protocol_registration") == ["http://protocol.example:8000"]
    assert proxy_pool_for(config, "liveness") == ["http://health.example:8000"]
    assert proxy_pool_for(config, "promotion") == ["http://promo.example:8000"]


def test_health_selection_does_not_restore_registration_affinity():
    config = {
        "proxy": {
            "registration": "http://signup.example:8000",
            "default": "http://legacy.example:8000",
        },
        "account_health": {"proxy_pool": ["http://health.example:8000"]},
    }
    account = {
        "email": "new@example.com",
        "identity_context": {
            "proxy_affinity": {
                "host": "signup.example",
                "port": 8000,
                "scheme": "http",
                "pool_index": 0,
            }
        },
    }
    assert select_operation_proxy(account, operation="liveness", config=config) == "http://health.example:8000"
    assert select_operation_proxy(account, operation="promotion", config=config) == "http://health.example:8000"


def test_health_pool_avoids_registration_exit_when_alternative_exists():
    config = {
        "account_health": {
            "proxies": {
                "liveness": [
                    "http://signup.example:8000",
                    "http://clean-health.example:8000",
                ]
            }
        }
    }
    account = {
        "email": "fresh@example.com",
        "identity_context": {
            "proxy_affinity": {"host": "signup.example", "port": 8000}
        },
    }
    assert select_operation_proxy(account, operation="liveness", config=config) == "http://clean-health.example:8000"
