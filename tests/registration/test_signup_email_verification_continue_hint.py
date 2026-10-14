"""The extra declared-screen ``authorize/continue`` from ``/email-verification``.

The 2026-10-07 scan (``docs/audits/scan-2026-10-07-protocol-registration.md``
P0-A) found the sibling toggle ``registration.signup_continue_screen_hint``
**unreachable on the failure shape it was meant to test**: its only read point
is ``_continue_signup_username``, which ``_prepare_signup_auth_state`` skips
because ``authorize`` lands on ``/email-verification`` and the function returns
early.  ``run_p15_hint.log`` proves it -- the hint arm ran 0/5 with **zero**
``Signup username continue`` lines, so the arm measured nothing.

``registration.signup_email_verification_continue_hint`` (default **False**)
moves the action point into that early return: one extra ``authorize/continue``
carrying ``screen_hint: "signup"``, on the password lane only.  This file pins:

1. Default **off** keeps the early return byte-for-byte: no extra POST, the
   state is the same ``skipped`` dict the lane returned before the toggle
   existed.
2. On, the extra POST happens, ``force`` bypasses the entry guard, and the
   body declares ``screen_hint`` without needing the sibling toggle.
3. The forced request logs under the distinct ``Email verification continue
   hint`` label -- the A/B harness's mechanism check greps it, and a marker
   shared with P1-5 would let one experiment's arm impersonate the other's.
4. The passwordless lane never posts it (its own contract is "the browser
   sends the OTP from that state"), and a password-step landing never posts it
   (the transaction is already on the password arm there).

Classes inherit ``unittest.TestCase`` deliberately: this suite mixes plain
pytest functions and TestCase classes, and a bare class silently collects zero
tests.
"""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import Mock, patch

from sms_tool import auth_flow
from sms_tool.auth_flow import signup as signup_module

_EMAIL_VERIFICATION = "https://auth.openai.com/email-verification"
_PASSWORD_STEP = "https://auth.openai.com/create-account/password"


def _response(status_code=200, url=_EMAIL_VERIFICATION):
    response = Mock()
    response.status_code = status_code
    response.url = url
    response.headers = {}
    response.json.return_value = {"continue_url": url}
    return response


def _run_continue(*, force, current_url=_EMAIL_VERIFICATION, screen_hint=False, status_code=200):
    """Drive one ``_continue_signup_username`` call with the transport captured."""
    response = _response(status_code=status_code)
    with (
        patch.object(auth_flow.deps, "request_with_retry", return_value=response) as request,
        patch.object(auth_flow.steps, "_signup_continue_screen_hint_enabled", lambda: screen_hint),
        patch.object(auth_flow.deps, "_follow_continue_url", return_value=Mock(url=response.url)),
        patch.object(signup_module, "_emit_operator_line") as emit_line,
        patch.object(
            signup_module.sentinel_flow,
            "_authorize_continue_sentinel",
            return_value=({"sentinel_source": "test"}, "tok", "so"),
        ),
    ):
        result = auth_flow._continue_signup_username(
            Mock(),
            "user@example.com",
            "device-id",
            "https://auth.openai.com",
            {},
            current_url,
            force=force,
        )
    return result, request, emit_line


class ForcedContinueWireTests(unittest.TestCase):
    """``force`` 的线上形状：入口守卫被绕过、body 声明 screen、label 独占。"""

    def test_default_guard_still_skips_the_email_verification_landing(self):
        """默认（``force=False``）与今天逐字节一致：不发请求，直接 ``skipped``。"""
        result, request, emit_line = _run_continue(force=False)
        self.assertEqual(result, {"ok": True, "url": _EMAIL_VERIFICATION, "skipped": True})
        request.assert_not_called()
        emit_line.assert_not_called()

    def test_force_posts_the_continue_from_the_email_verification_landing(self):
        result, request, _emit_line = _run_continue(force=True)
        self.assertTrue(result["ok"])
        request.assert_called_once()
        self.assertTrue(request.call_args.args[2].endswith("/api/accounts/authorize/continue"))

    def test_force_declares_screen_hint_without_the_sibling_toggle(self):
        """新开关自己带声明：不需要（也不许）同时打开 P1-5 的开关。"""
        _result, request, _emit_line = _run_continue(force=True, screen_hint=False)
        self.assertEqual(request.call_args.kwargs["json"]["screen_hint"], "signup")
        self.assertEqual(
            request.call_args.kwargs["json"]["username"],
            {"value": "user@example.com", "kind": "email"},
        )

    def test_force_emits_the_distinct_mechanism_line(self):
        """机制行必须与 P1-5 的 ``Signup username continue`` 不同。"""
        _result, request, emit_line = _run_continue(force=True)
        emit_line.assert_called_once()
        self.assertIn("Email verification continue hint", emit_line.call_args.args[1])
        # The wire label is distinct too, so a retry log names the forced POST.
        self.assertEqual(request.call_args.kwargs["label"], "Email verification continue hint")
        _result, request, emit_line = _run_continue(
            force=False, current_url="https://auth.openai.com/api/accounts/authorize"
        )
        emit_line.assert_not_called()
        self.assertEqual(request.call_args.kwargs["label"], "Signup username continue")

    def test_p1_5_marker_is_emitted_only_inside_the_toggle_branch(self):
        """P1-5 的机制行属于**开关自己的分支**，不是基线路径。

        旧标记 ``Signup username continue`` 由基线路径无条件打印：对照臂只要
        走到这个函数就会满足「必须不出现」，而 P1-8 的强制路径也会打印它。
        新标记只在 ``force=False`` 且开关打开时出现（2026-10-07 扫描 §5.2）。
        """
        _result, _request, emit_line = _run_continue(
            force=False,
            current_url="https://auth.openai.com/api/accounts/authorize",
            screen_hint=True,
        )
        emit_line.assert_called_once()
        self.assertIn("Signup continue declares screen_hint=signup", emit_line.call_args.args[1])

    def test_the_forced_p1_8_path_does_not_emit_the_p1_5_marker(self):
        """P1-8 的强制路径不得让 P1-5 的机制门禁误通过。"""
        _result, _request, emit_line = _run_continue(force=True, screen_hint=False)
        emit_line.assert_called_once()
        self.assertIn("Email verification continue hint", emit_line.call_args.args[1])
        self.assertNotIn("Signup continue declares screen_hint=signup", emit_line.call_args.args[1])


def _prepare(*, hint_enabled, landing=_EMAIL_VERIFICATION, passwordless_web=False, continue_result=None):
    """Run ``_prepare_signup_auth_state`` with the authorize landing controlled."""
    signin_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai")
    signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state-1"}
    signin_response.headers = {}
    authorize_response = Mock(status_code=302, url=landing)
    authorize_response.headers = {"location": landing}
    session = Mock()
    session.post.return_value = signin_response
    session.get.return_value = authorize_response
    advanced = (
        continue_result
        if continue_result is not None
        else {
            "ok": True,
            "status": 200,
            "url": _EMAIL_VERIFICATION,
        }
    )
    with (
        patch.object(auth_flow.steps, "_signup_email_verification_continue_hint_enabled", lambda: hint_enabled),
        patch.object(auth_flow.signup, "_continue_signup_username", return_value=advanced) as continue_signup,
    ):
        state = auth_flow._prepare_signup_auth_state(
            session,
            "user@example.com",
            "did-1",
            "logging-1",
            "https://auth.openai.com",
            "https://chatgpt.com",
            {},
            "csrf",
            passwordless_web=passwordless_web,
            attempts=({"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},),
        )
    return state, continue_signup, session


class EarlyReturnBranchTests(unittest.TestCase):
    """早返回分支的接线：只有 password 泳道 + ``/email-verification`` 才补发。"""

    def test_toggle_off_keeps_the_skipped_early_return(self):
        state, continue_signup, session = _prepare(hint_enabled=False)
        self.assertTrue(state["ok"])
        self.assertTrue(state["skipped"])
        continue_signup.assert_not_called()
        # Only the signin POST; no extra authorize/continue.
        session.post.assert_called_once()

    def test_toggle_on_posts_the_hinted_continue(self):
        state, continue_signup, _session = _prepare(hint_enabled=True)
        self.assertTrue(state["ok"])
        continue_signup.assert_called_once()
        self.assertTrue(continue_signup.call_args.kwargs["force"])
        self.assertTrue(state["email_verification_continue_hint"])
        self.assertEqual(state["attempt"], "signup_screen_hint")

    def test_passwordless_lane_never_posts_the_hint(self):
        state, continue_signup, session = _prepare(hint_enabled=True, passwordless_web=True)
        self.assertTrue(state["skipped"])
        continue_signup.assert_not_called()
        session.post.assert_called_once()

    def test_password_step_landing_does_not_trigger_the_hint(self):
        """落密码步 ⇒ 事务已在密码臂上，补发 signup continue 是错的。"""
        state, continue_signup, _session = _prepare(hint_enabled=True, landing=_PASSWORD_STEP)
        self.assertTrue(state["skipped"])
        continue_signup.assert_not_called()

    def test_a_failed_hint_continue_is_reported_by_name(self):
        state, _continue_signup, _session = _prepare(
            hint_enabled=True,
            continue_result={"ok": False, "status": 400, "url": _EMAIL_VERIFICATION, "body": {}},
        )
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "email_verification_continue_hint_failed")


class EmailVerificationContinueHintGateTests(unittest.TestCase):
    """``_signup_email_verification_continue_hint_enabled`` 的取值语义。"""

    def _gate_with(self, registration_config):
        with patch.object(auth_flow.deps, "current_config_data", return_value={"registration": registration_config}):
            return auth_flow.steps._signup_email_verification_continue_hint_enabled()

    def test_default_is_off_when_key_absent(self):
        self.assertFalse(self._gate_with({}))

    def test_false_spellings_are_off(self):
        for value in (False, 0, "0", "false", "False", "no", "No", "off"):
            with self.subTest(value=value):
                self.assertFalse(self._gate_with({"signup_email_verification_continue_hint": value}))

    def test_true_spellings_are_on(self):
        for value in (True, 1, "1", "true", "True", "yes", "Yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(self._gate_with({"signup_email_verification_continue_hint": value}))

    def test_a_frozen_registration_section_is_read(self):
        """生产配置段是 ``mappingproxy`` 不是 ``dict``（P1-D 类缺陷回归）。"""
        frozen = MappingProxyType({"signup_email_verification_continue_hint": True})
        with patch.object(auth_flow.deps, "current_config_data", return_value={"registration": frozen}):
            self.assertTrue(auth_flow.steps._signup_email_verification_continue_hint_enabled())

    def test_non_mapping_registration_is_off(self):
        with patch.object(auth_flow.deps, "current_config_data", return_value={"registration": "oops"}):
            self.assertFalse(auth_flow.steps._signup_email_verification_continue_hint_enabled())

    def test_config_failure_is_off(self):
        with patch.object(auth_flow.deps, "current_config_data", side_effect=RuntimeError("boom")):
            self.assertFalse(auth_flow.steps._signup_email_verification_continue_hint_enabled())


if __name__ == "__main__":
    unittest.main()
