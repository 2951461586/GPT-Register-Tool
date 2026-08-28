import pytest

from sms_tool.config import ConfigError, validate_config


def _base_config():
    return {
        "chatgpt": {
            "auth_base_url": "https://auth.openai.com",
            "chat_base_url": "https://chatgpt.com",
        }
    }


def test_proxy_lane_configuration_accepts_independent_pools():
    config = _base_config()
    config.update({
        "proxy": {
            "browser_registration_pool": ["http://browser.example:8000"],
            "protocol_registration_pool": "http://protocol.example:8000",
            "health": ["http://health.example:8000"],
        },
        "account_health": {
            "proxy_pool": ["http://health-default.example:8000"],
            "proxies": {
                "liveness": ["http://liveness.example:8000"],
                "promotion": "http://promotion.example:8000",
                "browser": ["http://browser-health.example:8000"],
            },
        },
    })

    validate_config(config)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("browser_registration_pool", {"proxy": "bad"}, "proxy.browser_registration_pool"),
        ("protocol_registration_pool", 123, "proxy.protocol_registration_pool"),
        ("health", {"proxy": "bad"}, "proxy.health"),
    ],
)
def test_registration_and_health_proxy_lanes_reject_invalid_shapes(path, value, message):
    config = _base_config()
    config["proxy"] = {path: value}

    with pytest.raises(ConfigError, match=message):
        validate_config(config)


def test_account_health_proxy_lanes_reject_unknown_lane():
    config = _base_config()
    config["account_health"] = {
        "proxies": {"registration": ["http://wrong-lane.example:8000"]}
    }

    with pytest.raises(ConfigError, match="unsupported account_health proxy lane"):
        validate_config(config)
