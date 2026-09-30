import json
import shutil
import subprocess
import textwrap
from pathlib import Path
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
from sms_tool.sentinel.runner import (
    SentinelRunnerError,
    _platform_for_family,
    check_node_runner_readiness,
    run_sentinel_sdk,
)


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


def test_readiness_checks_pinned_bundle_and_executable_without_running_node():
    with (
        patch("sms_tool.sentinel.runner.validate_runtime_bundle") as bundle,
        patch("sms_tool.sentinel.runner.shutil.which", return_value="/fake/node") as which,
        patch("sms_tool.sentinel.runner.subprocess.run") as run,
    ):
        check_node_runner_readiness()
    bundle.assert_called_once_with()
    which.assert_called_once()
    run.assert_not_called()


def test_readiness_reports_missing_node_without_configured_path(monkeypatch):
    secret_path = "/private/session-secret/node"
    monkeypatch.setenv("OPENAI_SENTINEL_NODE_PATH", secret_path)
    with (
        patch("sms_tool.sentinel.runner.validate_runtime_bundle"),
        patch("sms_tool.sentinel.runner.shutil.which", return_value=None) as which,
    ):
        with pytest.raises(SentinelRunnerError, match="^sentinel_runner_node_missing$") as caught:
            check_node_runner_readiness()
    which.assert_called_once_with(secret_path)
    assert secret_path not in str(caught.value)


def test_readiness_reports_invalid_bundle_without_sensitive_details():
    with (
        patch(
            "sms_tool.sentinel.runner.validate_runtime_bundle",
            side_effect=SentinelBundleError("sentinel_runtime_hash_mismatch:private-secret.js"),
        ),
        patch("sms_tool.sentinel.runner.shutil.which") as which,
    ):
        with pytest.raises(SentinelRunnerError, match="^sentinel_runtime_hash_mismatch$") as caught:
            check_node_runner_readiness()
    which.assert_not_called()
    assert "private-secret" not in str(caught.value)


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
    with (
        patch(
            "sms_tool.sentinel.client.issue_sentinel_token",
            side_effect=RuntimeError("asset is broken"),
        ),
        patch("sms_tool.sentinel_tokens._extract_sentinel", return_value=None),
    ):
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

    with (
        patch(
            "sms_tool.sentinel.client.issue_sentinel_token",
            side_effect=_wrapped,
        ),
        patch("sms_tool.sentinel_tokens._extract_sentinel", return_value=None),
    ):
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


# ---------------------------------------------------------------------------
# Family-consistent fingerprint (P1-1)
# ---------------------------------------------------------------------------
# The runner used to hardcode ``userAgentDataPlatform``/``secChUaPlatform`` to
# "Windows" and the vendored JS attached Chromium-only navigator members to any
# non-Safari family.  Firefox is the *majority* family in the pool
# (``auth_headers._FAMILY_WEIGHTS``), so a Firefox UA was routinely paired with
# ``navigator.userAgentData``/``navigator.gpu``/``navigator.deviceMemory``/
# ``window.chrome`` -- APIs Gecko does not expose.  These tests pin the fix.

_SAFARI_PROFILE = {
    **PROFILE,
    "impersonate": "safari18_0",
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "navigator_platform": "MacIntel",
    "navigator_vendor": "Apple Computer, Inc.",
    "sec_ch_ua_platform": '"macOS"',
}

_FIREFOX_PROFILE = {
    **PROFILE,
    "impersonate": "firefox144",
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"),
    "navigator_platform": "Win32",
    "navigator_vendor": "",
    "sec_ch_ua_platform": '"Windows"',
}

_IOS_PROFILE = {
    **PROFILE,
    "impersonate": "safari18_0_ios",
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "navigator_platform": "iPhone",
    "navigator_vendor": "Apple Computer, Inc.",
    "sec_ch_ua_platform": '"iOS"',
    "max_touch_points": 5,
}


def _capture_runner_config(profile: dict) -> dict:
    """Run the adapter with a stubbed node process, returning the config it wrote."""
    captured: dict = {}

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {"p": "proof", "t": "turnstile", "c": "challenge", "id": DEVICE_ID, "flow": "authorize_continue"}
        )

    def _fake_run(command, **_kwargs):
        config_path = Path(command[command.index("--config") + 1])
        captured.update(json.loads(config_path.read_text(encoding="utf-8")))
        return _Completed()

    with patch("sms_tool.sentinel.runner.subprocess.run", side_effect=_fake_run):
        run_sentinel_sdk(
            _Response.json(),
            flow="authorize_continue",
            device_id=DEVICE_ID,
            profile=profile,
            cookie=f"oai-did={DEVICE_ID}",
            page_url="https://auth.openai.com/email-verification",
        )
    return captured


def test_runner_derives_platform_from_the_profile_not_a_windows_default():
    config = _capture_runner_config(_SAFARI_PROFILE)
    assert config["browserFamily"] == "safari"
    assert config["navigatorPlatform"] == "MacIntel"
    assert config["secChUaPlatform"] == "macOS"
    assert config["userAgentDataPlatform"] == "macOS"


def test_runner_keeps_firefox_vendor_empty_and_forwards_touch_points():
    firefox = _capture_runner_config(_FIREFOX_PROFILE)
    assert firefox["browserFamily"] == "firefox"
    # An empty vendor is Firefox's real value; ``||`` in the JS would coerce it
    # back to "Google Inc.", so the runner must pass it through verbatim.
    assert firefox["navigatorVendor"] == ""

    ios = _capture_runner_config(_IOS_PROFILE)
    assert ios["secChUaPlatform"] == "iOS"
    assert ios["userAgentDataPlatform"] == "iOS"
    assert ios["maxTouchPoints"] == 5


def test_platform_helper_falls_back_when_profile_omits_the_field():
    assert _platform_for_family("safari", {"impersonate": "safari18_0"}) == "macOS"
    assert _platform_for_family("safari", {"impersonate": "safari18_0_ios"}) == "iOS"
    assert _platform_for_family("firefox", {"impersonate": "firefox144"}) == "Windows"
    assert _platform_for_family("chrome", {"impersonate": "chrome146"}) == "Windows"
    assert _platform_for_family("safari", {"impersonate": "safari18_0", "sec_ch_ua_platform": '"macOS"'}) == "macOS"


def test_runner_coerces_malformed_numeric_profile_fields_to_defaults():
    """A loose ``Mapping`` must not crash issuance with a bare ``int()``."""
    garbage = {
        **PROFILE,
        "screen": "not-a-screen",
        "hardware_concurrency": "eight",
        "device_memory": None,
        "device_pixel_ratio": "big",
        "max_touch_points": "five",
        "timezone_offset_minutes": "?",
    }
    config = _capture_runner_config(garbage)
    assert config["hardwareConcurrency"] == 8
    assert config["deviceMemory"] == 8
    assert config["devicePixelRatio"] == 1.0
    assert config["maxTouchPoints"] == 0
    assert config["timezoneOffsetMinutes"] == 0
    assert config["width"] == 1920
    assert config["height"] == 1080


_CTX_BASE_OPTIONS = {
    "flow": "authorize_continue",
    "sentinelSid": "sid",
    "pageUrl": "https://auth.openai.com/email-verification",
    "scriptSrc": "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
    "buildId": "",
    "reactListeningKey": "",
    "reactContainerKey": "",
    "reactResourcesKey": "",
    "cookie": "oai-did=ctx",
    "contentType": "",
    "requestIdleCallback": True,
    "language": "en-US",
    "languages": ["en-US"],
    "timeZone": "America/New_York",
    "timezoneName": "Eastern Standard Time",
    "timezoneOffsetMinutes": -300,
    "hardwareConcurrency": 8,
    "jsHeapSizeLimit": 4395630592,
    "deviceMemory": 8,
    "devicePixelRatio": 1,
    "chromeMajor": "146",
    "chromeFullVersion": "146.0.0.0",
    "secChUa": "",
    "secChUaFullVersionList": "",
    "secChUaPlatformVersion": "10.0.0",
    "secChUaArch": "x86",
    "secChUaBitness": "64",
    "secChUaModel": "",
    "cfEdgeMsec": 38,
    "cfOriginTtfbMsec": 74,
    "cfTcpRttMsec": 22,
    "cfQuicRttMsec": 0,
    "screen": {
        "width": 1920,
        "height": 1080,
        "availWidth": 1920,
        "availHeight": 1042,
        "colorDepth": 30,
        "pixelDepth": 30,
        "orientation": {"type": "landscape-primary", "angle": 0},
    },
}


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required by the Sentinel runner")
def test_fabricated_navigator_is_family_consistent(tmp_path):
    """Firefox must not be handed Chromium-only navigator members.

    ``createBrowserContext`` is the single place the fake JS environment is
    built; probing it directly is the only way to assert what the SDK will see,
    because the runner's stdout is the opaque Sentinel token.
    """
    script = tmp_path / "probe_ctx.js"
    script.write_text(
        textwrap.dedent(
            """
            const runner = require(__RUNNER__);
            const base = __BASE__;
            function probe(family, ua, platform, touch, secChUaPlatform, vendor) {
              const { context } = runner.createBrowserContext({
                ...base, browserFamily: family, userAgent: ua, navigatorPlatform: platform,
                maxTouchPoints: touch, secChUaPlatform, navigatorVendor: vendor,
              });
              const n = context.navigator;
              return {
                hasUserAgentData: "userAgentData" in n,
                hasDeviceMemory: "deviceMemory" in n,
                hasGpu: "gpu" in n,
                hasLogin: "login" in n,
                hasChrome: typeof context.window.chrome !== "undefined",
                vendor: n.vendor,
                maxTouchPoints: n.maxTouchPoints,
              };
            }
            console.log(JSON.stringify({
              firefox: probe("firefox",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0",
                "Win32", 0, "Windows", ""),
              chrome: probe("chrome",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "Win32", 0, "Windows", "Google Inc."),
              ios: probe("safari",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
                "iPhone", 5, "iOS", "Apple Computer, Inc."),
            }));
            """
        )
        .replace("__RUNNER__", json.dumps(str(RUNNER_PATH)))
        .replace("__BASE__", json.dumps(_CTX_BASE_OPTIONS)),
        encoding="utf-8",
    )
    node_binary = shutil.which("node")
    if not node_binary:  # pragma: no cover - guarded by the skipif above
        pytest.skip("Node.js is required by the Sentinel runner")
    completed = subprocess.run(
        [node_binary, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    data = json.loads(completed.stdout)

    firefox = data["firefox"]
    assert firefox["hasUserAgentData"] is False
    assert firefox["hasDeviceMemory"] is False
    assert firefox["hasGpu"] is False
    assert firefox["hasLogin"] is False
    assert firefox["hasChrome"] is False
    assert firefox["vendor"] == ""

    chrome = data["chrome"]
    assert chrome["hasUserAgentData"] is True
    assert chrome["hasDeviceMemory"] is True
    assert chrome["hasGpu"] is True
    assert chrome["hasChrome"] is True
    assert chrome["vendor"] == "Google Inc."

    ios = data["ios"]
    assert ios["hasUserAgentData"] is False
    assert ios["hasDeviceMemory"] is False
    assert ios["hasChrome"] is False
    assert ios["maxTouchPoints"] == 5


# ---------------------------------------------------------------------------
# Checkout Sentinel authority + legacy-fallback default (2026-09-30)
# ---------------------------------------------------------------------------
def test_legacy_fallback_defaults_to_disabled():
    """A dead runner must fail loudly, not silently degrade to pure Python.

    The pure-Python issuer passes the surface endpoints but the OTP service
    validates the real SDK JS server-side; the silent default-True fallback on
    2026-09-17 turned a corrupt bundle into 126/126 ``create_account`` failures.
    """
    assert client._legacy_fallback_enabled({}) is False
    assert client._legacy_fallback_enabled({"email_registration": {}}) is False


def test_node_runner_failure_without_configured_fallback_propagates():
    with patch(
        "sms_tool.sentinel.client.issue_sentinel_token",
        side_effect=RuntimeError("runner failed"),
    ):
        with pytest.raises(RuntimeError, match="runner failed"):
            client.issue_sentinel_flow(
                flow="authorize_continue",
                device_id=DEVICE_ID,
                config={},
            )


def test_chatgpt_checkout_flow_is_registered():
    assert client.CHECKOUT_SENTINEL_FLOW == "chatgpt_checkout"
    assert client.FLOW_PAGE_URLS[client.CHECKOUT_SENTINEL_FLOW]


def test_checkout_sentinel_never_uses_the_legacy_issuer():
    """The legacy issuer cannot mint ``chatgpt_checkout``; it must raise instead."""
    with (
        patch(
            "sms_tool.sentinel.client.issue_sentinel_token",
            side_effect=RuntimeError("runner failed"),
        ),
        patch("sms_tool.sentinel_tokens._extract_sentinel", return_value={}) as legacy,
    ):
        with pytest.raises(client.SentinelIssueError, match="sentinel_fallback_incomplete:chatgpt_checkout"):
            client.issue_sentinel_flow(
                flow=client.CHECKOUT_SENTINEL_FLOW,
                device_id=DEVICE_ID,
                config={"email_registration": {"sentinel_legacy_fallback": True}},
            )

    assert legacy.call_count == 1


def test_checkout_sentinel_mint_reuses_a_successful_result():
    client._CHECKOUT_MINT_CACHE.clear()
    calls: list[dict] = []

    def fake_flow(**kwargs):
        calls.append(kwargs)
        return client.SentinelToken(
            flow=client.CHECKOUT_SENTINEL_FLOW,
            device_id=kwargs["device_id"],
            token="token-fixture",
            so_token="so-fixture",
        )

    with patch("sms_tool.sentinel.client.issue_sentinel_flow", side_effect=fake_flow):
        first = client.issue_checkout_sentinel(device_id=DEVICE_ID, proxy="http://exit.test:80")
        second = client.issue_checkout_sentinel(device_id=DEVICE_ID, proxy="http://exit.test:80")

    assert first.token == second.token == "token-fixture"
    assert len(calls) == 1
    headers = client.checkout_sentinel_headers(device_id=DEVICE_ID, proxy="http://exit.test:80")
    assert headers["OpenAI-Sentinel-Token"] == "token-fixture"
    assert headers["OpenAI-Sentinel-SO-Token"] == "so-fixture"
    assert len(calls) == 1
    client._CHECKOUT_MINT_CACHE.clear()
