from pathlib import Path
from unittest.mock import patch

from sms_tool import account_liveness


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
