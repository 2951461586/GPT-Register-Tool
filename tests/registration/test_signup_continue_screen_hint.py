"""The signup continue body's optional ``screen_hint: "signup"`` declaration.

``_continue_signup_username`` posts ``authorize/continue`` from the signup lane.
Its body historically carried only ``username``; the login lane gained an
explicit ``screen_hint: "login"`` after the 09-14/09-16 measurements (see
``_existing_login_continue_enabled``), and both working reference clients
declare the screen on every continue.  The signup declaration lives behind
``registration.signup_continue_screen_hint`` (default **False**) because the
request currently *works* on some exits -- a wire change to a working lane
needs its own A/B (``p1-5-signup-continue-screen-hint``) before it earns a
default.

Three things this file pins:

1. Default **off** keeps today's request shape byte-for-byte: ``username``
   only, no ``screen_hint`` key at all -- not an empty/None value, which is a
   different request shape than absence.
2. On, the body carries ``screen_hint: "signup"`` alongside the untouched
   ``username`` entry.
3. The gate is read per call from ``steps._signup_continue_screen_hint_enabled``
   (module-qualified patch seam), so a config reload between attempts changes
   the body -- and the rest of the request (headers, sentinel pair, endpoint)
   is identical in both modes.

Classes inherit ``unittest.TestCase`` deliberately: this suite mixes plain
pytest functions and TestCase classes, and a bare class silently collects zero
tests -- the failure mode this file originally shipped with.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sms_tool import auth_flow
from sms_tool.auth_flow import signup as signup_module


def _run_continue(*, enabled: bool, sentinel=("tok-1", "so-1")):
    """Drive one ``_continue_signup_username`` call with the transport captured.

    The ``current_url`` must NOT be a password/email-verification step: the
    function's entry guard returns ``skipped`` for those landings without
    posting anything.  Use an authorize landing, the shape the caller actually
    hands over when the continue POST happens.
    """
    response = _make_response()
    with (
        patch.object(auth_flow.deps, "request_with_retry", return_value=response) as request,
        patch.object(auth_flow.steps, "_signup_continue_screen_hint_enabled", lambda: enabled),
        patch.object(auth_flow.deps, "_follow_continue_url", return_value=Mock(url=response.url)),
        patch.object(
            signup_module.sentinel_flow,
            "_authorize_continue_sentinel",
            return_value=({"sentinel_source": "test"}, sentinel[0], sentinel[1]),
        ),
    ):
        result = auth_flow._continue_signup_username(
            Mock(),
            "user@example.com",
            "device-id",
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/api/accounts/authorize",
            sentinel_token="stale-tok",
            sentinel_so_token="stale-so",
            proxy="",
        )
    assert result["ok"], result
    return request


def _make_response():
    response = Mock()
    response.status_code = 200
    response.url = "https://auth.openai.com/email-verification"
    response.headers = {}
    response.json.return_value = {"continue_url": "https://auth.openai.com/email-verification"}
    return response


class SignupContinueScreenHintTests(unittest.TestCase):
    def test_default_keeps_the_username_only_body(self):
        """开关关（默认）⇒ 请求形状与今天完全一致：只有 ``username``。

        ``screen_hint`` 缺席与 ``screen_hint=None`` 是**两种**请求形状；这里钉
        的是「键不存在」，与 ``_existing_login_continue_enabled`` 的默认分支同构。
        """
        request = _run_continue(enabled=False)
        body = request.call_args.kwargs["json"]
        self.assertEqual(body, {"username": {"value": "user@example.com", "kind": "email"}})
        self.assertNotIn("screen_hint", body)

    def test_enabled_declares_signup_on_the_body(self):
        request = _run_continue(enabled=True)
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["screen_hint"], "signup")
        # The username entry itself must be untouched -- the declaration rides
        # alongside it, never replaces it.
        self.assertEqual(body["username"], {"value": "user@example.com", "kind": "email"})

    def test_the_endpoint_and_sentinel_pair_are_identical_in_both_modes(self):
        """除 body 外，端点、Sentinel 头、方法都不得因开关漂移。

        两次运行的 session Mock 是不同实例（id 必然不同），所以位置参数只比
        method 与 URL。headers 比较排除三个**每次请求随机生成**的链路追踪头
        （traceparent / x-datadog-parent-id / x-datadog-trace-id）——它们本就
        逐请求变化，与开关无关；除这些外任何 header 键值差异都是漂移。
        """
        volatile = {"traceparent", "x-datadog-parent-id", "x-datadog-trace-id"}
        off = _run_continue(enabled=False)
        on = _run_continue(enabled=True)
        self.assertEqual(off.call_args.args[1:], on.call_args.args[1:])
        off_kwargs = dict(off.call_args.kwargs)
        on_kwargs = dict(on.call_args.kwargs)
        off_headers = {k: v for k, v in off_kwargs["headers"].items() if k.lower() not in volatile}
        on_headers = {k: v for k, v in on_kwargs["headers"].items() if k.lower() not in volatile}
        self.assertEqual(set(off_kwargs["headers"]), set(on_kwargs["headers"]))
        self.assertEqual(off_headers, on_headers)
        self.assertEqual(off_kwargs["impersonate"], on_kwargs["impersonate"])
        self.assertEqual(off_kwargs["label"], on_kwargs["label"])
        # The only difference is the request body itself.
        self.assertNotEqual(off_kwargs["json"], on_kwargs["json"])


class SignupContinueGateTests(unittest.TestCase):
    """``_signup_continue_screen_hint_enabled`` 的取值语义（真值表钉死）。"""

    def _gate_with(self, registration_config):
        with patch.object(auth_flow.deps, "current_config_data", return_value={"registration": registration_config}):
            return auth_flow.steps._signup_continue_screen_hint_enabled()

    def test_default_is_off_when_key_absent(self):
        self.assertFalse(self._gate_with({}))

    def test_explicit_false_is_off(self):
        self.assertFalse(self._gate_with({"signup_continue_screen_hint": False}))

    def test_string_false_is_off(self):
        self.assertFalse(self._gate_with({"signup_continue_screen_hint": "false"}))

    def test_true_is_on(self):
        self.assertTrue(self._gate_with({"signup_continue_screen_hint": True}))

    def test_string_true_is_on(self):
        self.assertTrue(self._gate_with({"signup_continue_screen_hint": "true"}))

    def test_non_dict_registration_is_off(self):
        with patch.object(auth_flow.deps, "current_config_data", return_value={"registration": "oops"}):
            self.assertFalse(auth_flow.steps._signup_continue_screen_hint_enabled())

    def test_config_failure_is_off(self):
        """读配置炸了 ⇒ 关（fail closed），不许把默认语义翻转。"""
        with patch.object(auth_flow.deps, "current_config_data", side_effect=RuntimeError("boom")):
            self.assertFalse(auth_flow.steps._signup_continue_screen_hint_enabled())


if __name__ == "__main__":
    unittest.main()
