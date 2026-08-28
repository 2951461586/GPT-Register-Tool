from pathlib import Path
from unittest.mock import patch

from sms_tool import account_liveness
from sms_tool.account_identity import create_registration_identity


ROOT = Path(__file__).resolve().parents[1]


def test_probe_uses_saved_access_token_and_account_id():
    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"quota": {"remaining": 3, "limit": 5}}

    with patch.object(account_liveness.curl_requests, "get", return_value=FakeResponse()) as get:
        result = account_liveness.probe_account_liveness(
            {"email": "ok@example.com", "access_token": "at_123", "chatgpt_account_id": "acc_123"},
            proxy="http://proxy.example:8080",
        )

    assert result["ok"]
    assert result["mode"] == "local"
    assert result["quota_status"] == "3/5"
    call = get.call_args
    assert call.args[0] == account_liveness.CODEX_USAGE_URL
    assert call.kwargs["headers"]["Authorization"] == "Bearer at_123"
    assert call.kwargs["headers"]["Chatgpt-Account-Id"] == "acc_123"
    assert call.kwargs["proxies"]["https"] == "http://proxy.example:8080"


def test_liveness_uses_dedicated_health_proxy_with_account_fingerprint_and_device():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:secret@proxy.example:443"
    health_proxy = "http://health.example:8000"
    config = {
        "proxy": {"registration": base_proxy, "pool": [base_proxy]},
        "account_health": {"proxies": {"liveness": [health_proxy]}},
    }
    account = {
        "access_token": "at",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"quota": {"remaining": 3, "limit": 5}}

    with patch.object(account_liveness, "CFG", config), patch.object(
        account_liveness.curl_requests,
        "get",
        return_value=FakeResponse(),
    ) as get:
        result = account_liveness.probe_account_liveness(
            account,
            proxy="http://127.0.0.1:7897",
        )

    assert result["ok"]
    assert get.call_args.kwargs["proxies"]["https"] == health_proxy
    assert get.call_args.kwargs["impersonate"] == "chrome146"
    assert get.call_args.kwargs["headers"]["oai-device-id"] == "device-123"
    assert "Chrome/146" in get.call_args.kwargs["headers"]["User-Agent"]


def test_probe_normalizes_provider_host_port_user_password_proxy():
    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"quota": {"remaining": 3, "limit": 5}}

    with patch.object(account_liveness.curl_requests, "get", return_value=FakeResponse()) as get:
        result = account_liveness.probe_account_liveness(
            {"email": "ok@example.com", "access_token": "at_123"},
            proxy="http://sg.cliproxy.io:443:user-region-JP:pass",
        )

    assert result["ok"]
    assert get.call_args.kwargs["proxies"]["https"] == "http://user-region-JP:pass@sg.cliproxy.io:443"


def test_probe_redacts_proxy_credentials_from_transport_errors():
    proxy = "http://sg.cliproxy.io:443:user-region-JP:secret"
    normalized = "http://user-region-JP:secret@sg.cliproxy.io:443"
    with patch.object(
        account_liveness.curl_requests,
        "get",
        side_effect=RuntimeError(f"proxy connection failed: {normalized}"),
    ):
        result = account_liveness.probe_account_liveness(
            {"email": "ok@example.com", "access_token": "at_123"},
            proxy=proxy,
        )

    assert not result["ok"]
    assert "secret" not in result["error"]
    assert "http://***:***@sg.cliproxy.io:443" in result["error"]


def test_quota_contract_classifies_only_401_as_invalid_token():
    invalid = account_liveness.quota_result_from_payload(
        {"status_code": 401, "body": {"error": {"message": "unauthorized"}}},
        mode="local",
    )
    inconclusive = account_liveness.quota_result_from_payload(
        {"status_code": 403, "body": {"error": {"message": "cloudflare"}}},
        mode="local",
    )

    assert invalid["status"] == "token_invalid"
    assert inconclusive["status"] == "unknown"
    assert not inconclusive["ok"]


def test_registration_and_payment_use_the_canonical_liveness_seam():
    assert account_liveness.CODEX_USAGE_URL == "https://chatgpt.com/backend-api/wham/usage"
    for relative in ("sms_tool/registration.py", "sms_tool/payment_auth.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "probe_account_liveness" in source
        assert "/backend-api/me" not in source

    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "services" / "protocol-payment").rglob("*.py")
        if "/backend-api/me" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
