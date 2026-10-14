"""``identity_provider_mismatch`` is the **second** spelling of "already registered".

Measured 2026-09-15 07:33:37 (run ``b93f598d``, pid 4520): ``create_account``
answered 400 with

    {"error": {"code": "identity_provider_mismatch",
               "message": "You tried signing in as \\"ec***@icloud.com\\" using a
                           password, which is not the authentication method you
                           used during sign up. Try again using the
                           authentication method you used during sign up."}}

for an address whose signup record used a non-password method.  The message
asserts a signup record exists, so the verdict is exactly as permanent as
``user_already_exists`` -- but only the structured ``code`` says so.

Before this change the address got none of the three protections: no
``partial_registered`` state, no dead-end ledger row, and the reported cause was
classified ``unknown`` (empty advice) -- so the next batch re-drove it and spent
a fresh email OTP rediscovering the same fact.

Two design points are pinned here because getting either wrong silently
regresses attribution:

1.  The marker must be the **structured code**, not the message tail.
    ``RegistrationRetryGuard.record`` truncates ``last_error`` to 160 chars, and
    the ``email_otp_validate:{"endpoint": …}`` prefix already spends 110+ of
    them -- that string's tail is measurably cut.  ``create_account_failed:``
    is short, so the code survives.
2.  ``create_account`` must **not** set ``existing_account = True`` for this
    code.  That flag flips ``create_ok`` to ``True`` twenty lines later, after
    which ``registration_outcome._create_account_error`` returns ``""`` and the
    run blames whatever the re-login fallback last hit -- the mis-attribution
    that module's ``_existing_account_error`` docstring documents.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sms_tool import registration_retry_guard
from sms_tool.failure_registry import (
    PASSWORDLESS_SIGNUP_CODE,
    PASSWORDLESS_SIGNUP_MESSAGE_MARKER,
    is_passwordless_signup_mismatch,
)
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_outcome import _create_account_error
from sms_tool.registration_retry_guard import (
    DEAD_END_MARKERS,
    RegistrationRetryGuard,
    _dead_end_reason,
)
from sms_tool.registration_runtime import RegistrationRuntimeState

#: The real response body, verbatim, minus the address masking.
_MISMATCH_BODY = {
    "error": {
        "code": "identity_provider_mismatch",
        "message": (
            'You tried signing in as "ec***@icloud.com" using a password, which is not '
            "the authentication method you used during sign up. Try again using the "
            "authentication method you used during sign up."
        ),
        "type": "invalid_request_error",
    }
}

#: The string ``registration_outcome._create_account_error`` builds from it --
#: this is what actually reaches ``record()`` / ``last_error``.
_MISMATCH_ERROR = (
    'create_account_failed:identity_provider_mismatch: You tried signing in as '
    '"ec***@icloud.com" using a password, which is not the authentication method you '
    "used during sign up. Try again using the authentication method you used during sign up."
)


# ---------------------------------------------------------------------------
# 1) The predicate
# ---------------------------------------------------------------------------

def test_the_structured_code_is_enough():
    assert is_passwordless_signup_mismatch(_MISMATCH_BODY) is True


def test_the_message_is_only_a_fallback_for_a_body_without_a_code():
    """老响应 / 被代理改写过的 body 拿不到 ``code`` 时才回落到消息。"""
    body = {"error": {"message": _MISMATCH_BODY["error"]["message"]}}
    assert is_passwordless_signup_mismatch(body) is True


def test_an_assembled_error_string_still_matches():
    assert is_passwordless_signup_mismatch(_MISMATCH_ERROR) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        "",
        {"error": {"code": "user_already_exists", "message": "already exists"}},
        {"error": {"code": "wrong_email_otp_code"}},
        {"error": {}},
        {"error": "not-a-mapping"},
        {"code": "identity_provider_mismatch"},  # 没有 ``error`` 外壳 ⇒ 不猜
        "existing_login_otp_validate:{\"status\": 401}",
    ],
)
def test_unrelated_shapes_are_not_matched(value):
    """判据必须**双向自证** —— 只测正例的话，恒 True 的实现也能全绿。"""
    assert is_passwordless_signup_mismatch(value) is False


def test_the_code_constant_is_the_string_the_server_actually_sent():
    assert PASSWORDLESS_SIGNUP_CODE == _MISMATCH_BODY["error"]["code"]
    assert PASSWORDLESS_SIGNUP_MESSAGE_MARKER in _MISMATCH_BODY["error"]["message"].casefold()


# ---------------------------------------------------------------------------
# 2) The guard consumes it
# ---------------------------------------------------------------------------

def test_the_marker_is_registered():
    assert PASSWORDLESS_SIGNUP_CODE in DEAD_END_MARKERS


def test_the_marker_survives_the_160_char_truncation():
    """🔴 报错串被截到 160 字符后，结构化 code 仍在里面。

    这就是为什么判据落在 ``code`` 上而不是消息尾巴上：同一次测量里
    ``create_account_failed:identity_provider_mismatch: …`` 的尾巴还在，而
    ``email_otp_validate:{"endpoint": …}`` 的尾巴已经被切掉了。
    """
    assert _dead_end_reason(_MISMATCH_ERROR) == PASSWORDLESS_SIGNUP_CODE
    assert _dead_end_reason(_MISMATCH_ERROR[:160]) == PASSWORDLESS_SIGNUP_CODE
    # 消息尾巴在 160 字符处已经没了 —— 反面证据，防止有人把判据改回消息匹配。
    assert PASSWORDLESS_SIGNUP_MESSAGE_MARKER not in _MISMATCH_ERROR[:160].casefold()


def test_a_different_verdict_is_not_captured_by_the_new_marker():
    assert _dead_end_reason("create_account_failed:invalid_auth_step") == ""
    assert _dead_end_reason("existing_login_otp_validate:{\"status\": 401}") == ""


def test_the_guard_records_the_second_spelling_under_its_own_name(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "guard.json")
    guard.record("conflict@example.test", failure_class="unknown", error=_MISMATCH_ERROR)
    row = guard.check("conflict@example.test")
    assert row["dead_end"] is True
    assert row["dead_end_reason"] == PASSWORDLESS_SIGNUP_CODE
    assert row["registration_status"] == "partial_registered"


def test_a_later_failure_cannot_erase_the_verdict(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "guard.json")
    guard.record("conflict@example.test", failure_class="unknown", error=_MISMATCH_ERROR)
    guard.record("conflict@example.test", failure_class="network", error="curl: (28) timeout")
    assert guard.check("conflict@example.test")["dead_end_reason"] == PASSWORDLESS_SIGNUP_CODE


# ---------------------------------------------------------------------------
# 3) ``create_account`` -- the only place the code is ever produced
# ---------------------------------------------------------------------------

def _workflow(tmp_path, monkeypatch, response, *, is_user_already_exists=None):
    monkeypatch.setattr(registration_retry_guard, "runtime_file", lambda cfg, name: tmp_path / name)
    monkeypatch.setenv("SMSWORKBENCH_EVENTS", "1")
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    w.runtime = RegistrationRuntimeState(
        username="conflict@example.test", auth_base="https://auth.example.test"
    )
    w._issue_sentinel = Mock(return_value=SimpleNamespace(token="", so_token=""))
    w._operations = SimpleNamespace(
        request_with_retry=Mock(return_value=response),
        _auth_request_headers=Mock(return_value={}),
        auth_impersonate=Mock(return_value=""),
        _sanitize_text=str,
        think_stage=Mock(),
        _is_user_already_exists=is_user_already_exists
        or (lambda data: str((data.get("error") or {}).get("code") or "") == "user_already_exists"),
        _follow_continue_url=Mock(),
        _create_account_continue_url=Mock(return_value=""),
    )
    return w


def _response(body, status=400):
    return SimpleNamespace(status_code=status, json=lambda: body)


def test_the_second_spelling_marks_a_dead_end(tmp_path, monkeypatch):
    w = _workflow(tmp_path, monkeypatch, _response(_MISMATCH_BODY))
    w.create_account()
    row = RegistrationRetryGuard({}).check(w.runtime.username)
    assert row["dead_end"] is True
    assert row["dead_end_reason"] == PASSWORDLESS_SIGNUP_CODE
    assert row["registration_status"] == "partial_registered"


def test_the_second_spelling_keeps_its_own_cause(tmp_path, monkeypatch):
    """🔴 不能把 ``create_ok`` 翻成 True —— 那会抹掉归因。"""
    w = _workflow(tmp_path, monkeypatch, _response(_MISMATCH_BODY))
    w.create_account()
    assert w.runtime.existing_account is False
    assert w.runtime.create_ok is False
    reported = _create_account_error(w.runtime.create_ok, w.runtime.create_data)
    assert reported.startswith("create_account_failed:identity_provider_mismatch:")


def test_an_unrelated_400_marks_nothing(tmp_path, monkeypatch):
    """判据不许把普通失败变成死路 —— 那会永久拉黑可注册地址。"""
    w = _workflow(tmp_path, monkeypatch, _response({"error": {"code": "invalid_auth_step"}}))
    w.create_account()
    row = RegistrationRetryGuard({}).check(w.runtime.username)
    assert not row.get("dead_end")
    assert w.runtime.create_ok is False


def test_the_original_verdict_is_unchanged(tmp_path, monkeypatch, capsys):
    """回归：``user_already_exists`` 仍走原分支（``existing_account=True``）。"""
    w = _workflow(tmp_path, monkeypatch, _response({"error": {"code": "user_already_exists"}}))
    w.create_account()
    assert w.runtime.existing_account is True
    assert w.runtime.create_ok is True
    row = RegistrationRetryGuard({}).check(w.runtime.username)
    assert row["dead_end_reason"] == "user_already_exists"
    assert '"registration_status":"partial_registered"' in capsys.readouterr().out


def test_the_dead_end_is_written_before_the_continue_follow(tmp_path, monkeypatch):
    """死路必须在后续网络调用**之前**落盘 —— 否则一个异常就把它丢了。"""
    w = _workflow(tmp_path, monkeypatch, _response(_MISMATCH_BODY))
    seen: list[object] = []

    def follow(*args, **kwargs):
        seen.append(RegistrationRetryGuard({}).check(w.runtime.username))

    w._operations._follow_continue_url = Mock(side_effect=follow)
    w.create_account()
    assert seen and seen[0]["dead_end"] is True
    assert json.dumps(seen[0])  # 行本身是可序列化的
