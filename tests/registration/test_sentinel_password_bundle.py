"""Password-page Sentinel flow bundle: one shared proof (2026-10-01 scan P1-3).

A real browser primes the password iframe with the *same* requirements proof
(``p``) for several flows; the historical ``issue_sentinel_bundle`` re-randomised
it per flow.  These tests pin the shared-proof behaviour and the opt-in
registration wiring (``registration.sentinel_password_bundle``, default off).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sms_tool import registration_handlers as rh
from sms_tool import sentinel as sentinel_pkg
from sms_tool.sentinel import client


class _Cookies:
    def set(self, *_args, **_kwargs):
        return None

    def get_dict(self):
        return {}


class _Session:
    cookies = _Cookies()


class _ChallengeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"token": "challenge-token"}


def test_bundle_shares_one_requirements_proof_across_flows(monkeypatch):
    posted: list[dict] = []
    solved: list[str] = []

    def fake_request(_session, _method, _url, **kwargs):
        posted.append(json.loads(kwargs["data"]))
        return _ChallengeResponse()

    def fake_run(_challenge, *, flow, **_kwargs):
        solved.append(flow)
        return json.dumps({"so": "", "c": "c", "flow": flow})

    monkeypatch.setattr(client, "request_with_retry", fake_request)
    monkeypatch.setattr(client, "run_sentinel_sdk", fake_run)
    monkeypatch.setattr(
        client,
        "sentinel_fingerprint",
        lambda: {"screen": "1920x1080", "timezone": "UTC", "lang": "en-US"},
    )

    bundle = client.issue_sentinel_bundle(
        flows=("authorize_continue", "username_password_create"),
        device_id="did-1",
        session=_Session(),
    )

    assert [item["flow"] for item in posted] == ["authorize_continue", "username_password_create"]
    assert len({item["p"] for item in posted}) == 1
    assert solved == ["authorize_continue", "username_password_create"]
    assert bundle["sentinel_authorize_continue_token"]
    assert bundle["sentinel_token"]


def _fake_workflow(*, registration_mode="password", enabled=True, sanitize=None):
    runtime = SimpleNamespace(
        registration_mode=registration_mode,
        device_id="did-1",
        session=object(),
        proxy="",
        sentinel_data={},
        sentinel_token="",
        sentinel_authorize_token="",
        sentinel_so_token="",
    )
    workflow: Any = object.__new__(rh.RegistrationEmailWorkflow)
    workflow.runtime = runtime
    workflow.config = {"registration": {"sentinel_password_bundle": enabled}}
    workflow._operations = SimpleNamespace(_sanitize_text=sanitize or (lambda value: str(value)))
    return workflow, runtime


def test_password_bundle_priming_merges_the_shared_tokens(monkeypatch):
    workflow, runtime = _fake_workflow()
    calls: dict = {}

    def fake_bundle(**kwargs):
        calls.update(kwargs)
        return {
            "sentinel_token": "tok-user",
            "sentinel_authorize_continue_token": "tok-auth",
            "sentinel_authorize_continue_so_token": "so-auth",
            "sentinel_oauth_token": "",
            "sentinel_so_token": "",
            "cookie_str": "oai-did=did-1",
            "oai_did": "did-1",
            "sentinel_source": "node_sdk_runner",
        }

    monkeypatch.setattr(sentinel_pkg, "issue_sentinel_bundle", fake_bundle)
    monkeypatch.setattr(sentinel_pkg, "sentinel_backend", lambda config=None: "node_runner")

    workflow._prime_password_sentinel_bundle()

    assert calls["flows"] == ("authorize_continue", "username_password_create")
    assert runtime.sentinel_data["sentinel_token"] == "tok-user"
    assert runtime.sentinel_data["sentinel_authorize_continue_token"] == "tok-auth"
    assert runtime.sentinel_token == "tok-user"
    assert runtime.sentinel_authorize_token == "tok-auth"
    assert runtime.sentinel_data["sentinel_authorize_continue_so_token"] == "so-auth"
    assert "cookie_str" not in runtime.sentinel_data


def test_password_bundle_is_off_by_default(monkeypatch):
    workflow, _runtime = _fake_workflow(enabled=False)
    called = False

    def fake_bundle(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(sentinel_pkg, "issue_sentinel_bundle", fake_bundle)
    monkeypatch.setattr(sentinel_pkg, "sentinel_backend", lambda config=None: "node_runner")

    workflow._prime_password_sentinel_bundle()

    assert called is False
    assert workflow._password_sentinel_bundle_enabled() is False


def test_password_bundle_skips_the_passwordless_lane(monkeypatch):
    workflow, _runtime = _fake_workflow(registration_mode="passwordless")
    called = False

    def fake_bundle(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(sentinel_pkg, "issue_sentinel_bundle", fake_bundle)
    monkeypatch.setattr(sentinel_pkg, "sentinel_backend", lambda config=None: "node_runner")

    workflow._prime_password_sentinel_bundle()

    assert called is False


def test_password_bundle_failure_falls_back_to_per_flow_issuance(monkeypatch, caplog):
    workflow, runtime = _fake_workflow(sanitize=lambda value: str(value))

    def fake_bundle(**_kwargs):
        raise RuntimeError("node runner unavailable")

    monkeypatch.setattr(sentinel_pkg, "issue_sentinel_bundle", fake_bundle)
    monkeypatch.setattr(sentinel_pkg, "sentinel_backend", lambda config=None: "node_runner")

    with caplog.at_level("WARNING"):
        workflow._prime_password_sentinel_bundle()

    assert runtime.sentinel_data == {}
    assert runtime.sentinel_token == ""
    assert "falling back to per-flow issuance" in caplog.text


def test_password_bundle_is_skipped_when_the_backend_is_not_the_node_runner(monkeypatch):
    workflow, _runtime = _fake_workflow()
    called = False

    def fake_bundle(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(sentinel_pkg, "issue_sentinel_bundle", fake_bundle)
    monkeypatch.setattr(sentinel_pkg, "sentinel_backend", lambda config=None: "legacy")

    workflow._prime_password_sentinel_bundle()

    assert called is False
