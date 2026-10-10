"""The create_account stage's two opt-in wire behaviors (P1-6 / P1-7).

``create_account`` historically issued one Sentinel, POSTed once, and parsed.
Two reference-informed behaviors now exist behind default-off toggles:

* **P1-6** — ``registration.prime_about_you_page``: GET ``/about-you`` before
  the POST, so the page state exists (turb ``navigate_about_you``).  The POST
  already claims that state via its Referer; the prime is the same gap the
  password page had.  Non-fatal by contract: a failed prime must never abort
  a stage that works without it.
* **P1-7** — ``registration.create_account_disallowed_backoff``: retry the
  POST on ``registration_disallowed`` with bounded backoff (``[8, 20, 45]``s,
  SunnyRegister's measured ladder) and a **fresh Sentinel proof each round**.
  Off by default because the failure classifies terminal (``account``) today.

What these tests pin:

1. Default shape is byte-for-byte today's: no extra GET, exactly one POST, one
   Sentinel.
2. The prime's failure paths (transport raise, off-page landing) are reported
   and swallowed -- never fatal.
3. The backoff only fires on a body containing ``registration_disallowed``;
   other non-200 bodies still terminate on the first attempt.  The final
   attempt's response is what the stage parses, so the terminal verdict is
   unchanged.
4. Backoff sleeps are cancellable (``cancellable_sleep`` returning True raises
   ``RegistrationCancelled``), and each retry re-mints the Sentinel.

Two fixture conventions, both load-bearing:

* Classes inherit ``unittest.TestCase`` deliberately: this suite mixes plain
  pytest functions and TestCase classes, and a bare class silently collects
  zero tests -- the failure mode this file originally shipped with.
* The ``SimpleNamespace`` operations stub stays in a **local** variable and is
  injected with ``object.__setattr__``; asserting against the local keeps the
  static checker away from the ``RegistrationOperations`` declaration types
  (a ``Mock`` assigned into that interface reads back as ``FunctionType``),
  the same trap ``test_user_register_response_contract.py`` documents.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sms_tool import registration_handlers as rh
from sms_tool.registration_cancel import RegistrationCancelled
from sms_tool.registration_handlers import RegistrationEmailWorkflow


def _response(status=200, body=None, text=""):
    response = Mock()
    response.status_code = status
    payload = body if body is not None else {}
    response.json.return_value = payload
    response.text = text or json.dumps(payload)
    return response


def _state():
    return SimpleNamespace(
        session=Mock(),
        auth_base="https://auth.openai.com",
        base_headers={"X-Base": "1"},
        device_id="did-1",
        full_name="Test User",
        birthdate="1995-06-15",
        create_data=None,
        create_ok=False,
        existing_account=False,
        # ``create_account`` reads ``s.context`` for the
        # ``existing_account_password_known`` branch; a 200 path with no
        # existing-account verdict never touches its fields, so ``None`` is a
        # faithful stand-in for the identity context.
        context=None,
        # ``registration.otp_external_url_branch`` (2026-10-10): the handler
        # skips the POST when this is set.  Empty is the default shape, so the
        # tests here exercise the unchanged path.
        otp_external_url="",
        # The 200 path persists the SESSION_PENDING checkpoint on the way out.
        username="user@example.com",
    )


def _workflow(state, *, responses, config=None):
    """Build a bare workflow plus its **local** operations stub: ``(w, ops)``."""
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = config if config is not None else {}
    w.runtime = state
    w._persist_checkpoint = Mock()
    sentinel_mint = Mock(side_effect=[SimpleNamespace(token=f"T{i}", so_token=f"S{i}") for i in range(10)])
    object.__setattr__(w, "_issue_sentinel", sentinel_mint)
    ops = SimpleNamespace(
        request_with_retry=Mock(side_effect=list(responses)),
        _auth_request_headers=Mock(return_value={}),
        auth_impersonate=Mock(return_value=""),
        _sanitize_text=str,
        _json_or_raw=lambda resp, limit=1000: resp.json(),
        _follow_continue_url=Mock(return_value=Mock(url="https://auth.openai.com/about-you")),
        _is_user_already_exists=Mock(return_value=False),
        think_stage=Mock(),
    )
    object.__setattr__(w, "_operations", ops)
    # The mint stub is returned alongside so assertions stay on typed locals.
    object.__setattr__(w, "_sentinel_mint_for_test", sentinel_mint)
    return w, ops, sentinel_mint


# ---------------------------------------------------------------------------
# P1-6: about-you 页面 prime
# ---------------------------------------------------------------------------


class PrimeAboutYouPageTests(unittest.TestCase):
    def test_default_does_not_prime(self):
        """默认关 ⇒ 请求形状与今天完全一致：只有一次 POST，无前置 GET。"""
        state = _state()
        w, ops, mint = _workflow(state, responses=[_response(200)])
        ops._follow_continue_url = Mock()

        w.create_account()

        self.assertEqual(ops._follow_continue_url.call_count, 0)
        self.assertEqual(ops.request_with_retry.call_count, 1)

    def test_enabled_gets_about_you_before_the_post(self):
        state = _state()
        w, ops, mint = _workflow(
            state, responses=[_response(200)], config={"registration": {"prime_about_you_page": True}}
        )

        calls: list[str] = []
        ops._follow_continue_url = Mock(
            side_effect=lambda *a, **kw: calls.append("prime") or Mock(url="https://auth.openai.com/about-you")
        )
        ops.request_with_retry = Mock(side_effect=lambda *a, **kw: calls.append("post") or _response(200))

        w.create_account()

        self.assertEqual(calls, ["prime", "post"])
        # The prime navigates to the profile page with the right referer.
        args, kwargs = ops._follow_continue_url.call_args
        self.assertEqual(args[1], "https://auth.openai.com/about-you")
        self.assertEqual(kwargs["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(kwargs["label"], "About you page prime")

    def test_an_off_page_landing_is_reported_not_fatal(self):
        """落点不在 profile 步 ⇒ 打一行警告，POST 照常进行。"""
        state = _state()
        w, ops, mint = _workflow(
            state, responses=[_response(200)], config={"registration": {"prime_about_you_page": True}}
        )
        ops._follow_continue_url = Mock(return_value=Mock(url="https://auth.openai.com/log-in/password"))

        w.create_account()  # must not raise

        self.assertEqual(ops.request_with_retry.call_count, 1)
        self.assertTrue(state.create_ok)

    def test_a_prime_transport_failure_is_swallowed(self):
        """prime 传输炸掉 ⇒ 记警告继续；决不能把可用的 stage 变成新失败模式。"""
        state = _state()
        w, ops, mint = _workflow(
            state, responses=[_response(200)], config={"registration": {"prime_about_you_page": True}}
        )
        ops._follow_continue_url = Mock(side_effect=RuntimeError("proxy reset"))

        w.create_account()  # must not raise

        self.assertEqual(ops.request_with_retry.call_count, 1)
        self.assertTrue(state.create_ok)

    def test_the_off_page_landing_report_reaches_the_log_channel(self):
        """P1-A′: the prime's report line goes through ``emit``, not ``print``."""
        state = _state()
        w, ops, mint = _workflow(
            state, responses=[_response(200)], config={"registration": {"prime_about_you_page": True}}
        )
        ops._follow_continue_url = Mock(return_value=Mock(url="https://auth.openai.com/log-in/password"))

        with self.assertLogs("sms_tool.registration_handlers", level="INFO") as captured:
            w.create_account()

        self.assertIn("About you page prime landed off the profile step", "\n".join(captured.output))


# ---------------------------------------------------------------------------
# P1-7: registration_disallowed 有界退避
# ---------------------------------------------------------------------------


class CreateAccountDisallowedBackoffTests(unittest.TestCase):
    def test_retry_rounds_bypass_the_supplied_bundle_short_circuit(self):
        """每轮重试必须绕过 ``supplied_data`` 缓存短路，真正重铸 proof。

        离线复现：``_issue_sentinel`` 把铸好的 token 写回 ``s.sentinel_data``，
        下一轮再把它当 ``supplied_data`` 传给 ``issue_sentinel_flow`` ——
        ``_token_from_bundle`` 非空就直接返回缓存值，底层只铸了一次，两轮拿到
        同一个 token。“每轮刷新 Sentinel 证明”是谎话，重试重放的恰是服务端
        刚拒绝的那份。Mock 掉 ``_issue_sentinel`` 的旧断言看不到这一层。
        """
        state = _state()
        state.sentinel_data = {"sentinel_oauth_token": "bundled-token", "oai_did": "did-1"}
        state.sentinel_token = ""
        state.sentinel_authorize_token = ""
        state.sentinel_so_token = ""
        state.proxy = ""
        state.session_recovery_attempts = 0
        state.session_recovery_started_at = 0
        w = object.__new__(RegistrationEmailWorkflow)
        w.config = {"registration": {"create_account_disallowed_backoff": True}}
        object.__setattr__(w, "runtime", state)
        w._persist_checkpoint = Mock()
        ops = SimpleNamespace(
            request_with_retry=Mock(
                side_effect=[
                    _response(400, {"error": {"code": "registration_disallowed"}}),
                    _response(200),
                ]
            ),
            _auth_request_headers=Mock(return_value={}),
            auth_impersonate=Mock(return_value=""),
            _sanitize_text=str,
            _json_or_raw=lambda resp, limit=1000: resp.json(),
            _follow_continue_url=Mock(return_value=Mock(url="https://auth.openai.com/about-you")),
            _is_user_already_exists=Mock(return_value=False),
            _create_account_continue_url=Mock(return_value=""),
            think_stage=Mock(),
        )
        object.__setattr__(w, "_operations", ops)
        minted = [SimpleNamespace(token=f"fresh-{i}", so_token=f"so-{i}", device_id="did-1") for i in range(3)]

        with (
            patch.object(rh, "cancellable_sleep", return_value=False),
            patch("sms_tool.sentinel.issue_sentinel_flow", side_effect=minted) as mint,
        ):
            w.create_account()

        self.assertEqual(mint.call_count, 2)
        # Round 0 may consume the pre-minted bundle; the retry must not.
        self.assertEqual(
            mint.call_args_list[0].kwargs["supplied_data"],
            {"sentinel_oauth_token": "bundled-token", "oai_did": "did-1"},
        )
        self.assertIsNone(mint.call_args_list[1].kwargs["supplied_data"])
        # The retried POST is built with the fresh proof, not the bundle token
        # the server just rejected.
        header_args = ops._auth_request_headers.call_args_list[1].kwargs
        self.assertEqual(header_args["sentinel_token"], "fresh-1")
        self.assertEqual(header_args["sentinel_so_token"], "so-1")

    def test_default_is_single_attempt_even_on_disallowed(self):
        """默认关 ⇒ 一次 POST；registration_disallowed 照旧终态，无重试。"""
        state = _state()
        w, ops, mint = _workflow(state, responses=[_response(400, {"error": {"code": "registration_disallowed"}})])

        with patch.object(rh, "cancellable_sleep") as sleep:
            w.create_account()

        self.assertEqual(ops.request_with_retry.call_count, 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertFalse(state.create_ok)

    def test_backoff_retries_disallowed_with_fresh_sentinel_each_round(self):
        """开启后：disallowed 触发 [8,20,45]s 退避，每轮重新铸造 Sentinel。"""
        state = _state()
        w, ops, mint = _workflow(
            state,
            responses=[
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(200),
            ],
            config={"registration": {"create_account_disallowed_backoff": True}},
        )

        with patch.object(rh, "cancellable_sleep", return_value=False) as sleep:
            w.create_account()

        self.assertEqual(ops.request_with_retry.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [8, 20])
        # Fresh proof per round: three mints for three attempts.
        self.assertEqual(mint.call_count, 3)
        self.assertTrue(state.create_ok)

    def test_backoff_exhaustion_terminates_on_the_final_attempt(self):
        """三次全 disallowed ⇒ 解析**最后一次**响应，终态判定不变（account 类）。"""
        state = _state()
        w, ops, mint = _workflow(
            state,
            responses=[
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(400, {"error": {"code": "registration_disallowed"}}),
            ],
            config={"registration": {"create_account_disallowed_backoff": True}},
        )

        with patch.object(rh, "cancellable_sleep", return_value=False) as sleep:
            w.create_account()

        # 4 attempts total (1 + 3 retries), 3 sleeps, then the last response is parsed.
        self.assertEqual(ops.request_with_retry.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [8, 20, 45])
        self.assertFalse(state.create_ok)
        self.assertEqual(state.create_data["error"]["code"], "registration_disallowed")

    def test_other_non_200_bodies_terminate_immediately(self):
        """非 disallowed 的失败（如 user_already_exists）不重试。"""
        state = _state()
        w, ops, mint = _workflow(
            state,
            responses=[_response(400, {"error": {"code": "user_already_exists"}})],
            config={"registration": {"create_account_disallowed_backoff": True}},
        )

        with patch.object(rh, "cancellable_sleep") as sleep:
            w.create_account()

        self.assertEqual(ops.request_with_retry.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_cancellation_during_backoff_sleep_raises(self):
        """退避睡眠中被取消 ⇒ RegistrationCancelled（协作取消语义）。"""
        state = _state()
        w, ops, mint = _workflow(
            state,
            responses=[
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(200),
            ],
            config={"registration": {"create_account_disallowed_backoff": True}},
        )

        with patch.object(rh, "cancellable_sleep", return_value=True):
            with pytest.raises(RegistrationCancelled):
                w.create_account()

    def test_the_retry_line_reaches_the_log_channel(self):
        """P1-A′: P1-7's registered mechanism marker must be greppable in ``sms_tool.log``."""
        state = _state()
        w, ops, mint = _workflow(
            state,
            responses=[
                _response(400, {"error": {"code": "registration_disallowed"}}),
                _response(200),
            ],
            config={"registration": {"create_account_disallowed_backoff": True}},
        )

        with patch.object(rh, "cancellable_sleep", return_value=False):
            with self.assertLogs("sms_tool.registration_handlers", level="INFO") as captured:
                w.create_account()

        self.assertIn("Create account temporarily disallowed", "\n".join(captured.output))


# ---------------------------------------------------------------------------
# 开关门禁
# ---------------------------------------------------------------------------


class ToggleGateTests(unittest.TestCase):
    def _gate(self, config, attr):
        w = object.__new__(RegistrationEmailWorkflow)
        w.config = config
        return getattr(RegistrationEmailWorkflow, attr)(w)

    def test_prime_about_you_gate_defaults_off(self):
        self.assertFalse(self._gate({}, "_prime_about_you_page_enabled"))
        self.assertFalse(self._gate({"registration": {}}, "_prime_about_you_page_enabled"))

    def test_prime_about_you_gate_truth_table(self):
        self.assertTrue(self._gate({"registration": {"prime_about_you_page": True}}, "_prime_about_you_page_enabled"))
        self.assertTrue(self._gate({"registration": {"prime_about_you_page": "true"}}, "_prime_about_you_page_enabled"))
        self.assertFalse(self._gate({"registration": {"prime_about_you_page": False}}, "_prime_about_you_page_enabled"))
        self.assertFalse(
            self._gate({"registration": {"prime_about_you_page": "false"}}, "_prime_about_you_page_enabled")
        )

    def test_backoff_gate_defaults_to_empty(self):
        self.assertEqual(self._gate({}, "_create_account_disallowed_backoff_delays"), ())
        self.assertEqual(
            self._gate(
                {"registration": {"create_account_disallowed_backoff": False}},
                "_create_account_disallowed_backoff_delays",
            ),
            (),
        )

    def test_backoff_gate_ladder(self):
        self.assertEqual(
            self._gate(
                {"registration": {"create_account_disallowed_backoff": True}},
                "_create_account_disallowed_backoff_delays",
            ),
            (8, 20, 45),
        )

    def test_the_gates_answer_their_default_for_an_unrecognised_value(self):
        """P1-C′: the two workflow toggles share the two-sided ``registration_flag``.

        Before the consolidation each hand-rolled its own truthy whitelist, so a
        typo in the config was silently treated as one of three different things.
        """
        self.assertFalse(self._gate({"registration": {"prime_about_you_page": "maybe"}}, "_prime_about_you_page_enabled"))
        self.assertEqual(
            self._gate(
                {"registration": {"create_account_disallowed_backoff": "maybe"}},
                "_create_account_disallowed_backoff_delays",
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
