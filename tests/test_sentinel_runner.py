import json
import shutil
from unittest.mock import patch

import pytest

from sms_tool.sentinel import client
from sms_tool.sentinel.bundle import validate_runtime_bundle
from sms_tool.sentinel.runner import run_sentinel_sdk


DEVICE_ID = "22222222-2222-4222-8222-222222222222"
PROFILE = {
    "screen": "1920x1080",
    "lang": "en-US",
    "lang_full": "en-US,en;q=0.9",
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "impersonate": "chrome136",
    "navigator_platform": "Win32",
    "navigator_vendor": "Google Inc.",
    "timezone": "UTC",
    "session_id": "11111111-1111-4111-8111-111111111111",
}


class _Cookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **_kwargs):
        self.values[name] = value

    def get_dict(self):
        return dict(self.values)


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {
            "token": "challenge-test",
            "proofofwork": {"required": False},
            "turnstile": {"required": False},
            "so": {"required": False},
        }


class _Session:
    def __init__(self):
        self.cookies = _Cookies()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


def test_vendored_runtime_bundle_matches_pinned_hashes():
    sdk, runner = validate_runtime_bundle()
    assert sdk.name == "sdk.js"
    assert runner.name == "sentinel-runner.js"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required by the Sentinel runner")
def test_node_runner_executes_vendored_sdk_offline():
    token = run_sentinel_sdk(
        _Response.json(),
        flow="authorize_continue",
        device_id=DEVICE_ID,
        profile=PROFILE,
        cookie=f"oai-did={DEVICE_ID}",
        page_url="https://auth.openai.com/email-verification",
    )
    parsed = json.loads(token)
    assert parsed["id"] == DEVICE_ID
    assert parsed["flow"] == "authorize_continue"
    assert parsed["c"] == "challenge-test"
    assert "p" in parsed
    assert "t" in parsed


def test_client_fetches_challenge_and_passes_same_identity_to_runner():
    session = _Session()
    emitted = json.dumps(
        {"p": "proof", "t": "turnstile", "c": "challenge-test", "id": DEVICE_ID, "flow": "authorize_continue"}
    )
    with patch("sms_tool.sentinel.client.run_sentinel_sdk", return_value=emitted) as runner:
        result = client.issue_sentinel_token(
            flow="authorize_continue",
            device_id=DEVICE_ID,
            session=session,
            profile=PROFILE,
        )

    assert result.device_id == DEVICE_ID
    assert result.flow == "authorize_continue"
    request = json.loads(session.calls[0][1]["data"])
    assert request["id"] == DEVICE_ID
    assert request["flow"] == "authorize_continue"
    assert request["p"].startswith("gAAAAAC")
    assert runner.call_args.kwargs["device_id"] == DEVICE_ID
    assert runner.call_args.kwargs["flow"] == "authorize_continue"
    assert f"oai-did={DEVICE_ID}" in runner.call_args.kwargs["cookie"]


def test_runner_keeps_cookie_out_of_process_arguments():
    class _Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {"p": "proof", "t": "turnstile", "c": "challenge", "id": DEVICE_ID, "flow": "authorize_continue"}
        )

    secret_cookie = f"oai-did={DEVICE_ID}; session=secret-value"
    with patch("sms_tool.sentinel.runner.subprocess.run", return_value=_Completed()) as invoked:
        run_sentinel_sdk(
            _Response.json(),
            flow="authorize_continue",
            device_id=DEVICE_ID,
            profile=PROFILE,
            cookie=secret_cookie,
            page_url="https://auth.openai.com/email-verification",
        )

    command = invoked.call_args.args[0]
    assert all("secret-value" not in str(part) for part in command)


def test_flow_uses_node_runner_and_honors_disabled_legacy_fallback():
    with patch(
        "sms_tool.sentinel.client.issue_sentinel_token",
        side_effect=RuntimeError("runner failed"),
    ):
        with pytest.raises(RuntimeError, match="runner failed"):
            client.issue_sentinel_flow(
                flow="authorize_continue",
                device_id=DEVICE_ID,
                config={
                    "email_registration": {
                        "sentinel_backend": "node_runner",
                        "sentinel_legacy_fallback": False,
                    }
                },
            )
