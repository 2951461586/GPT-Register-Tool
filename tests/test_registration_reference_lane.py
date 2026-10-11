"""``registration.reference_lane``: the reference project's password stage order as one switch."""

from __future__ import annotations

import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sms_tool import auth_flow
from sms_tool import registration_reference_lane as lane
from sms_tool.auth_flow import signup as signup_module
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationStateMachine
from sms_tool.sentinel import client as sentinel_client

DEVICE_ID = "33333333-3333-4333-8333-333333333333"


def _workflow(config):
    return RegistrationEmailWorkflow(RegistrationStateMachine(lambda *_: None), operations=Mock(), config=config)


def test_lane_is_off_by_default_and_leaves_the_config_object_alone():
    config = {"registration": {"signin_prompt_login": False}}
    assert lane.reference_lane_enabled(config) is False
    assert lane.apply_reference_lane(config) is config
    assert lane.apply_reference_lane({}) == {}
    assert lane.apply_reference_lane(None) is None


def test_preset_forces_the_lane_toggles_even_over_explicit_false_and_keeps_the_rest():
    # Shipped configs list every toggle as an explicit false; the preset must still win.
    config = {
        "registration": {
            "reference_lane": True,
            "signin_prompt_login": False,
            "chatgpt_bootstrap": {"enabled": False, "anonymous": True},
            "humanize": {"enabled": False, "factor": 0.5},
        },
        "email_registration": {"registration_mode": "passwordless", "otp_timeout": 120},
    }
    effective = lane.apply_reference_lane(config)

    registration = effective["registration"]
    for key in lane.LANE_FLAGS:
        assert registration[key] is True
    assert registration["chatgpt_bootstrap"] == {"enabled": True, "anonymous": True, "authenticated": True}
    assert registration["humanize"] == {"enabled": True, "factor": 0.5}
    assert effective["email_registration"] == {"registration_mode": "password", "otp_timeout": 120}
    # The input must not be mutated.
    assert config["registration"]["chatgpt_bootstrap"] == {"enabled": False, "anonymous": True}
    assert config["email_registration"]["registration_mode"] == "passwordless"


def test_preset_accepts_the_frozen_production_config():
    frozen = MappingProxyType(
        {
            "registration": MappingProxyType({"reference_lane": True}),
            "email_registration": MappingProxyType({"registration_mode": "passwordless"}),
        }
    )
    effective = lane.apply_reference_lane(frozen)
    assert effective["email_registration"]["registration_mode"] == "password"
    assert effective["registration"]["otp_validate_sentinel"] is True


def test_workflow_applies_the_preset_and_overrides_a_caller_supplied_mode():
    workflow = _workflow({"registration": {"reference_lane": True}})
    workflow.input_registration_mode = "passwordless"
    workflow._apply_reference_lane()
    assert workflow.input_registration_mode == "password"
    assert workflow.config["registration"]["prime_create_account_password"] is True
    assert workflow._reference_lane_active() is True


def test_workflow_without_the_switch_keeps_its_config_and_mode():
    config = {"registration": {}}
    workflow = _workflow(config)
    workflow.input_registration_mode = "passwordless"
    workflow._apply_reference_lane()
    assert workflow.config is config
    assert workflow.input_registration_mode == "passwordless"
    assert workflow._reference_lane_active() is False


def _runtime(**extra):
    session = Mock()
    session.get.return_value = Mock(status_code=200)
    runtime = SimpleNamespace(
        device_id=DEVICE_ID,
        session_logging_id="logging-1",
        session=session,
        proxy="",
        registration_mode="password",
        sentinel_data={"oai_did": DEVICE_ID},
        sentinel_token="",
        **extra,
    )
    return runtime


def test_pre_signin_reads_the_anonymous_set_then_providers_in_order():
    workflow = _workflow({"registration": {"reference_lane": True}})
    workflow.runtime = _runtime()
    workflow.fingerprint_country = "IN"

    lane.pre_signin(workflow)

    urls = [call.args[0] for call in workflow.runtime.session.get.call_args_list]
    assert urls == [
        "https://chatgpt.com/backend-anon/accounts/check/v4-2023-04-27?timezone_offset_min=330",
        "https://chatgpt.com/ces/v1/projects/oai/settings",
        "https://chatgpt.com/backend-anon/me",
        "https://chatgpt.com/backend-anon/checkout_pricing_config/configs/IN",
        "https://chatgpt.com/api/auth/providers",
    ]
    for call in workflow.runtime.session.get.call_args_list:
        assert call.kwargs["headers"]["Referer"] == lane.LOGIN_PAGE_REFERER


def test_bootstrap_failures_are_swallowed():
    workflow = _workflow({"registration": {"reference_lane": True}})
    workflow.runtime = _runtime()
    workflow.runtime.session.get.side_effect = RuntimeError("boom")
    workflow.fingerprint_country = ""

    lane.pre_signin(workflow)  # must not raise

    assert workflow.runtime.session.get.call_count == 4  # no country -> no pricing read


def test_prime_password_bundle_stores_the_token_where_issue_sentinel_reads_it():
    workflow = _workflow({"registration": {"reference_lane": True}})
    workflow.runtime = _runtime()
    issued = SimpleNamespace(token='{"flow":"username_password_create"}')
    with (
        patch("sms_tool.sentinel.issue_password_page_bundle", return_value=issued) as bundle,
        patch("sms_tool.sentinel.sentinel_backend", return_value="node_runner"),
    ):
        lane.prime_password_bundle(workflow)
    bundle.assert_called_once()
    assert workflow.runtime.sentinel_token == issued.token
    assert workflow.runtime.sentinel_data["sentinel_token"] == issued.token
    assert workflow.runtime.sentinel_data["oai_did"] == DEVICE_ID


def test_prime_password_bundle_failure_falls_back_to_per_flow_issuance():
    workflow = _workflow({"registration": {"reference_lane": True}})
    workflow.runtime = _runtime()
    with (
        patch("sms_tool.sentinel.issue_password_page_bundle", side_effect=RuntimeError("down")),
        patch("sms_tool.sentinel.sentinel_backend", return_value="node_runner"),
    ):
        lane.prime_password_bundle(workflow)
    assert workflow.runtime.sentinel_token == ""
    assert "sentinel_token" not in workflow.runtime.sentinel_data


class _BundleSession:
    def __init__(self):
        self.calls = []
        self.cookies = SimpleNamespace(set=lambda *a, **k: None, get_dict=lambda: {})

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return SimpleNamespace(status_code=200, text="<html></html>")

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "token": "challenge",
                "proofofwork": {"required": False},
                "turnstile": {"required": False},
                "so": {"required": False},
            },
        )


def test_password_page_bundle_loads_the_frame_then_shares_one_proof_across_three_flows():
    session = _BundleSession()
    emitted = json.dumps({"p": "p", "t": "t", "c": "challenge", "id": DEVICE_ID, "flow": "username_password_create"})
    with patch.object(sentinel_client, "run_sentinel_sdk", return_value=emitted) as runner:
        issued = sentinel_client.issue_password_page_bundle(
            device_id=DEVICE_ID, session=session, profile={"user_agent": "UA", "session_id": "sid"}
        )

    assert session.calls[0][0] == "get" and "frame.html?sv=" in session.calls[0][1]
    assert session.calls[0][2]["headers"]["Sec-Fetch-Dest"] == "iframe"
    posts = [json.loads(call[2]["data"]) for call in session.calls if call[0] == "post"]
    assert [body["flow"] for body in posts] == list(sentinel_client.PASSWORD_PAGE_BUNDLE_FLOWS)
    assert len({body["p"] for body in posts}) == 1
    runner.assert_called_once()
    assert runner.call_args.kwargs["flow"] == "username_password_create"
    assert issued.flow == "username_password_create"
    assert json.loads(issued.token)["flow"] == "username_password_create"


def test_password_page_bundle_probe_failure_is_skipped():
    class _FlakySession(_BundleSession):
        def post(self, url, **kwargs):
            body = json.loads(kwargs["data"])
            if body["flow"] == "email_otp_validate":
                self.calls.append(("post", url, kwargs))
                return SimpleNamespace(status_code=500, json=lambda: {})
            return super().post(url, **kwargs)

    session = _FlakySession()
    emitted = json.dumps({"p": "p", "t": "t", "c": "challenge", "id": DEVICE_ID, "flow": "username_password_create"})
    with patch.object(sentinel_client, "run_sentinel_sdk", return_value=emitted):
        issued = sentinel_client.issue_password_page_bundle(device_id=DEVICE_ID, session=session)
    assert issued.flow == "username_password_create"


def _prepare(*, reference_navigation):
    signin_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai")
    signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state-1"}
    signin_response.headers = {}
    landing = "https://auth.openai.com/create-account/password"
    authorize_response = Mock(status_code=200, url=landing)
    authorize_response.headers = {}
    session = Mock()
    session.post.return_value = signin_response
    session.get.return_value = authorize_response
    kwargs = {"reference_navigation": True} if reference_navigation else {}
    with patch.object(signup_module, "_emit_operator_line"):
        auth_flow._prepare_signup_auth_state(
            session,
            "user@example.com",
            "did-1",
            "logging-1",
            "https://auth.openai.com",
            "https://chatgpt.com",
            {},
            "csrf",
            attempts=({"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": "login"},),
            **kwargs,
        )
    return session.post.call_args


def test_signin_uses_the_login_page_referer_and_relative_callback_in_the_lane():
    call = _prepare(reference_navigation=True)
    assert "callbackUrl=%2F&" in call.kwargs["data"] or call.kwargs["data"].startswith("csrfToken=csrf&callbackUrl=%2F")
    assert call.kwargs["headers"]["Referer"] == "https://chatgpt.com/auth/login"


def test_signin_default_shape_is_unchanged_outside_the_lane():
    call = _prepare(reference_navigation=False)
    assert "callbackUrl=https%3A%2F%2Fchatgpt.com%2F" in call.kwargs["data"]
    assert call.kwargs["headers"]["Referer"] == "https://chatgpt.com/"


@pytest.mark.parametrize("key", lane.LANE_FLAGS)
def test_every_preset_flag_is_a_registered_boolean_toggle(key):
    from sms_tool.config import validate_config

    validate_config({"chatgpt": {}, "registration": {key: True}})
