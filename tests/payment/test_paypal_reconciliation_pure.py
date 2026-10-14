r"""``sms_tool/paypal_link/reconciliation.py`` 的行为测试。

这个模块 1306 行，此前**零测试 import**（AST 复算确认，非"文本出现过"）。
它负责判定一笔 PayPal 支付最终是 succeeded / failed / cancelled ——
判错的后果是**把失败的付款记成成功**（或反之），且它同时是全链路唯一
"不得泄漏 query 参数 / token"的收口。

测试分层（不碰网络、不碰浏览器、不碰真钱）：

* URL 白名单校验 —— 拒绝非法来源，且错误消息不得回显 URL
* 状态词归一化 / 合并优先级 —— FAILED、CANCELLED 必须"粘住"
* 终局判定 —— 钱的结论从这里出
* 结果脱敏 —— ``to_dict()`` 是唯一对外出口，绝不能带出 URL / secret
* HTML 候选提取 —— 跳过 script/style，避免把脚本内容当支付状态

``reconcile_paypal_return`` 的编排分支用 fake transport 驱动，不发起真实请求。
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from sms_tool.paypal_link import reconciliation as rec
from sms_tool.paypal_link.reconciliation import (
    NormalizedReturnState,
    PaymentOutcome,
    ReconciliationClassification,
    ReconciliationHop,
    RemoteStatus,
    ReturnStage,
    ReturnURLValidationError,
    _Evidence,
    _first,
    _make_result,
    _merge_status,
    _normalize_remote_status,
    _validate_return_url,
    _validate_transition,
    normalize_return_state,
)


def _hop(index: int = 0, stage: ReturnStage = ReturnStage.CHECKOUT_VERIFY,
         **kw: Any) -> ReconciliationHop:
    base = dict(index=index, stage=stage, host="chatgpt.com", status_code=200)
    base.update(kw)
    return ReconciliationHop(**base)


class ReturnURLValidationTests(unittest.TestCase):
    """白名单校验：非法来源必须挡在门外。"""

    def _code(self, url: str) -> str:
        with self.assertRaises(ReturnURLValidationError) as raised:
            _validate_return_url(url)
        return raised.exception.code

    def test_https_is_required(self):
        self.assertEqual(self._code("http://chatgpt.com/"), "https_required")

    def test_userinfo_is_forbidden(self):
        self.assertEqual(
            self._code("https://user:pw@chatgpt.com/"), "userinfo_forbidden")

    def test_non_standard_port_is_forbidden(self):
        self.assertEqual(self._code("https://chatgpt.com:8443/"), "port_forbidden")

    def test_port_443_is_allowed(self):
        _parsed, stage = _validate_return_url("https://chatgpt.com:443/")
        self.assertIs(stage, ReturnStage.CHATGPT_LANDING)

    def test_host_must_be_allowlisted(self):
        self.assertEqual(self._code("https://evil.example.com/"), "host_not_allowed")

    def test_suffix_spoofing_is_rejected(self):
        """chatgpt.com.evil.com 不能因为'包含'白名单就放行。"""
        self.assertEqual(self._code("https://chatgpt.com.evil.example/"), "host_not_allowed")

    def test_empty_and_oversized(self):
        self.assertEqual(self._code(""), "empty_url")
        self.assertEqual(self._code("https://chatgpt.com/?q=" + "a" * 20_000),
                         "url_too_long")

    def test_control_characters_are_rejected(self):
        self.assertEqual(self._code("https://chatgpt.com/\x00"), "invalid_url")
        self.assertEqual(self._code("https://chatgpt.com/\x7f"), "invalid_url")

    def test_stage_mapping(self):
        cases = [
            ("https://pm-redirects.stripe.com/return", ReturnStage.STRIPE_RETURN),
            ("https://pm-redirects.stripe.com/authorize/x", ReturnStage.STRIPE_RETURN),
            ("https://pay.openai.com/c/pay/pay_123", ReturnStage.OPENAI_PAY),
            ("https://chatgpt.com/checkout/verify", ReturnStage.CHECKOUT_VERIFY),
            ("https://chatgpt.com/", ReturnStage.CHATGPT_LANDING),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                _parsed, stage = _validate_return_url(url)
                self.assertIs(stage, expected)

    def test_stripe_path_must_be_allowlisted(self):
        self.assertEqual(self._code("https://pm-redirects.stripe.com/admin"),
                         "path_not_allowed")

    def test_openai_pay_requires_an_id(self):
        for path in ("/c/pay/", "/c/pay/", "/c/pay/a/b", "/c/pay/.."):
            with self.subTest(path=path):
                self.assertEqual(self._code(f"https://pay.openai.com{path}"),
                                 "path_not_allowed")

    def test_error_messages_never_echo_the_url(self):
        """错误消息要能安全写进日志 —— 所以不能带 query 参数。"""
        secret_url = "https://evil.example.com/return?setup_intent_client_secret=pi_9sUPERSECRET"
        with self.assertRaises(ReturnURLValidationError) as raised:
            _validate_return_url(secret_url)
        message = str(raised.exception)
        self.assertNotIn("SUPERSECRET", message)
        self.assertNotIn("evil.example.com", message)
        self.assertNotIn("setup_intent_client_secret", message)

    def test_error_codes_are_stable(self):
        """callers 按 code 分支，不是按文案 —— 改文案没事，改 code 是破坏性变更。"""
        expected = {
            "": "empty_url",
            "http://chatgpt.com/": "https_required",
            "https://a:b@chatgpt.com/": "userinfo_forbidden",
            "https://chatgpt.com:1/": "port_forbidden",
            "https://nope.example/": "host_not_allowed",
        }
        for url, code in expected.items():
            with self.subTest(url=url):
                self.assertEqual(self._code(url), code)


class RemoteStatusNormalizationTests(unittest.TestCase):
    """状态词 -> 枚举。这里是表驱动，加词要在表里加并配用例。"""

    def test_known_words(self):
        cases = {
            "success": RemoteStatus.SUCCEEDED,
            "Succeeded": RemoteStatus.SUCCEEDED,
            " COMPLETED ": RemoteStatus.SUCCEEDED,
            "paid": RemoteStatus.SUCCEEDED,
            "failed": RemoteStatus.FAILED,
            "requires-payment-method": RemoteStatus.FAILED,  # 连字符归一成下划线
            "declined": RemoteStatus.FAILED,
            "cancelled": RemoteStatus.CANCELLED,
            "canceled": RemoteStatus.CANCELLED,
            "pending": RemoteStatus.PENDING,
            "requires_action": RemoteStatus.PENDING,
            "in_progress": RemoteStatus.PENDING,
            "something-else": RemoteStatus.UNKNOWN,
            "": RemoteStatus.UNKNOWN,
            None: RemoteStatus.UNKNOWN,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIs(_normalize_remote_status(raw), expected)

    def test_cancelled_and_failed_are_distinct(self):
        """取消与失败是两种终局，不能归并 —— 重试策略不同。"""
        self.assertIs(_normalize_remote_status("canceled"), RemoteStatus.CANCELLED)
        self.assertIs(_normalize_remote_status("failed"), RemoteStatus.FAILED)


class MergeStatusTests(unittest.TestCase):
    """多跳证据合并：坏消息必须粘住，不能被后来的好消息冲掉。

    唯一例外：FAILED 与 CANCELLED 相遇时是 **observed 优先**（谁新听谁的），
    不是"FAILED 恒赢" —— 所以 sticky 断言要把这两个互相撞上的组合排除。
    """

    _TERMINAL_BAD = {RemoteStatus.FAILED, RemoteStatus.CANCELLED}

    def test_failed_is_sticky_against_non_terminal_statuses(self):
        for other in RemoteStatus:
            if other in self._TERMINAL_BAD:
                continue
            with self.subTest(other=other):
                self.assertIs(_merge_status(RemoteStatus.FAILED, other),
                              RemoteStatus.FAILED)
                self.assertIs(_merge_status(other, RemoteStatus.FAILED),
                              RemoteStatus.FAILED)

    def test_cancelled_is_sticky_against_non_terminal_statuses(self):
        for other in RemoteStatus:
            if other in self._TERMINAL_BAD:
                continue
            with self.subTest(other=other):
                self.assertIs(_merge_status(RemoteStatus.CANCELLED, other),
                              RemoteStatus.CANCELLED)
                self.assertIs(_merge_status(other, RemoteStatus.CANCELLED),
                              RemoteStatus.CANCELLED)

    def test_bad_news_from_observed_wins_over_existing_unknown(self):
        self.assertIs(_merge_status(RemoteStatus.UNKNOWN, RemoteStatus.CANCELLED),
                      RemoteStatus.CANCELLED)

    def test_success_beats_pending_and_unknown(self):
        self.assertIs(_merge_status(RemoteStatus.PENDING, RemoteStatus.SUCCEEDED),
                      RemoteStatus.SUCCEEDED)
        self.assertIs(_merge_status(RemoteStatus.UNKNOWN, RemoteStatus.PENDING),
                      RemoteStatus.PENDING)

    def test_observed_terminal_wins_when_both_bad(self):
        """FAILED 撞 CANCELLED：听 observed（第二个参数）的。"""
        self.assertIs(_merge_status(RemoteStatus.FAILED, RemoteStatus.CANCELLED),
                      RemoteStatus.CANCELLED)
        self.assertIs(_merge_status(RemoteStatus.CANCELLED, RemoteStatus.FAILED),
                      RemoteStatus.FAILED)

    def test_merge_is_idempotent(self):
        for status in RemoteStatus:
            with self.subTest(status=status):
                self.assertIs(_merge_status(status, status), status)


class EvidenceTerminalOutcomeTests(unittest.TestCase):
    """钱的终局判定 —— 这个模块最关键的一段逻辑。"""

    def test_no_evidence_means_no_terminal_outcome(self):
        ev = _Evidence()
        self.assertIsNone(ev.terminal_outcome())
        self.assertFalse(ev.has_success_evidence())

    def test_failure_precedes_cancellation(self):
        """响应里先判 FAILED 再判 CANCELLED —— 顺序写在实现里，这里钉住。"""
        ev = _Evidence(response_status=RemoteStatus.FAILED,
                       redirect_status=RemoteStatus.CANCELLED)
        self.assertIs(ev.terminal_outcome(), PaymentOutcome.FAILED)

    def test_cancelled_when_only_cancelled_present(self):
        ev = _Evidence(redirect_status=RemoteStatus.CANCELLED)
        self.assertIs(ev.terminal_outcome(), PaymentOutcome.CANCELLED)

    def test_priority_is_response_then_redirect_then_stripe(self):
        ev = _Evidence(stripe_return_status=RemoteStatus.FAILED,
                       redirect_status=RemoteStatus.CANCELLED,
                       response_status=RemoteStatus.UNKNOWN)
        # redirect 在 stripe 之前 -> CANCELLED 胜出
        self.assertIs(ev.terminal_outcome(), PaymentOutcome.CANCELLED)

        ev2 = _Evidence(stripe_return_status=RemoteStatus.CANCELLED,
                        response_status=RemoteStatus.FAILED)
        self.assertIs(ev2.terminal_outcome(), PaymentOutcome.FAILED)

    def test_success_evidence_ignores_stripe_return_status(self):
        """stripe_return_status 的 SUCCEEDED 不算成功证据 —— 只有 response/redirect 算。"""
        ev = _Evidence(stripe_return_status=RemoteStatus.SUCCEEDED)
        self.assertFalse(ev.has_success_evidence())
        ev.response_status = RemoteStatus.SUCCEEDED
        self.assertTrue(ev.has_success_evidence())

    def test_pending_is_not_terminal(self):
        ev = _Evidence(response_status=RemoteStatus.PENDING)
        self.assertIsNone(ev.terminal_outcome())

    def test_observe_url_accumulates(self):
        ev = _Evidence()
        state = NormalizedReturnState(
            stage=ReturnStage.CHECKOUT_VERIFY,
            host="chatgpt.com",
            redirect_status=RemoteStatus.SUCCEEDED,
            stripe_return_status=RemoteStatus.UNKNOWN,
            has_setup_intent=True,
            has_client_secret=False,
            has_success_return_url=True,
            success_return_stage=None,
        )
        ev.observe_url(state)
        self.assertTrue(ev.observed_setup_intent)
        self.assertTrue(ev.observed_success_return_url)
        self.assertTrue(ev.reached_verify)
        self.assertFalse(ev.observed_client_secret)

    def test_observed_flags_are_sticky_once_true(self):
        ev = _Evidence()
        ev.observed_client_secret = True
        ev.observe_url(NormalizedReturnState(
            stage=ReturnStage.CHATGPT_LANDING, host="chatgpt.com",
            redirect_status=RemoteStatus.UNKNOWN,
            stripe_return_status=RemoteStatus.UNKNOWN,
            has_setup_intent=False, has_client_secret=False,
            has_success_return_url=False, success_return_stage=None,
        ))
        self.assertTrue(ev.observed_client_secret)


class StageTransitionTests(unittest.TestCase):
    def test_allowed_transitions(self):
        allowed = [
            (ReturnStage.STRIPE_RETURN, ReturnStage.OPENAI_PAY),
            (ReturnStage.STRIPE_RETURN, ReturnStage.CHECKOUT_VERIFY),
            (ReturnStage.OPENAI_PAY, ReturnStage.CHECKOUT_VERIFY),
            (ReturnStage.CHECKOUT_VERIFY, ReturnStage.CHATGPT_LANDING),
        ]
        for cur, nxt in allowed:
            with self.subTest(cur=cur, nxt=nxt):
                _validate_transition(cur, nxt)  # 不抛即可

    def test_backwards_transition_is_rejected(self):
        """已经到 landing 就不能再跳回 verify —— 防的是重放/伪造跳转链。"""
        with self.assertRaises(ReturnURLValidationError) as raised:
            _validate_transition(ReturnStage.CHATGPT_LANDING,
                                 ReturnStage.CHECKOUT_VERIFY)
        self.assertEqual(raised.exception.code, "invalid_stage_transition")

    def test_stripe_cannot_go_straight_to_landing(self):
        with self.assertRaises(ReturnURLValidationError):
            _validate_transition(ReturnStage.STRIPE_RETURN,
                                 ReturnStage.CHATGPT_LANDING)

    def test_self_transitions_are_allowed(self):
        for stage in ReturnStage:
            with self.subTest(stage=stage):
                _validate_transition(stage, stage)


class NormalizeReturnStateTests(unittest.TestCase):
    def test_stripe_return_reads_status_not_redirect_status(self):
        state = normalize_return_state(
            "https://pm-redirects.stripe.com/return?status=failed"
            "&redirect_status=succeeded"
        )
        self.assertIs(state.stripe_return_status, RemoteStatus.FAILED)
        self.assertIs(state.redirect_status, RemoteStatus.SUCCEEDED)

    def test_non_stripe_host_ignores_status_query(self):
        state = normalize_return_state(
            "https://chatgpt.com/checkout/verify?status=failed")
        self.assertIs(state.stripe_return_status, RemoteStatus.UNKNOWN)

    def test_secret_flags_are_detected(self):
        state = normalize_return_state(
            "https://chatgpt.com/checkout/verify"
            "?setup_intent=seti_1&setup_intent_client_secret=pi_secret_1"
        )
        self.assertTrue(state.has_setup_intent)
        self.assertTrue(state.has_client_secret)

    def test_success_return_url_with_illegal_path_is_rejected_early(self):
        """/admin 连 URL 白名单都过不了 —— 挡在 _validate_return_url 那一层。

        错误码是 path_not_allowed 而不是 invalid_success_return_url，
        因为路径校验发生在"判断它是不是允许的 success 目标"之前。
        """
        with self.assertRaises(ReturnURLValidationError) as raised:
            normalize_return_state(
                "https://pm-redirects.stripe.com/return"
                "?success_return_url=https%3A%2F%2Fchatgpt.com%2Fadmin"
            )
        self.assertEqual(raised.exception.code, "path_not_allowed")

    def test_success_return_url_must_target_allowed_route(self):
        """URL 本身合法、但 stage 不是 verify/landing -> invalid_success_return_url。

        用 stripe return 当 success 目标是天然的触发例：它是白名单 URL，
        但 stage=STRIPE_RETURN，不属于允许的落地阶段。
        """
        with self.assertRaises(ReturnURLValidationError) as raised:
            normalize_return_state(
                "https://pm-redirects.stripe.com/return"
                "?success_return_url=https%3A%2F%2Fpm-redirects.stripe.com%2Freturn"
            )
        self.assertEqual(raised.exception.code, "invalid_success_return_url")

    def test_allowed_success_return_url_records_its_stage(self):
        state = normalize_return_state(
            "https://pm-redirects.stripe.com/return"
            "?success_return_url=https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify"
        )
        self.assertTrue(state.has_success_return_url)
        self.assertIs(state.success_return_stage, ReturnStage.CHECKOUT_VERIFY)

    def test_normalized_state_carries_no_secrets(self):
        """to_dict 是落盘/上报的出口 —— 不能带 query 里的任何值。"""
        state = normalize_return_state(
            "https://chatgpt.com/checkout/verify?setup_intent_client_secret=LEAKME")
        blob = state.to_dict()
        for value in blob.values():
            self.assertNotIn("LEAKME", str(value))
        self.assertNotIn("LEAKME", repr(blob))


class ResultSanitizationTests(unittest.TestCase):
    """``to_dict()`` 是对外唯一出口，这里是脱敏的最后一道闸。"""

    def test_to_dict_shape(self):
        result = _make_result(
            ReconciliationClassification.CONCLUSIVE,
            PaymentOutcome.SUCCEEDED,
            retryable=False,
            error_stage=None,
            error_code=None,
            reason="ok",
            final_stage=ReturnStage.CHECKOUT_VERIFY,
            hops=(_hop(0), _hop(1, stage=ReturnStage.CHATGPT_LANDING)),
        )
        blob = result.to_dict()
        self.assertTrue(blob["ok"])
        self.assertTrue(blob["conclusive"])
        self.assertEqual(blob["outcome"], "succeeded")
        self.assertIsNone(blob["error_code"])
        self.assertEqual(len(blob["hops"]), 2)
        self.assertEqual(blob["hops"][1]["stage"], "chatgpt_landing")

    def test_ok_requires_both_conclusive_and_succeeded(self):
        unknown = _make_result(
            ReconciliationClassification.UNKNOWN, PaymentOutcome.SUCCEEDED,
            retryable=True, error_stage=None, error_code=None, reason="?")
        self.assertFalse(unknown.ok, "UNKNOWN 分类下 outcome=succeeded 也不能算 ok")

        failed = _make_result(
            ReconciliationClassification.CONCLUSIVE, PaymentOutcome.FAILED,
            retryable=False, error_stage="stripe_return",
            error_code="remote_payment_failed", reason="no")
        self.assertFalse(failed.ok)

    def test_to_dict_exposes_no_url_or_token(self):
        """即便 hop 里携带完整 URL 的 hash/host，也不得出现原始 query 值。"""
        result = _make_result(
            ReconciliationClassification.CONCLUSIVE,
            PaymentOutcome.SUCCEEDED,
            retryable=False, error_stage=None, error_code=None, reason="ok",
            final_stage=ReturnStage.CHECKOUT_VERIFY,
            hops=(_hop(host="chatgpt.com", status_code=200),),
        )
        blob = result.to_dict()
        serialized = repr(blob)
        for leak in ("setup_intent_client_secret", "pi_", "seti_"):
            self.assertNotIn(leak, serialized)

    def test_retryable_survives_serialization(self):
        result = _make_result(
            ReconciliationClassification.UNKNOWN, PaymentOutcome.UNKNOWN,
            retryable=True, error_stage=None, error_code="timeout", reason="slow")
        self.assertTrue(result.to_dict()["retryable"])

    def test_retryable_false_is_not_silently_flipped(self):
        """FAILED 的终局必须 retryable=False —— 重试一笔已失败的付款会重复扣款。

        （变异验证 M14 抓到的缺口：``retryable`` 恒为 True 时原测试全绿。）
        """
        result = _make_result(
            ReconciliationClassification.CONCLUSIVE, PaymentOutcome.FAILED,
            retryable=False, error_stage="stripe_return",
            error_code="remote_payment_failed", reason="no")
        self.assertFalse(result.retryable)
        self.assertFalse(result.to_dict()["retryable"])

    def test_retryable_only_matters_when_not_conclusive(self):
        """CONCLUSIVE 的结论不能被重试 —— 这是调用方分支的依据。"""
        conclusive = _make_result(
            ReconciliationClassification.CONCLUSIVE, PaymentOutcome.UNKNOWN,
            retryable=True, error_stage=None, error_code=None, reason="?")
        self.assertTrue(conclusive.conclusive)
        unknown = _make_result(
            ReconciliationClassification.UNKNOWN, PaymentOutcome.UNKNOWN,
            retryable=True, error_stage=None, error_code=None, reason="?")
        self.assertFalse(unknown.conclusive)


class CandidateHTMLParserTests(unittest.TestCase):
    """从 HTML 里挑候选跳转 URL —— 不能把脚本内容当成支付状态。"""

    def _urls(self, doc: str) -> list[str]:
        parser = rec._CandidateHTMLParser()
        parser.feed(doc)
        parser.close()
        return parser.urls

    def test_collects_anchor_hrefs(self):
        self.assertIn("/checkout/verify",
                      self._urls('<a href="/checkout/verify">go</a>'))

    def test_ignores_script_contents(self):
        doc = '<script>var u = "/checkout/verify";</script><a href="/ok">x</a>'
        urls = self._urls(doc)
        self.assertEqual(urls, ["/ok"])

    def test_ignores_style_and_template(self):
        doc = '<template><a href="/nope">x</a></template><a href="/yes">y</a>'
        self.assertEqual(self._urls(doc), ["/yes"])

    def test_meta_refresh_is_followed(self):
        doc = '<meta http-equiv="refresh" content="0; url=/checkout/verify">'
        self.assertEqual(self._urls(doc), ["/checkout/verify"])

    def test_text_parts_skip_ignored_blocks(self):
        parser = rec._CandidateHTMLParser()
        parser.feed("<script>SECRET_TEXT</script><p>visible</p>")
        parser.close()
        text = "".join(parser.text_parts)
        self.assertIn("visible", text)
        self.assertNotIn("SECRET_TEXT", text)


class _FakeResponse:
    """只提供 reconciliation 会读的三个属性：status_code / text / headers。"""

    def __init__(self, status_code: int = 200, text: str = "",
                 headers: Mapping[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})


class _FakeTransport:
    """按 URL 回放预置响应，并记录每一次调用。

    ``reconcile_paypal_return`` 要求 transport **不跟随重定向**，
    所以这里把每次调用的 kwargs 存下来，供测试断言。
    """

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self._responses = dict(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, timeout: object = None,
            allow_redirects: object = None) -> _FakeResponse:
        self.calls.append({"url": url, "timeout": timeout,
                           "allow_redirects": allow_redirects})
        response = self._responses.get(url, _FakeResponse(200, "", {}))
        if isinstance(response, BaseException):
            raise response
        return response


VERIFY_URL = "https://chatgpt.com/checkout/verify"
LANDING_URL = "https://chatgpt.com/"


class OrchestrationTests(unittest.TestCase):
    """``reconcile_paypal_return`` 的编排分支 —— fake transport 驱动，零网络。

    这里钉的是「什么情况下可重试」和「什么情况下算终局」，
    因为重试一笔已失败的付款 = 重复扣款。
    """

    def _run(self, source: Any, responses: Mapping[str, Any] | None = None,
             **kw: Any) -> tuple[Any, _FakeTransport]:
        transport = _FakeTransport(responses or {})
        result = rec.reconcile_paypal_return(source, transport=transport, **kw)
        return result, transport

    def test_input_validation_rejects_bad_knobs(self):
        url = VERIFY_URL
        self.assertEqual(self._run(url, max_hops=0)[0].error_code, "invalid_max_hops")
        self.assertEqual(self._run(url, max_hops=True)[0].error_code, "invalid_max_hops")
        self.assertEqual(self._run(url, timeout=0)[0].error_code, "invalid_timeout")
        self.assertEqual(
            rec.reconcile_paypal_return(url, transport=None).error_code,
            "invalid_transport")

    def test_missing_return_url(self):
        result, transport = self._run({})
        self.assertEqual(result.error_code, "missing_return_url")
        self.assertEqual(transport.calls, [])

    def test_start_url_must_pass_the_allowlist(self):
        result, transport = self._run("https://evil.example/return")
        self.assertEqual(result.error_code, "host_not_allowed")
        self.assertEqual(transport.calls, [])

    def test_cannot_start_from_a_landing_page(self):
        """/ landing 是终点不是起点 —— 从它开始意味着链路已被伪造或重放。"""
        result, transport = self._run(LANDING_URL)
        self.assertEqual(result.error_code, "invalid_start_stage")
        self.assertEqual(transport.calls, [])

    def test_terminal_status_in_query_short_circuits_the_transport(self):
        """query 里已经是 FAILED，就不该再发请求 —— 省一次网络，也防误判。"""
        result, transport = self._run(f"{VERIFY_URL}?redirect_status=failed")
        self.assertIs(result.outcome, PaymentOutcome.FAILED)
        self.assertTrue(result.conclusive)
        self.assertFalse(result.retryable)
        self.assertEqual(transport.calls, [])

    def test_transport_never_follows_redirects(self):
        """每跳都要先过白名单再请求 —— 不交给 transport 自动跟。"""
        _result, transport = self._run(
            VERIFY_URL, {VERIFY_URL: _FakeResponse(200, "payment was successful")})
        self.assertTrue(transport.calls)
        for call in transport.calls:
            with self.subTest(call=call):
                self.assertIs(call["allow_redirects"], False)

    def test_transport_error_is_retryable(self):
        result, _transport = self._run(
            VERIFY_URL, {VERIFY_URL: TimeoutError("boom")})
        self.assertEqual(result.error_code, "transport_error")
        self.assertTrue(result.retryable, "网络抖一下不该把付款判死")
        self.assertIs(result.outcome, PaymentOutcome.UNKNOWN)
        self.assertFalse(result.conclusive)
        # 异常类名做了清洗，不能把异常消息原样带出去
        self.assertNotIn("boom", result.reason)

    def test_503_is_retryable_but_401_is_not(self):
        transient, _ = self._run(VERIFY_URL, {VERIFY_URL: _FakeResponse(503)})
        self.assertEqual(transient.error_code, "transient_http_error")
        self.assertTrue(transient.retryable)

        auth, _ = self._run(VERIFY_URL, {VERIFY_URL: _FakeResponse(401)})
        self.assertEqual(auth.error_code, "authentication_required")
        self.assertFalse(auth.retryable, "没登录重试一万次也是没登录")

    def test_404_is_a_hard_failure(self):
        result, _ = self._run(VERIFY_URL, {VERIFY_URL: _FakeResponse(404)})
        self.assertEqual(result.error_code, "merchant_http_error")
        self.assertFalse(result.retryable)
        self.assertFalse(result.conclusive)

    def test_failure_marker_in_body_ends_the_chain(self):
        result, _ = self._run(
            VERIFY_URL, {VERIFY_URL: _FakeResponse(200, "<p>card was declined</p>")})
        self.assertIs(result.outcome, PaymentOutcome.FAILED)
        self.assertTrue(result.conclusive)
        self.assertFalse(result.retryable)
        self.assertEqual(result.error_code, "remote_payment_failed")

    def test_cancelled_marker_in_body_ends_the_chain(self):
        result, _ = self._run(
            VERIFY_URL, {VERIFY_URL: _FakeResponse(200, "payment cancelled")})
        self.assertIs(result.outcome, PaymentOutcome.CANCELLED)
        self.assertEqual(result.error_code, "remote_payment_cancelled")

    def test_success_marker_on_verify_is_conclusive(self):
        result, _ = self._run(
            VERIFY_URL, {VERIFY_URL: _FakeResponse(200, "payment was successful")})
        self.assertTrue(result.ok)
        self.assertIs(result.outcome, PaymentOutcome.SUCCEEDED)
        self.assertFalse(result.retryable)

    def test_no_terminal_evidence_stays_pending(self):
        """拿不到终局证据就报 pending —— 不能默认成功，也不能默认失败。"""
        result, _ = self._run(VERIFY_URL, {VERIFY_URL: _FakeResponse(200, "")})
        self.assertEqual(result.error_code, "payment_pending")
        self.assertTrue(result.retryable)
        self.assertIs(result.outcome, PaymentOutcome.UNKNOWN)

    def test_landing_without_success_evidence_is_not_ok(self):
        """跳到了 landing 但没看到成功证据 —— 可疑，但不能判成功。"""
        result, _ = self._run(
            VERIFY_URL,
            {VERIFY_URL: _FakeResponse(302, "", {"location": LANDING_URL})})
        self.assertEqual(result.error_code, "landing_without_success_evidence")
        self.assertIs(result.final_stage, ReturnStage.CHATGPT_LANDING)
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)

    def test_redirect_loop_is_detected(self):
        result, _ = self._run(
            VERIFY_URL,
            {VERIFY_URL: _FakeResponse(302, "", {"location": "/checkout/verify"})})
        self.assertEqual(result.error_code, "redirect_loop")
        self.assertFalse(result.retryable)

    def test_illegal_redirect_target_is_rejected_before_requesting_it(self):
        result, transport = self._run(
            VERIFY_URL,
            {VERIFY_URL: _FakeResponse(302, "", {"location": "https://evil.example/x"})})
        self.assertEqual(result.error_code, "host_not_allowed")
        self.assertEqual(len(transport.calls), 1, "非法目标不该被真正请求")

    def test_hop_limit_is_enforced(self):
        chain = {VERIFY_URL: _FakeResponse(302, "", {"location": f"{VERIFY_URL}?n=1"}),
                 f"{VERIFY_URL}?n=1": _FakeResponse(302, "", {"location": f"{VERIFY_URL}?n=2"})}
        result, _ = self._run(VERIFY_URL, chain, max_hops=1)
        self.assertEqual(result.error_code, "max_hops_exceeded")

    def test_result_carries_no_secret_from_the_source_mapping(self):
        """source 里带 client_secret 是常态 —— 出口的 to_dict 不能带出来。"""
        result, _ = self._run(
            {"return_url": VERIFY_URL, "client_secret": "pi_SUPERSECRET_VALUE",
             "setup_intent": "seti_1"},
            {VERIFY_URL: _FakeResponse(200, "payment was successful")})
        self.assertTrue(result.ok,
                        "先确认链路真跑通了，否则脱敏断言在一个失败结果上也是绿的")
        blob = result.to_dict()
        self.assertNotIn("SUPERSECRET", repr(blob))
        self.assertTrue(blob["observed_setup_intent"])
        self.assertTrue(blob["observed_client_secret"],
                        "是否观察到 secret 是布尔位，值本身不外泄")

    def test_mapping_source_is_accepted(self):
        """调用方常传 dict 而不是裸 URL —— 认的是 _INPUT_URL_KEYS 白名单。"""
        for key in ("return_url", "returnURL", "returnUrl", "redirect_url",
                    "final_redirect_url", "success_return_url", "verification_url"):
            with self.subTest(key=key):
                result, _ = self._run(
                    {key: VERIFY_URL},
                    {VERIFY_URL: _FakeResponse(200, "payment was successful")})
                self.assertTrue(result.ok, f"{key} 没被认成起始 URL")

    def test_plain_url_key_is_not_recognized(self):
        """⚠️ 坑：`url` / `href` 不在 _INPUT_URL_KEYS 白名单里。

        传 ``{"url": ...}`` 不会报错，而是静默变成 ``missing_return_url``
        （classification=failed，但这不是真的支付失败）。调用方如果这么传，
        所有 PayPal 单子都会被记成"需要人工对账"。
        """
        result, transport = self._run({"url": VERIFY_URL})
        self.assertEqual(result.error_code, "missing_return_url")
        self.assertEqual(transport.calls, [])

    def test_nested_mapping_source_is_accepted(self):
        """URL 常常埋在 result / data 这类容器里（最深 4 层）。"""
        result, _ = self._run(
            {"result": {"data": {"return_url": VERIFY_URL}}},
            {VERIFY_URL: _FakeResponse(200, "payment was successful")})
        self.assertTrue(result.ok)

    def test_backwards_redirect_is_rejected(self):
        """verify 之后又跳回 stripe return —— 跳转链被伪造，必须挡下。"""
        result, transport = self._run(
            VERIFY_URL,
            {VERIFY_URL: _FakeResponse(
                302, "", {"location": "https://pm-redirects.stripe.com/return"})})
        self.assertEqual(result.error_code, "invalid_stage_transition")
        self.assertFalse(result.retryable)
        self.assertEqual(len(transport.calls), 1, "非法目标不该被真正请求")


class QueryHelperTests(unittest.TestCase):
    def test_first_picks_keys_in_order_and_strips(self):
        query = {"b": ["  x  "], "c": ["y"]}
        self.assertEqual(_first(query, "a", "b", "c"), "x")
        self.assertEqual(_first(query, "c"), "y")

    def test_first_stops_at_present_but_blank_value(self):
        """已知怪癖（钉住现状，不是改 bug）：``parse_qs(keep_blank_values=True)``
        会给 `?a=` 产生 ``[""]`` —— 这是个**非空列表**，所以 ``if values`` 命中，
        ``_first`` 当场返回空串，**不再看后面的兜底键**。

        实际影响：`?setup_intent=&setup_intent_id=seti_1` 会被判成
        ``has_setup_intent=False``。调用方目前没有依赖兜底键的场景，
        所以这里只把行为钉住；真要改 ``_first`` 语义，这个用例必须先改。
        """
        query = {"a": [""], "b": ["  x  "], "c": ["y"]}
        self.assertEqual(_first(query, "a", "b"), "")

    def test_first_returns_empty_when_nothing_matches(self):
        self.assertEqual(_first({}, "a", "b"), "")
        self.assertEqual(_first({"a": []}, "a"), "")


if __name__ == "__main__":
    unittest.main()
