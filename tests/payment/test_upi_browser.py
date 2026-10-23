"""Behaviour tests for ``sms_tool/upi_link/browser.py`` (measured 16% at 2026-10-23).

Why this exists
---------------
This is the headless rail that produces the two values the protocol path cannot
synthesise: ``x-oai-is-client-observation`` (a browser-signed header the risk
gate validates) and the in-page Sentinel token bound to the real fingerprint.
It was the least-covered module on the payment path, and every failure it can
produce is silent by design -- it degrades to an empty capture so the caller can
fall back.

What is stubbed and why
-----------------------
Only Playwright itself. The module imports ``sync_playwright`` inside the
function, so a fake ``playwright.sync_api`` module is installed in
``sys.modules``; the real code path (cookie seeding, header observation, route
interception, cookie harvesting, token validation, error mapping) runs
untouched. No browser, no network.

The fake is deliberately scripted rather than magic-mocked: every page
evaluation the module can issue is answered by its own branch, so a new
``page.evaluate`` call without a matching branch fails loudly instead of
returning a ``Mock`` that happens to compare equal to everything.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from sms_tool import endpoints
from sms_tool.upi_link import browser as b


# ────────────────────────────── Playwright doubles ──────────────────────────────


class _Request:
    def __init__(self, url: str, headers: dict[str, str] | None = None, post_data: Any = "") -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.post_data = post_data

    def all_headers(self) -> dict[str, str]:
        return dict(self.headers)


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued_with: dict[str, str] | None = None
        self.continue_calls = 0

    def continue_(self, headers: dict[str, str] | None = None) -> None:
        self.continue_calls += 1
        self.continued_with = dict(headers or {})


class _Page:
    def __init__(self, script: dict[str, Any], context: _Context) -> None:
        self.script = script
        self.context = context
        self.route_handler = None
        self.goto_calls: list[str] = []
        self.waited_ms = 0
        self.added_scripts: list[str] = []
        self.evaluations: list[str] = []
        self.wait_for_function_calls: list[tuple[str, int | None]] = []

    def route(self, pattern: str, handler) -> None:
        self.pattern = pattern
        self.route_handler = handler

    def goto(self, url: str, wait_until: str = "", timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        self.context.fire_requests()

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms

    def add_script_tag(self, url: str) -> None:
        self.added_scripts.append(url)

    def wait_for_function(self, expression: str, timeout: int | None = None) -> None:
        self.wait_for_function_calls.append((expression, timeout))

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.evaluations.append(expression)
        if "checkout/approve" in expression:
            return self.script.get("approve_payload", {"status": 200, "body": '{"result":"approved"}'})
        if "Sentinel token timeout" in expression:
            return self.script.get("sentinel_token", "")
        if "SentinelSDK === 'object'" in expression:
            return bool(self.script.get("sdk_present", True))
        if "outerHTML" in expression:
            return self.script.get("dom", "")
        if "backend-api/models" in expression:
            return ""
        raise AssertionError(f"unexpected page.evaluate: {expression[:80]}")


class _Context:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.requests: list[_Request] = []
        self.observers: list[Any] = []
        self.cookie_calls: list[list[dict[str, str]]] = []
        self.page = _Page(script, self)
        self.closed = False

    def on(self, event: str, handler) -> None:
        assert event == "request"
        self.observers.append(handler)

    def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        self.cookie_calls.append(list(cookies))

    def new_page(self) -> _Page:
        return self.page

    def cookies(self, url: str | None = None) -> list[dict[str, str]]:
        return list(self.script.get("cookies", ()))

    def fire_requests(self) -> None:
        for request in self.requests:
            for observer in self.observers:
                observer(request)


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.launch_kwargs: dict[str, Any] = {}

    def new_context(self, **kwargs: Any) -> _Context:
        self.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.context.closed = True


class _Chromium:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.launch_kwargs: dict[str, Any] = {}

    def launch(self, **kwargs: Any) -> _Browser:
        if self.script.get("launch_error"):
            raise self.script["launch_error"]
        self.launch_kwargs = kwargs
        return _Browser(self.script["context"])


class _Playwright:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.chromium = _Chromium(script)

    def __enter__(self) -> _Playwright:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None


def _install_fake_playwright(monkeypatch, script: dict[str, Any]) -> dict[str, Any]:
    """Install a fake ``playwright.sync_api``; return the script dict for inspection."""
    script.setdefault("context", _Context(script))
    fake_module = types.ModuleType("playwright.sync_api")
    setattr(fake_module, "sync_playwright", lambda: _Playwright(script))
    package = types.ModuleType("playwright")
    setattr(package, "sync_api", fake_module)
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return script


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Replace ``browser.time`` with a clock that advances one second per look.

    The capture loop waits for the attestation header until
    ``timeout_s - 5`` seconds of *real* time pass. With a fake page that returns
    instantly, that loop would burn seven seconds of wall clock per test; the
    fake clock expires the deadline after a handful of iterations without
    patching the global ``time`` module.
    """
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(b, "time", types.SimpleNamespace(monotonic=monotonic, sleep=lambda seconds: None))


def _capture(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "access_token": "at-test",
        "session_token": "sess-token",
        "device_id": "did-1",
        "proxy": "",
        "fingerprint": {"locale": "en-GB", "timezone": "Europe/London", "user_agent": "UA/1"},
        "page_url": "",
        "sentinel_flow": "",
        "timeout_s": 12.0,
    }
    kwargs.update(overrides)
    return b._upi_browser_capture(**kwargs)


# ────────────────────────────── availability / proxy ──────────────────────────────


def test_browser_availability_follows_the_playwright_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert b._upi_browser_available() is False
    _install_fake_playwright(monkeypatch, {})
    assert b._upi_browser_available() is True


def test_proxy_dict_maps_socks5h_and_percent_decodes_credentials():
    mapped = b._upi_browser_proxy("socks5h://user%40x:p%40ss@proxy.example:1080")
    assert mapped == {
        "server": "socks5://proxy.example:1080",
        "username": "user@x",
        "password": "p@ss",
    }


def test_proxy_dict_keeps_http_and_bare_hosts():
    assert b._upi_browser_proxy("http://proxy.example:8080") == {"server": "http://proxy.example:8080"}
    assert b._upi_browser_proxy("http://proxy.example") == {"server": "http://proxy.example"}


def test_proxy_dict_rejects_empty_and_unparseable_values():
    for value in ("", "   ", "not-a-url", "http://"):
        assert b._upi_browser_proxy(value) is None, value


def test_browser_id_has_the_stripe_42_character_shape():
    value = b._upi_browser_id()
    assert len(value) == 42, value
    assert value.count("-") == 4, "uuid4 prefix keeps the dashes"
    assert value != b._upi_browser_id()


# ────────────────────────────── capture ──────────────────────────────


def test_capture_without_playwright_degrades_to_a_typed_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = _capture()
    assert result["error"] == "playwright_unavailable"
    assert result["attestation"] == ""
    assert result["observation"] == ""


def test_capture_reads_the_browser_signed_headers_and_launches_through_the_proxy(monkeypatch):
    script = _install_fake_playwright(
        monkeypatch,
        {
            "requests": [
                _Request(
                    "https://chatgpt.com/backend-api/models",
                    {
                        "oai-web-deployment-attestation": "att-1",
                        "x-oai-is-client-observation": "obs-1",
                        "oai-client-version": "prod-1",
                        "oai-client-build-number": "123",
                    },
                )
            ],
            "cookies": [
                {"name": "__Secure-next-auth.session-token", "value": "sess-token"},
                {"name": "__stripe_mid", "value": "muid-cookie"},
                {"name": "__stripe_sid", "value": "sid-cookie"},
            ],
        },
    )
    script["context"].requests = script["requests"]
    result = _capture(proxy="http://proxy.example:8080")

    assert result["attestation"] == "att-1"
    assert result["observation"] == "obs-1"
    assert result["client_version"] == "prod-1"
    assert result["client_build"] == "123"
    assert result["cookies"]["__Secure-next-auth.session-token"] == "sess-token"
    assert result["stripe_ids"]["muid"] == "muid-cookie"
    assert result["stripe_ids"]["sid"] == "sid-cookie"
    assert result["error"] == ""

    chromium = script["context"].page  # sanity: the page really was driven
    assert chromium.goto_calls == [endpoints.CHATGPT_ORIGIN]
    context = script["context"]
    assert context.cookie_calls, "cookies must be seeded before navigation"
    seeded = {cookie["name"]: cookie["value"] for cookie in context.cookie_calls[0]}
    assert seeded["oai-did"] == "did-1"
    assert seeded["__Secure-next-auth.session-token"] == "sess-token"


def test_capture_parses_stripe_ids_from_a_stripe_request(monkeypatch):
    script = _install_fake_playwright(
        monkeypatch,
        {
            "requests": [
                _Request(
                    "https://m.stripe.com/6?guid=guid-1&muid=muid-1",
                    {},
                    post_data="sid=sid-1&stripe_js_id=jsid-1",
                )
            ],
        },
    )
    script["context"].requests = script["requests"]
    result = _capture()

    assert result["stripe_ids"] == {"guid": "guid-1", "muid": "muid-1", "sid": "sid-1", "stripe_js_id": "jsid-1"}


def test_capture_falls_back_to_the_dom_for_the_attestation(monkeypatch):
    script = _install_fake_playwright(
        monkeypatch,
        {"dom": '<script>{"webDeploymentAttestation":"eyJhIjox.signature"}</script>'},
    )
    result = _capture()

    assert result["attestation"] == "eyJhIjox.signature"


def test_capture_skips_a_sentinel_mint_the_deployed_sdk_never_resolves(monkeypatch):
    _install_fake_playwright(monkeypatch, {})
    result = _capture(sentinel_flow="chatgpt_checkout")

    assert "browser_sentinel_skipped:chatgpt_checkout" in result["error"]
    assert result["sentinel_token"] == ""


def test_capture_accepts_an_in_page_sentinel_token_bound_to_the_device(monkeypatch):
    token = json.dumps({"id": "did-1", "flow": "authorize_continue", "c": "c-value", "p": "p-value"})
    script = _install_fake_playwright(
        monkeypatch,
        {"sentinel_token": token, "sdk_present": False},
    )
    result = _capture(sentinel_flow="authorize_continue")

    assert result["sentinel_token"] == token
    assert result["sentinel_flow"] == "authorize_continue"
    page = script["context"].page
    assert any("sdk.js" in url for url in page.added_scripts), "the SDK is injected when absent"
    assert page.wait_for_function_calls, "the mint must wait for SentinelSDK.token to exist"


def test_capture_rejects_a_sentinel_token_for_another_device(monkeypatch):
    token = json.dumps({"id": "someone-else", "flow": "authorize_continue", "c": "c", "p": "p"})
    _install_fake_playwright(monkeypatch, {"sentinel_token": token})
    result = _capture(sentinel_flow="authorize_continue")

    assert result["sentinel_token"] == ""


def test_capture_reports_a_launch_failure_without_raising(monkeypatch):
    _install_fake_playwright(monkeypatch, {"launch_error": RuntimeError("no browser binary")})
    result = _capture()

    assert result["error"].startswith("RuntimeError: no browser binary")


def test_capture_route_handler_injects_the_access_token_only_for_backend_calls(monkeypatch):
    script = _install_fake_playwright(monkeypatch, {})
    _capture()
    page = script["context"].page

    backend = _Route(_Request("https://chatgpt.com/backend-api/models", {"accept": "application/json"}))
    page.route_handler(backend)
    backend_headers = backend.continued_with or {}
    assert backend_headers["authorization"] == "Bearer at-test"
    assert backend_headers["oai-device-id"] == "did-1"

    sentinel = _Route(_Request("https://chatgpt.com/backend-api/sentinel/req", {}))
    page.route_handler(sentinel)
    assert "authorization" not in (sentinel.continued_with or {}), "the sentinel mint must not carry the token"

    third_party = _Route(_Request("https://m.stripe.com/6", {"accept": "*/*"}))
    page.route_handler(third_party)
    assert third_party.continue_calls == 1
    assert "authorization" not in third_party.request.headers


# ────────────────────────────── approve ──────────────────────────────


def _approve(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "access_token": "at-test",
        "session_token": "sess-token",
        "device_id": "did-1",
        "proxy": "",
        "fingerprint": {},
        "processor_entity": "openai",
        "cs_id": "cs_live_1",
        "timeout_s": 12.0,
    }
    kwargs.update(overrides)
    return b._upi_browser_approve(**kwargs)


def test_approve_success_returns_the_browser_issued_observation(monkeypatch):
    script = _install_fake_playwright(
        monkeypatch,
        {
            "requests": [
                _Request(
                    "https://chatgpt.com/backend-api/models",
                    {"x-oai-is-client-observation": "obs-1", "oai-web-deployment-attestation": "att-1"},
                )
            ],
            "approve_payload": {"status": 200, "body": '{"result":"approved"}'},
        },
    )
    script["context"].requests = script["requests"]
    result = _approve()

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["result"] == "approved"
    assert result["observation"] == "obs-1"
    assert result["attestation"] == "att-1"
    assert result["error"] == ""
    assert script["context"].page.goto_calls == [f"{endpoints.CHATGPT_BASE}/checkout/openai/cs_live_1"]


def test_approve_reports_a_non_approved_result_as_an_error(monkeypatch):
    _install_fake_playwright(
        monkeypatch,
        {"approve_payload": {"status": 200, "body": '{"result":"blocked"}'}},
    )
    result = _approve()

    assert result["ok"] is False
    assert result["result"] == "blocked"
    assert result["error"] == "status=200 result=blocked"


def test_approve_without_playwright_degrades_to_a_typed_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = _approve()
    assert result["ok"] is False
    assert result["error"] == "playwright_unavailable"


def test_approve_swallows_launch_failures(monkeypatch):
    _install_fake_playwright(monkeypatch, {"launch_error": TimeoutError("launch timed out")})
    result = _approve()
    assert result["ok"] is False
    assert result["error"].startswith("TimeoutError: launch timed out")
