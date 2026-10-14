from sms_tool.proxy_routing import (
    operation_proxy_candidates,
    proxy_pool_for,
    select_operation_proxy,
    select_operation_proxy_candidate,
)


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


def test_health_fallback_keeps_full_registration_pool():
    config = {
        "proxy": {
            "registration": "http://registration.example:8000",
            "pool": ["http://pool-a.example:8000", "http://pool-b.example:8000"],
        },
        "account_health": {},
    }
    assert proxy_pool_for(config, "liveness") == [
        "http://registration.example:8000",
        "http://pool-a.example:8000",
        "http://pool-b.example:8000",
    ]


def test_explicit_proxy_wins_over_enabled_affinity_and_operation_pool():
    config = {
        "proxy": {"liveness": ["http://pool.example:8000"]},
        "account_health": {"use_registration_affinity": True},
    }
    account = {
        "email": "user@example.com",
        "identity_context": {
            "proxy_affinity": {"host": "saved.example", "port": 8000, "scheme": "http"}
        },
    }
    selected = select_operation_proxy_candidate(
        account,
        operation="liveness",
        explicit="http://explicit.example:8000",
        config=config,
    )
    assert selected is not None
    assert selected.proxy == "http://explicit.example:8000"
    assert selected.source == "explicit"
    assert select_operation_proxy(
        account,
        operation="liveness",
        explicit="http://explicit.example:8000",
        config=config,
    ) == "http://explicit.example:8000"


def test_operation_candidates_have_one_canonical_precedence_order():
    config = {
        "proxy": {
            "pool": ["http://saved.example:8000"],
            "promotion": ["http://pool.example:8000"],
        },
        "account_health": {"use_registration_affinity": True},
    }
    account = {
        "email": "user@example.com",
        "identity_context": {
            "proxy_affinity": {"host": "saved.example", "port": 8000, "scheme": "http"}
        },
    }
    candidates = operation_proxy_candidates(
        account,
        operation="promotion",
        explicit="http://explicit.example:8000",
        config=config,
    )
    assert [(item.proxy, item.source) for item in candidates] == [
        ("http://explicit.example:8000", "explicit"),
        ("http://saved.example:8000", "registration_affinity"),
        ("http://pool.example:8000", "operation_pool"),
    ]


def test_supplied_candidate_pool_uses_same_precedence_and_source():
    candidates = operation_proxy_candidates(
        {"email": "stable@example.com"},
        operation="promotion",
        explicit="http://explicit.example:8000",
        pool=["http://candidate-a.example:8000", "http://candidate-b.example:8000"],
        config={},
    )
    assert candidates[0].source == "explicit"
    assert candidates[0].proxy == "http://explicit.example:8000"
    assert {item.source for item in candidates[1:]} == {"operation_pool"}
