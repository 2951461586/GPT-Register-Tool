"""``sms_tool/codex_phone.py`` 的行为测试（零覆盖 → 全覆盖 + 变异验证）。

**为什么值得测**：手机验证是注册流程里唯一会把**真钱**和**账号存亡**绑在一起的环节。

- 走错分支 = 租来的号码既没完成也没取消 → **号码一直挂着继续计费**，注册却已经往下走了。
- ``_verify_with_reuse_pool`` 的成功/失败整形共用 ``result.get("ok")`` 一个判据，
  池子返回 ``{"ok": 0}`` 和 ``{"ok": True}`` 走的是完全不同的一组字段 —— 键名对不上，
  调用方拿到的就是一堆空串。
- ``_next_url`` 的三级兜底链决定注册下一步跳哪；断在任意一级，账号就卡在验证页。

模块级依赖都是 ``from X import f``，所以 patch 目标**必须打在 ``codex_phone`` 自己的
命名空间上**（``codex_phone.load_cached_sentinel`` 等）；legacy 分支的两个依赖是
**函数内 import**，所以打在**源模块**上才生效（见各测试里的注释）。
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from sms_tool import codex_phone

SEND_URL = "https://auth.openai.com/api/accounts/add-phone/send"
VALIDATE_URL = "https://auth.openai.com/api/accounts/phone-otp/validate"

POOL_SUCCESS_KEYS = {
    "ok", "next_url", "phone", "provider", "activation_id",
    "reuse_count", "max_reuse_count", "remaining",
}
POOL_FAILURE_KEYS = {"ok", "error", "phone", "body", "message"}


class _FakeResponse:
    def __init__(self, status_code=200, text="", payload=None, headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeSession:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if url not in self.responses:
            raise AssertionError(f"unexpected POST to {url}")
        return self.responses[url]


class CodexPhoneTestBase(unittest.TestCase):
    def setUp(self):
        self.sentinel = {"sentinel_token": "ST", "sentinel_so_token": "SO"}
        self.pool_result = {"ok": True}
        self.pool_calls = []
        self.pick_result = ("+15550000000", "https://sms.example/api")
        self.poll_code = "123456"
        self.baseline = {"raw": "seed", "timestamp": 0}

        self._start(mock.patch.object(codex_phone, "load_cached_sentinel",
                                      return_value=self.sentinel))
        # 头部的真实构造另有测试覆盖（test_auth_headers_and_classification.py），
        # 这里只关心 codex_phone 有没有把 did / extra 正确递下去。
        self._start(mock.patch.object(
            codex_phone, "openai_auth_headers_lower",
            side_effect=lambda did="", extra=None: {
                **{str(k).lower(): v for k, v in (extra or {}).items()},
                "oai-did": did,
            }))
        self._start(mock.patch.object(codex_phone, "CFG", {}))

        # 函数内 import → 打在源模块上，调用时才重新读属性。
        self._start(mock.patch(
            "sms_tool.phone_reuse.complete_phone_verification_with_reuse",
            side_effect=self._pool))
        self._start(mock.patch("sms_tool.paypal.config_picker._pick_phone_and_sms",
                               side_effect=lambda cfg: self.pick_result))
        self._start(mock.patch("sms_tool.sms_utils._sms_baseline",
                               side_effect=lambda url: self.baseline))
        self._start(mock.patch("sms_tool.sms_utils._poll_sms_code",
                               side_effect=lambda *a, **kw: self.poll_code))

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _pool(self, **kwargs):
        self.pool_calls.append(kwargs)
        return self.pool_result

    def _session(self, overrides=None):
        """注意：URL 里带点和斜杠，不能用 ``**kwargs`` 传（键会变成字面量）。"""
        responses = {
            SEND_URL: _FakeResponse(200, text="ok"),
            VALIDATE_URL: _FakeResponse(200, text="ok", payload={"continue_url": "https://next"}),
        }
        responses.update(overrides or {})
        return _FakeSession(responses)


class DispatchTest(CodexPhoneTestBase):
    """``complete_phone_verification`` 的三条分支。"""

    def test_a_phone_pool_wins_over_the_disabled_flag(self):
        """🔴 优先级契约：传了池子就走池子，**哪怕 ``enabled=False``**。
        池子是调用方的显式意图，``enabled`` 只是全局开关 —— 前者的优先级更高。
        反过来写的话，"关掉自动手机处理"会把已经在池子里的租号悄悄丢掉。"""
        pool = object()
        codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur", enabled=False, phone_pool=pool)
        self.assertEqual(len(self.pool_calls), 1)
        self.assertIs(self.pool_calls[0]["phone_pool"], pool)

    def test_without_a_pool_and_disabled_the_caller_is_told_to_add_a_phone(self):
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur", enabled=False)
        self.assertEqual(result, {
            "ok": False,
            "error": "add_phone_required",
            "message": ("OpenAI requested phone verification; "
                        "automatic phone handling is disabled."),
        })
        self.assertEqual(self.pool_calls, [])

    def test_without_a_pool_and_enabled_the_legacy_path_runs(self):
        result = codex_phone.complete_phone_verification(
            self._session(), "did", "https://cur", enabled=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["phone"], "+15550000000")

    def test_the_disabled_path_is_taken_by_default(self):
        """``enabled`` 的默认值是 ``False`` —— 没传就是不开。"""
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur")
        self.assertEqual(result["error"], "add_phone_required")

    def test_an_empty_pool_is_still_no_pool(self):
        """⚠️ 判据是 ``if phone_pool:`` 的真值，不是 ``is not None``。
        空字典/空列表/0 都会被当成"没给池子"，掉进 legacy 分支。"""
        for label, pool in {"empty dict": {}, "empty list": [], "zero": 0,
                            "empty str": ""}.items():
            with self.subTest(label=label):
                self.pool_calls.clear()
                codex_phone.complete_phone_verification(
                    _FakeSession(), "did", "https://cur",
                    enabled=False, phone_pool=pool)
                self.assertEqual(self.pool_calls, [],
                                 f"{label!r} 应当被当成「没给池子」")

    def test_the_disabled_result_has_exactly_three_keys(self):
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur", enabled=False)
        self.assertEqual(set(result), {"ok", "error", "message"})

    def test_the_proxy_is_forwarded_to_the_pool_path(self):
        codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur",
            proxy="http://p:8080", phone_pool=object())
        self.assertEqual(self.pool_calls[0]["proxy"], "http://p:8080")


class ReusePoolTest(CodexPhoneTestBase):
    """``_verify_with_reuse_pool`` 的结果整形。"""

    def _run(self):
        return codex_phone._verify_with_reuse_pool(
            _FakeSession(), "did", "https://cur", object())

    def test_a_successful_pool_result_is_reshaped_to_nine_fields(self):
        self.pool_result = {"ok": True, "next_url": "https://n", "phone": "+1555",
                            "provider": "smsbower", "activation_id": "A1",
                            "reuse_count": 2, "max_reuse_count": 5, "remaining": 3}
        result = self._run()
        self.assertEqual(result, {
            "ok": True, "next_url": "https://n", "phone": "+1555",
            "provider": "smsbower", "activation_id": "A1",
            "reuse_count": 2, "max_reuse_count": 5, "remaining": 3,
        })

    def test_a_bare_success_still_yields_every_key(self):
        """池子只回 ``{"ok": True}`` 时，其余字段必须补默认值，
        不能让调用方拿到 KeyError。"""
        self.pool_result = {"ok": True}
        result = self._run()
        self.assertEqual(set(result), POOL_SUCCESS_KEYS)
        self.assertEqual(result["next_url"], "")
        self.assertEqual(result["reuse_count"], 0)

    def test_the_numeric_fields_default_to_zero_not_empty_string(self):
        """``reuse_count`` / ``max_reuse_count`` / ``remaining`` 是 **0（整数）**，
        不是空串 —— 调用方会拿它们做算术和比较。"""
        self.pool_result = {"ok": True}
        result = self._run()
        for key in ("reuse_count", "max_reuse_count", "remaining"):
            with self.subTest(key=key):
                self.assertEqual(result[key], 0)
                self.assertIsInstance(result[key], int)

    def test_a_failed_pool_result_is_reshaped_to_five_fields(self):
        self.pool_result = {"ok": False, "error": "no_number", "phone": "+1555",
                            "body": "sold out", "message": "try later"}
        result = self._run()
        self.assertEqual(set(result), POOL_FAILURE_KEYS)
        self.assertEqual(result["error"], "no_number")
        self.assertNotIn("next_url", result)

    def test_a_failure_without_an_error_code_gets_a_generic_one(self):
        self.pool_result = {"ok": False}
        self.assertEqual(self._run()["error"], "phone_verification_failed")

    def test_a_failed_pool_result_forwards_the_body_verbatim(self):
        """🔴 失败整形里**只有 ``body`` 键被断言过存在，没被断言过内容** ——
        变异探针证明：把它改成读 ``response`` 键，现有用例全绿。
        而这个 body 是排障时唯一能看出"号码为什么没租到"的东西。"""
        self.pool_result = {"ok": False, "error": "no_number", "body": "sold out"}
        self.assertEqual(self._run()["body"], "sold out")

    def test_the_failure_body_is_read_from_the_body_key_only(self):
        """池子如果把报错原文放在 ``response`` 里，这里一律拿不到 —— 钉死这个行为，
        免得有人"顺手兼容一下"就把键名改了。"""
        self.pool_result = {"ok": False, "response": "WRONG-KEY"}
        self.assertEqual(self._run()["body"], "")

    def test_an_empty_failure_body_stays_an_empty_string(self):
        self.pool_result = {"ok": False, "body": ""}
        result = self._run()
        self.assertEqual(result["body"], "")
        self.assertIsInstance(result["body"], str)

    def test_the_branch_hinges_on_the_truthiness_of_ok(self):
        """``if result.get("ok")`` —— ``0`` / ``""`` / ``{}`` 都是假，走失败分支。"""
        for label, ok in {"zero": 0, "empty str": "", "empty dict": {},
                          "none": None, "false": False}.items():
            with self.subTest(label=label):
                self.pool_result = {"ok": ok}
                self.assertEqual(set(self._run()), POOL_FAILURE_KEYS)

    def test_the_cached_sentinel_reaches_the_pool(self):
        self._run()
        self.assertEqual(self.pool_calls[0]["sentinel"], self.sentinel)

    def test_the_session_and_url_reach_the_pool(self):
        session = _FakeSession()
        codex_phone._verify_with_reuse_pool(session, "did-1", "https://cur", object())
        self.assertIs(self.pool_calls[0]["session"], session)
        self.assertEqual(self.pool_calls[0]["did"], "did-1")
        self.assertEqual(self.pool_calls[0]["current_url"], "https://cur")


class LegacyConfigTest(CodexPhoneTestBase):
    """legacy 单号路径：配置与依赖缺失时的降级。"""

    def _run(self, session=None):
        return codex_phone._verify_with_legacy(session or self._session(),
                                               "did", "https://cur")

    def test_a_missing_paypal_auto_section_reaches_the_picker_as_empty(self):
        """⚠️ ``phone_sms_config_missing`` 的真正触发点是**选择器返回空值**，
        不是"CFG 里没有这一节" —— 选择器是外部依赖，CFG 只决定它收到什么。
        所以这里断言的是"选择器收到了空配置"这个可观测事实。"""
        with mock.patch("sms_tool.paypal.config_picker._pick_phone_and_sms",
                        return_value=("", "")) as pick:
            result = self._run()
        self.assertEqual(result["error"], "phone_sms_config_missing")
        self.assertEqual(pick.call_args.args[0], {})

    def test_a_non_dict_paypal_auto_section_is_normalised_to_empty(self):
        """``CFG.get("paypal_auto") if isinstance(...) else {}`` ——
        非 dict 的整节被当成空配置，不是崩溃。"""
        for value in ("paypal", 42, [1]):
            with self.subTest(value=repr(value)):
                codex_phone.CFG = {"paypal_auto": value}
                with mock.patch("sms_tool.paypal.config_picker._pick_phone_and_sms",
                                return_value=("", "")) as pick:
                    result = self._run()
                self.assertEqual(result["error"], "phone_sms_config_missing")
                self.assertEqual(pick.call_args.args[0], {})

    def test_a_dict_section_is_passed_through_untouched(self):
        section = {"phone_number": "+1", "sms_api_url": "https://u"}
        codex_phone.CFG = {"paypal_auto": section}
        with mock.patch("sms_tool.paypal.config_picker._pick_phone_and_sms",
                        return_value=("", "")) as pick:
            self._run()
        self.assertEqual(pick.call_args.args[0], section)

    def test_a_missing_phone_or_sms_url_is_reported(self):
        for label, pick in {
            "no phone": ("", "https://sms.example/api"),
            "no url": ("+1555", ""),
            "neither": ("", ""),
            "none pair": (None, None),
        }.items():
            with self.subTest(label=label):
                self.pick_result = pick
                self.assertEqual(self._run()["error"], "phone_sms_config_missing")

    def test_the_picked_values_are_stripped(self):
        self.pick_result = ("  +15550000000  ", "  https://sms.example/api  ")
        session = self._session()
        result = codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertTrue(result["ok"])
        self.assertEqual(result["phone"], "+15550000000")
        self.assertEqual(session.calls[0]["json"], {"phone_number": "+15550000000"})

    def test_an_import_failure_is_degraded_not_raised(self):
        """``try: from ... import ...`` / ``except Exception`` ——
        依赖装不上时返回 ``phone_helpers_unavailable:<异常>``，不炸调用方。"""
        stub = types.ModuleType("sms_tool.paypal.config_picker")
        with mock.patch.dict(sys.modules, {"sms_tool.paypal.config_picker": stub}):
            result = self._run()
        self.assertTrue(result["error"].startswith("phone_helpers_unavailable:"),
                        result["error"])
        self.assertFalse(result["ok"])

    def test_a_dependency_that_fails_with_a_non_import_error_is_also_degraded(self):
        """🔴 上一条用的是"模块里没有这个属性"，触发的是 **ImportError**；
        把 ``except Exception`` 收窄成 ``except ImportError`` 它照样绿。
        而这个 ``except`` 的真实价值恰恰在于**接住非 ImportError**：
        依赖模块内部炸了（语法错、循环导入里的 NameError、读配置失败）时抛出来的
        不是 ImportError，收窄之后这些会直接穿透到调用方、整个注册流程崩掉。"""
        stub = types.ModuleType("sms_tool.paypal.config_picker")

        def _boom(name):
            raise ValueError(f"boom:{name}")

        stub.__getattr__ = _boom
        with mock.patch.dict(sys.modules, {"sms_tool.paypal.config_picker": stub}):
            result = self._run()
        self.assertFalse(result["ok"])
        self.assertIn("phone_helpers_unavailable:", result["error"])
        self.assertIn("boom", result["error"], "原始异常文本必须带出来，否则没法排障")

    def test_the_picked_sms_url_is_stripped_before_it_reaches_the_helpers(self):
        """🔴 和 ``phone`` 一样要 strip，但现有用例只断言了 **phone** 被 strip ——
        URL 上的空白会原样传给 ``_sms_baseline`` 和 ``_poll_sms_code``，
        取到的基线就对不上，轮询永远等不到新短信。"""
        self.pick_result = ("+1555", "  https://sms.example/api?a=1\n")
        with mock.patch("sms_tool.sms_utils._sms_baseline",
                        side_effect=lambda url: {"raw": url}) as baseline, \
             mock.patch("sms_tool.sms_utils._poll_sms_code",
                        side_effect=lambda *a, **kw: "123456") as poll:
            self._run()
        self.assertEqual(baseline.call_args.args[0], "https://sms.example/api?a=1")
        self.assertEqual(poll.call_args.args[0], "https://sms.example/api?a=1")

    def test_a_none_phone_is_normalised_to_empty_not_the_string_none(self):
        """🔴 ``str(phone or "").strip()`` 里的 ``or ""`` 是**有语义的**。

        去掉之后 ``None`` 会变成字符串 ``"None"``（真值！），于是
        "没选到号码"被当成"选到了号码 None"，直接往下发验证码 POST。
        现有用例之所以抓不到：``(None, None)`` 那一组里 URL 也是 ``None``，
        第二个条件照样拦住了 —— **必须让 URL 有效、只有 phone 缺失**才测得出来。
        """
        self.pick_result = (None, "https://sms.example/api")
        session = self._session()
        result = codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(result["error"], "phone_sms_config_missing")
        self.assertEqual(session.calls, [], "没选到号码就不该发出任何请求")


class LegacyFlowTest(CodexPhoneTestBase):
    """legacy 单号路径：两次 POST 的契约。"""

    def test_both_posts_hit_the_documented_endpoints(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual([c["url"] for c in session.calls],
                         [SEND_URL, VALIDATE_URL])

    def test_the_send_post_carries_the_phone_number(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(session.calls[0]["json"], {"phone_number": "+15550000000"})

    def test_the_validate_post_carries_the_code(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(session.calls[1]["json"], {"code": "123456"})

    def test_the_two_posts_use_different_referers(self):
        """send 用的是**当前** URL，validate 用的是固定的验证页 URL。"""
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur/page")
        self.assertEqual(session.calls[0]["headers"]["referer"], "https://cur/page")
        self.assertEqual(session.calls[1]["headers"]["referer"],
                         "https://auth.openai.com/phone-verification")

    def test_the_sentinel_token_is_attached_to_both_posts(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        for call in session.calls:
            self.assertEqual(call["headers"]["openai-sentinel-token"], "ST")

    def test_the_proxy_is_accepted_but_never_used(self):
        """🔴 钉住现状，这是个**真问题**，不是设计意图。

        ``_verify_with_legacy(session, did, current_url, proxy=proxy)`` 收下
        ``proxy`` 之后**再没用过一次** —— 两次 POST 都不带它，也不走
        ``session.proxies``。后果：legacy 单号路径的手机验证流量**全部不走代理**，
        而同一批账号的注册流程是走代理的 —— IP 直接关联。
        池子路径（``_verify_with_reuse_pool``）是**正常传**的，对比起来更明显。
        修的时候注意：这条用例必须一起改。
        """
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur",
                                        proxy="http://p:8080")
        for call in session.calls:
            self.assertNotIn("proxies", call)
            self.assertNotIn("proxy", call)
        self.assertIsNone(getattr(session, "proxies", None))

    def test_the_proxy_does_reach_the_pool_path(self):
        """对照上一条：池子路径的 proxy 是正常往下传的。"""
        codex_phone._verify_with_reuse_pool(_FakeSession(), "did", "https://cur",
                                            object(), proxy="http://p:8080")
        self.assertEqual(self.pool_calls[0]["proxy"], "http://p:8080")

    def test_both_posts_declare_a_json_content_type(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        for call in session.calls:
            self.assertEqual(call["headers"]["content-type"], "application/json")

    def test_both_posts_use_a_thirty_second_timeout(self):
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual([c["timeout"] for c in session.calls], [30, 30])

    def test_the_impersonation_profile_is_pinned_at_chrome110(self):
        """⚠️ 钉住现状，**不是**在推荐这个值。

        ``paypal_fingerprints.PAYPAL_CHROME_VERSION`` 已经是 136，这里仍然写死
        ``chrome110`` —— UA 头说 Chrome 136、TLS/JA3 指纹说 Chrome 110，
        **两者不一致**。要改得三个模块一起改，别只动这一处。"""
        session = self._session()
        codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual([c["impersonate"] for c in session.calls],
                         ["chrome110", "chrome110"])

    def test_a_non_200_send_stops_the_flow_and_truncates_the_body(self):
        session = self._session({SEND_URL: _FakeResponse(429, text="x" * 900)})
        result = codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(result["error"], "phone_send_failed:429")
        self.assertEqual(len(result["body"]), 300)
        self.assertEqual(len(session.calls), 1, "send 失败后不该再发 validate")

    def test_a_non_200_validate_stops_after_the_second_post(self):
        session = self._session({VALIDATE_URL: _FakeResponse(400, text="bad code")})
        result = codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(result["error"], "phone_validate_failed:400")
        self.assertEqual(result["body"], "bad code")

    def test_a_non_200_validate_truncates_the_body(self):
        """🔴 send 分支的 300 字截断有用例钉着，validate 分支没有 ——
        变异探针显示去掉这里的 ``[:300]`` 现有用例全绿。
        验证失败的响应体可能是整页 HTML，不截断就会灌进日志和 UI。"""
        session = self._session({VALIDATE_URL: _FakeResponse(500, text="z" * 900)})
        result = codex_phone._verify_with_legacy(session, "did", "https://cur")
        self.assertEqual(result["error"], "phone_validate_failed:500")
        self.assertEqual(len(result["body"]), 300)
        self.assertEqual(result["body"], "z" * 300)

    def test_a_short_validate_body_is_kept_whole(self):
        """截断只在超长时生效，短响应体必须原样保留。"""
        session = self._session({VALIDATE_URL: _FakeResponse(400, text="bad code")})
        self.assertEqual(codex_phone._verify_with_legacy(
            session, "did", "https://cur")["body"], "bad code")

    def test_an_unreceived_sms_code_is_a_timeout(self):
        self.poll_code = None
        result = self._run_flow()
        self.assertEqual(result["error"], "phone_sms_timeout")
        self.assertEqual(len(self._session_calls), 1, "收不到码就不该发 validate")

    def test_the_sms_polling_windows_come_from_the_config(self):
        codex_phone.CFG = {"paypal_auto": {"sms_timeout": 15, "sms_poll_interval": 2}}
        with mock.patch("sms_tool.sms_utils._poll_sms_code",
                        side_effect=lambda *a, **kw: "") as poll:
            self._run_flow()
        self.assertEqual(poll.call_args.kwargs["timeout"], 15)
        self.assertEqual(poll.call_args.kwargs["poll_interval"], 2)

    def test_the_sms_polling_windows_have_documented_defaults(self):
        with mock.patch("sms_tool.sms_utils._poll_sms_code",
                        side_effect=lambda *a, **kw: "") as poll:
            self._run_flow()
        self.assertEqual(poll.call_args.kwargs["timeout"], 120)
        self.assertEqual(poll.call_args.kwargs["poll_interval"], 5)

    def test_the_sms_baseline_is_taken_from_the_picked_url(self):
        with mock.patch("sms_tool.sms_utils._sms_baseline",
                        side_effect=lambda url: {"raw": "seed"}) as baseline:
            self._run_flow()
        self.assertEqual(baseline.call_args.args[0], "https://sms.example/api")

    def test_success_returns_only_three_fields(self):
        """⚠️ 和池子路径的形状**不一样**（那边是 9 个字段）。
        调用方如果按池子的键名取值，legacy 成功时会拿到 KeyError。"""
        result = self._run_flow()
        self.assertEqual(set(result), {"ok", "next_url", "phone"})
        self.assertEqual(result["next_url"], "https://next")

    # -- helpers -------------------------------------------------------------
    def _run_flow(self):
        self._session_calls = []
        session = self._session()

        def _post(url, **kwargs):
            self._session_calls.append(url)
            return session.responses[url]

        session.post = _post
        return codex_phone._verify_with_legacy(session, "did", "https://cur")



class NextUrlTest(unittest.TestCase):
    """``_next_url`` 的三级兜底链。"""

    def _resp(self, payload=None, headers=None, url="https://fallback"):
        return _FakeResponse(payload=payload, headers=headers, url=url)

    def test_the_continue_url_from_the_json_body_wins(self):
        resp = self._resp(payload={"continue_url": "https://a"},
                          headers={"Location": "https://b"})
        self.assertEqual(codex_phone._next_url(resp), "https://a")

    def test_it_falls_back_to_the_location_header(self):
        resp = self._resp(payload={}, headers={"Location": "https://b"})
        self.assertEqual(codex_phone._next_url(resp), "https://b")

    def test_it_falls_back_to_the_response_url(self):
        resp = self._resp(payload={}, headers={}, url="https://c")
        self.assertEqual(codex_phone._next_url(resp), "https://c")

    def test_a_body_that_is_not_json_is_treated_as_empty(self):
        resp = self._resp(payload=None, headers={"Location": "https://b"})
        self.assertEqual(codex_phone._next_url(resp), "https://b")

    def test_an_empty_continue_url_does_not_win(self):
        """``or`` 链：空串是假值，会继续往下一级走。"""
        resp = self._resp(payload={"continue_url": ""},
                          headers={"Location": "https://b"})
        self.assertEqual(codex_phone._next_url(resp), "https://b")

    def test_a_missing_location_header_key_falls_through(self):
        resp = self._resp(payload={}, headers={}, url="https://c")
        self.assertEqual(codex_phone._next_url(resp), "https://c")


class OaiHeadersTest(CodexPhoneTestBase):
    def test_it_delegates_to_the_lower_cased_header_builder(self):
        with mock.patch.object(codex_phone, "openai_auth_headers_lower",
                               return_value={"A": "1"}) as builder:
            result = codex_phone._oai_headers("did-9", {"Referer": "https://r"})
        self.assertEqual(result, {"A": "1"})
        self.assertEqual(builder.call_args.args[0], "did-9")
        self.assertEqual(builder.call_args.kwargs["extra"], {"Referer": "https://r"})

    def test_extra_defaults_to_none(self):
        with mock.patch.object(codex_phone, "openai_auth_headers_lower",
                               return_value={}) as builder:
            codex_phone._oai_headers("did-9")
        self.assertIsNone(builder.call_args.kwargs["extra"])


if __name__ == "__main__":
    unittest.main()
