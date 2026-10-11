"""Browser-style ``passwordless/send-otp`` on the passwordless lane, and the
value-aware dispatch verdict.

Grounded in the 2026-10-11 browser HAR: the verification page POSTs
``/api/accounts/passwordless/send-otp`` (empty body, fresh ``authorize_continue``
Sentinel, flow + navigation ids) and the answer carries
``passwordless_email_otp_send_pending: false``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sms_tool import registration_otp_stages as otp_stages
from sms_tool.auth_state import auth_dump_summary, otp_dispatch_verdict, send_pending_value

AUTH = "https://auth.openai.com"


def _dump(pending):
    body = {
        "client_auth_session": {
            "email_verification_mode": "passwordless_signup",
            "passwordless_email_otp_send_pending": pending,
            "session_id": "authsess_x",
        }
    }
    return auth_dump_summary(body)


def test_pending_false_is_a_finished_dispatch_not_stuck():
    assert otp_dispatch_verdict(_dump(False)) == "dispatched"
    assert send_pending_value(_dump(False)) is False


def test_pending_true_is_stuck():
    assert otp_dispatch_verdict(_dump(True)) == "stuck"
    assert send_pending_value(_dump(True)) is True


def test_key_present_with_unreadable_value_keeps_the_old_reading():
    legacy = {"client_auth_session_keys": ["passwordless_email_otp_send_pending", "email"], "signals": {}}
    assert send_pending_value(legacy) is None
    assert otp_dispatch_verdict(legacy) == "stuck"


def test_key_absent_is_dispatched_and_unreadable_dump_is_unknown():
    assert otp_dispatch_verdict({"client_auth_session_keys": ["email"], "signals": {}}) == "dispatched"
    assert otp_dispatch_verdict({"status": 404}) == "unknown"


class _Stage:
    def __init__(self, *, enabled, password_lane=False, sentinel_error=None, response=None):
        self.config = {"registration": {"passwordless_explicit_send_otp": enabled}}
        self.runtime = SimpleNamespace(
            reg_data={},
            session=Mock(),
            base_headers={"oai-device-id": "did-1", "Accept": "application/json"},
            auth_base=AUTH,
            auth_flow_started=100,
            device_id="did-1",
            session_logging_id="log-1",
            flow_invocation_id="flow-1",
            registration_mode="password" if password_lane else "passwordless",
            signup_state={"url": f"{AUTH}/email-verification"},
            otp_send_dump={},
            otp_issued_after=0,
        )
        self._password = password_lane
        self.sentinel_error = sentinel_error
        self.aborted: list[str] = []
        self.issued: list[tuple[str, bool]] = []
        self.r = SimpleNamespace(
            _email_otp_send_url=lambda data: f"{AUTH}/api/accounts/email-otp/send",
            _follow_continue_url=Mock(return_value=Mock(status_code=200)),
            _fetch_client_auth_session_dump=lambda *a, **k: {},
            _json_or_raw=lambda resp, **k: getattr(resp, "payload", {}),
            SyntheticResponse=lambda status, payload, url="": SimpleNamespace(
                status_code=status, payload=payload, url=url
            ),
            openai_auth_headers=self._auth_headers,
            auth_impersonate=lambda: "chrome",
            request_with_retry=Mock(return_value=response or self._ok_response()),
        )

    @staticmethod
    def _ok_response():
        return SimpleNamespace(
            status_code=200,
            payload={
                "oai-client-auth-session": {
                    "email_verification_mode": "passwordless_signup",
                    "passwordless_email_otp_send_pending": False,
                }
            },
        )

    @staticmethod
    def _auth_headers(did, **kwargs):
        headers = {"oai-device-id": did, "Referer": kwargs["referer"], "Origin": kwargs["origin"]}
        headers["openai-sentinel-token"] = kwargs["sentinel_token"]
        headers["x-access-flow-invocation-id"] = kwargs["flow_invocation_id"]
        headers.update(kwargs.get("extra") or {})
        return headers

    def _password_lane_active(self):
        return self._password

    def _abort(self, message):
        self.aborted.append(message)
        raise AssertionError(f"aborted: {message}")

    def _issue_sentinel(self, flow, *, force_fresh=False):
        self.issued.append((flow, force_fresh))
        if self.sentinel_error:
            raise self.sentinel_error
        return SimpleNamespace(token="sentinel-json", so_token="so-json")


def _send(stage):
    with patch.object(otp_stages, "_emit") as emit:
        otp_stages.send_email_otp(stage)
    return [call.args[1] % call.args[2:] if len(call.args) > 2 else call.args[1] for call in emit.call_args_list]


def test_default_passwordless_lane_still_assumes_the_pre_sent_code():
    stage = _Stage(enabled=False)
    lines = _send(stage)
    stage.r.request_with_retry.assert_not_called()
    assert stage.issued == []
    assert not any("Passwordless explicit send-otp" in line for line in lines)
    assert stage.runtime.otp_issued_after == 95


def test_toggle_posts_send_otp_like_the_browser_page():
    stage = _Stage(enabled=True)
    lines = _send(stage)
    call = stage.r.request_with_retry.call_args
    assert call.args[1] == "post"
    assert call.args[2] == f"{AUTH}/api/accounts/passwordless/send-otp"
    assert "json" not in call.kwargs and "data" not in call.kwargs
    headers = call.kwargs["headers"]
    assert headers["Referer"] == f"{AUTH}/email-verification"
    assert headers["Origin"] == AUTH
    assert headers["openai-sentinel-token"] == "sentinel-json"
    assert headers["x-access-flow-invocation-id"] == "flow-1"
    assert headers["x-openai-document-navigation-id"]
    assert headers["Sec-Fetch-Mode"] == "cors"
    # Absent is not enough: libcurl then labels the empty POST form-urlencoded
    # and the server answers 400 unsupported_content_type (live, 2026-10-11).
    assert "Content-Type" in headers and headers["Content-Type"] is None
    assert stage.issued == [("authorize_continue", True)]
    stage.r._follow_continue_url.assert_not_called()
    assert "  Passwordless explicit send-otp" in lines
    assert "  Passwordless send-otp: status=200 pending=false mode=passwordless_signup" in lines


def test_the_send_is_stamped_at_send_time_not_at_authorize_time():
    stage = _Stage(enabled=True)
    with patch.object(otp_stages.time, "time", return_value=5000):
        _send(stage)
    assert stage.runtime.otp_issued_after == 5000


def test_pending_true_and_an_unknown_mode_are_reported_without_echoing_server_strings():
    response = SimpleNamespace(
        status_code=200,
        payload={
            "oai-client-auth-session": {
                "email_verification_mode": "something-new-and-long",
                "passwordless_email_otp_send_pending": True,
            }
        },
    )
    stage = _Stage(enabled=True, response=response)
    lines = _send(stage)
    assert "  Passwordless send-otp: status=200 pending=true mode=other" in lines
    assert not any("something-new" in line for line in lines)


def test_a_non_200_send_aborts_with_the_status():
    stage = _Stage(enabled=True, response=SimpleNamespace(status_code=409, payload={}))
    with pytest.raises(AssertionError):
        _send(stage)
    assert stage.aborted == ["email_otp_send_failed:409"]


def test_a_sentinel_failure_fails_the_arm_instead_of_degrading_to_the_default():
    stage = _Stage(enabled=True, sentinel_error=RuntimeError("runner down"))
    with pytest.raises(AssertionError):
        _send(stage)
    assert stage.aborted and stage.aborted[0].startswith("email_otp_send_failed:sentinel_unavailable")
    stage.r.request_with_retry.assert_not_called()


def test_the_password_lane_ignores_the_toggle():
    stage = _Stage(enabled=True, password_lane=True)
    _send(stage)
    stage.r.request_with_retry.assert_not_called()
    stage.r._follow_continue_url.assert_called_once()


def test_config_rejects_a_non_boolean_toggle():
    from sms_tool.config import ConfigError, validate_config

    with pytest.raises(ConfigError, match=r"registration\.passwordless_explicit_send_otp must be a boolean"):
        validate_config({"registration": {"passwordless_explicit_send_otp": "yes"}})


def test_ab_harness_preregisters_p3_1():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "registration_ab.py"
    spec = importlib.util.spec_from_file_location("registration_ab_p3_1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    design = module.EXPERIMENTS["p3-1-passwordless-explicit-send-otp"]
    assert design["toggle"] == "registration.passwordless_explicit_send_otp"
    assert design["arms"] == {"default": False, "explicit": True}
    assert design["mechanism"] == "Passwordless explicit send-otp"
    assert any("DIFFERENT addresses" in item for item in design["hold_constant"])
