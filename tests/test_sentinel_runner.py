import json
import shutil
from unittest.mock import patch

import pytest

from sms_tool.sentinel import client
from sms_tool.sentinel.bundle import (
    RUNNER_PATH,
    RUNNER_SHA256,
    SDK_PATH,
    SDK_SHA256,
    SentinelBundleError,
    _digest,
    validate_runtime_bundle,
)
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
    # Assert the digests themselves, not just the filenames.  A name-only check
    # passes even when the pinned constant and the shipped asset disagree --
    # which is precisely how the 2026-09-17 line-ending incident reached
    # production (126/126 accounts failed, 42 of them on
    # `sentinel_legacy_incomplete:oauth_create_account`).
    assert _digest(SDK_PATH) == SDK_SHA256
    assert _digest(RUNNER_PATH) == RUNNER_SHA256
    sdk, runner = validate_runtime_bundle()
    assert sdk.name == "sdk.js"
    assert runner.name == "sentinel-runner.js"


def test_digest_ignores_line_ending_flavour(tmp_path):
    """A CRLF checkout and an LF checkout of the same asset must hash alike.

    ``core.autocrlf`` / ``.gitattributes`` rewrite working-tree line endings at
    checkout while leaving the index (and therefore ``git status``) unchanged,
    so a byte-sensitive digest silently breaks whenever the checkout flavour
    changes.  Guard the normalisation itself.
    """
    crlf = tmp_path / "crlf.js"
    lf = tmp_path / "lf.js"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    lf.write_bytes(b"a\nb\nc\n")
    assert _digest(crlf) == _digest(lf)


def test_runtime_bundle_rejects_altered_asset(tmp_path):
    """Normalisation must not weaken the check: a real content edit still fails."""
    tampered = tmp_path / "tampered.js"
    tampered.write_bytes(b"a\nb\nc\n// injected\n")
    assert _digest(tampered) != _digest(RUNNER_PATH)


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


def test_fallback_failure_reports_the_original_cause():
    """A dead runner plus an empty legacy issuer must not read as a channel fault.

    On 2026-09-17 the node runner failed on a corrupted vendored asset
    (``SentinelBundleError: sentinel_runtime_hash_mismatch``), the legacy issuer
    produced no token either, and every affected account was reported as
    ``sentinel_legacy_incomplete`` -- wording that reads like an upstream
    problem and kept the local defect hidden for a whole batch.
    """
    with patch(
        "sms_tool.sentinel.client.issue_sentinel_token",
        side_effect=RuntimeError("asset is broken"),
    ), patch("sms_tool.sentinel_tokens._extract_sentinel", return_value=None):
        with pytest.raises(client.SentinelIssueError) as caught:
            client.issue_sentinel_flow(
                flow="oauth_create_account",
                device_id=DEVICE_ID,
                config={"email_registration": {"sentinel_legacy_fallback": True}},
            )

    assert "sentinel_fallback_incomplete:oauth_create_account:" in str(caught.value)
    assert "RuntimeError(asset is broken)" in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_fallback_failure_surfaces_the_root_cause_behind_a_wrapper():
    """The actionable string must survive two layers of wrapping.

    Reproduces the 2026-09-17 shape exactly: the vendored asset fails with
    ``SentinelBundleError("sentinel_runtime_hash_mismatch:sentinel-runner.js")``,
    ``issue_sentinel_token`` re-wraps it as
    ``SentinelIssueError("sentinel_issue_failed:SentinelBundleError")``, and the
    legacy issuer yields nothing.  Reporting only the wrapper's type name kept the
    literal ``sentinel_runtime_hash_mismatch`` out of every one of the batch's 3161
    log lines; the fallback error must carry it instead.
    """

    def _wrapped(*_args, **_kwargs):
        try:
            raise SentinelBundleError("sentinel_runtime_hash_mismatch:sentinel-runner.js")
        except SentinelBundleError as inner:
            raise client.SentinelIssueError("sentinel_issue_failed:SentinelBundleError") from inner

    with patch(
        "sms_tool.sentinel.client.issue_sentinel_token",
        side_effect=_wrapped,
    ), patch("sms_tool.sentinel_tokens._extract_sentinel", return_value=None):
        with pytest.raises(client.SentinelIssueError) as caught:
            client.issue_sentinel_flow(
                flow="oauth_create_account",
                device_id=DEVICE_ID,
                config={"email_registration": {"sentinel_legacy_fallback": True}},
            )

    message = str(caught.value)
    assert "sentinel_fallback_incomplete:oauth_create_account:" in message
    assert "sentinel_runtime_hash_mismatch:sentinel-runner.js" in message
    assert len(message) < 300, "must survive the progress ledger's 300-char truncation"


def test_root_reason_is_bounded_and_redacted():
    """The rendered cause must not leak proxy credentials or grow without bound."""
    from sms_tool.sentinel.client import _REASON_LIMIT, _root_reason

    proxy = "http://user:Hunter2@proxy.example:8080"
    exc = RuntimeError(f"connect failed via {proxy}: " + "x" * 500)
    rendered = _root_reason(exc, proxy)

    assert "Hunter2" not in rendered
    assert rendered.startswith("RuntimeError(")
    assert len(rendered) <= len("RuntimeError(") + _REASON_LIMIT + 1
    assert rendered.endswith(")")


def test_pure_legacy_shortfall_keeps_its_original_wording():
    """With no runner failure to blame, the legacy wording must be unchanged."""
    with patch("sms_tool.sentinel_tokens._extract_sentinel", return_value=None):
        with pytest.raises(
            client.SentinelIssueError,
            match="sentinel_legacy_incomplete:oauth_create_account",
        ):
            client.issue_sentinel_flow(
                flow="oauth_create_account",
                device_id=DEVICE_ID,
                config={"email_registration": {"sentinel_backend": "legacy"}},
            )
