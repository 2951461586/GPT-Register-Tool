"""Behaviour tests for sms_tool/paypal_reverse.py -- orchestration layer (P3).

The money-relevant part of this module is not "does it parse HTML" (covered in
``test_paypal_reverse_pure.py``) but **branch selection**:

* which redirect shape is recognised, and which one escalates to browser;
* whether a missing token is retried or treated as terminal;
* whether the auth session is polled, and when a request is *not* sent at all.

Every request is served by an in-memory fake; nothing here reaches the network,
a browser, or real money.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sms_tool.paypal_reverse import (
    _NeedBrowserFallback,
    PayPalReverseClient,
    try_reverse_pay,
)

from paypal_reverse_fakes import FakeResponse, FakeSession, make_client
from sms_tool.paypal_reverse import ReversePayResult

_PP = "https://www.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token=EC-123"
_STRIPE = "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_x"


def _result(**kw) -> ReversePayResult:
    return ReversePayResult(**kw)


def _client(responses=None, **overrides):
    c = make_client(**overrides)
    c._session = FakeSession(responses)
    c._current_url = c.redirect_url
    return c


# ────────────────────────────── redirect URL parsing ──────────────────────────────


class ParseRedirectUrlTests(unittest.TestCase):
    def test_direct_paypal_url_needs_no_request(self):
        """URL 里已有 token → 纯解析，一次请求都不许发。"""
        c = _client(redirect_url=_PP)
        c._parse_redirect_url()
        self.assertEqual([], c._session.calls)
        self.assertEqual("EC-123", c._pp_token)
        self.assertEqual("", c._ba_token)
        self.assertEqual("_express-checkout", c._cmd)

    def test_ba_token_alone_is_enough(self):
        c = _client(redirect_url="https://www.paypal.com/agreements/approve?ba_token=BA-9")
        c._parse_redirect_url()
        self.assertEqual([], c._session.calls)
        self.assertEqual("BA-9", c._ba_token)
        self.assertEqual("", c._pp_token)

    def test_stripe_redirect_is_followed_once_without_auto_redirect(self):
        """跟随 Stripe 时必须自己接管跳转（allow_redirects=False）。"""
        c = _client(
            [FakeResponse(302, "", _STRIPE, headers={"Location": _PP})],
            redirect_url=_STRIPE,
        )
        c._parse_redirect_url()
        self.assertEqual(1, len(c._session.calls))
        call = c._session.calls[0]
        self.assertEqual("GET", call["method"])
        self.assertIs(False, call["allow_redirects"])
        self.assertEqual("EC-123", c._pp_token)
        self.assertEqual(_PP, c.redirect_url)

    def test_stripe_redirect_landing_on_paypal_without_token_is_accepted(self):
        landing = "https://www.paypal.com/checkoutnow"
        c = _client(
            [FakeResponse(302, "", _STRIPE, headers={"Location": landing})],
            redirect_url=_STRIPE,
        )
        c._parse_redirect_url()
        self.assertEqual(landing, c.redirect_url)
        self.assertEqual("", c._pp_token)

    def test_stripe_redirect_without_location_header_falls_through_to_chain(self):
        """没有 Location 头 → 不掉头报错，而是继续走多跳跟随。"""
        c = _client(
            [FakeResponse(200, "", _STRIPE), FakeResponse(200, "", _PP)],
            redirect_url=_STRIPE,
        )
        c._parse_redirect_url()
        self.assertEqual(2, len(c._session.calls))
        self.assertEqual("EC-123", c._pp_token)

    def test_lower_case_location_header_is_honoured(self):
        c = _client(
            [FakeResponse(302, "", _STRIPE, headers={"location": _PP})],
            redirect_url=_STRIPE,
        )
        c._parse_redirect_url()
        self.assertEqual("EC-123", c._pp_token)

    def test_paypal_url_without_any_token_raises_instead_of_following(self):
        """⚠️ 钉住现状：host 是 paypal.com 但没 token 时**直接**抛 fallback。

        不会去跟随跳转 —— 也就是说一个「已登录态跳到审批页」的 URL 会被判死。
        要放宽这条就必须先改本用例。
        """
        c = _client(redirect_url="https://www.paypal.com/myaccount/")
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._parse_redirect_url()
        self.assertEqual("parse_url", ctx.exception.step)
        self.assertEqual([], c._session.calls)

    def test_unknown_host_chain_that_never_reaches_paypal_raises(self):
        c = _client([FakeResponse(200, "", "https://example.com/stuck")])
        c.redirect_url = "https://example.com/start"
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._parse_redirect_url()
        self.assertEqual("parse_url", ctx.exception.step)
        self.assertIn("could not resolve PayPal URL", str(ctx.exception))

    def test_multi_hop_chain_that_lands_on_paypal_without_token_is_accepted(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/checkoutnow")])
        c.redirect_url = "https://example.com/start"
        c._parse_redirect_url()
        self.assertEqual("https://www.paypal.com/checkoutnow", c.redirect_url)

    def test_chain_rewrite_keeps_the_final_url_for_later_steps(self):
        final = "https://www.paypal.com/x?token=EC-9&cmd=_express-checkout"
        c = _client([FakeResponse(200, "", final)])
        c.redirect_url = "https://short.link/abc"
        c._parse_redirect_url()
        self.assertEqual(final, c.redirect_url)
        self.assertEqual("EC-9", c._pp_token)
        self.assertEqual("_express-checkout", c._cmd)


# ────────────────────────────── redirect following ──────────────────────────────


class FollowRedirectsTests(unittest.TestCase):
    def _chain(self, hops):
        """hops: [(status, location_or_None, url)] — 最后一跳是终点。"""
        responses = []
        for status, location, url in hops:
            headers = {"Location": location} if location else {}
            responses.append(FakeResponse(status, "", url, headers=headers))
        return responses

    def test_stops_at_first_non_redirect_status(self):
        c = _client(self._chain([(200, None, "https://a.example/end")]))
        r = c._follow_redirects("https://a.example/start")
        self.assertEqual("https://a.example/end", str(r.url))
        self.assertEqual(1, len(c._session.calls))

    def test_follows_relative_location_using_urljoin(self):
        c = _client(self._chain([
            (302, "/next", "https://a.example/start"),
            (200, None, "https://a.example/next"),
        ]))
        c._follow_redirects("https://a.example/dir/start")
        self.assertEqual(["https://a.example/dir/start", "https://a.example/next"], c._session.urls)

    def test_every_3xx_status_is_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                c = _client(self._chain([
                    (status, "https://a.example/next", "https://a.example/start"),
                    (200, None, "https://a.example/next"),
                ]))
                c._follow_redirects("https://a.example/start")
                self.assertEqual(2, len(c._session.calls))

    def test_non_redirect_status_with_location_stops(self):
        """200 即使带 Location 也不跟 —— 只在 3xx 时跟。"""
        c = _client(self._chain([(200, "https://a.example/nope", "https://a.example/start")]))
        r = c._follow_redirects("https://a.example/start")
        self.assertEqual("https://a.example/start", str(r.url))
        self.assertEqual(1, len(c._session.calls))

    def test_max_hops_caps_the_walk(self):
        """重定向环不能把进程拖死 —— max_hops 必须真的生效。"""
        hops = [(302, f"https://a.example/{i}", f"https://a.example/{i}") for i in range(20)]
        c = _client(self._chain(hops))
        c._follow_redirects("https://a.example/start", max_hops=3)
        self.assertEqual(3, len(c._session.calls))

    def test_redirects_are_never_delegated_to_the_transport(self):
        c = _client(self._chain([(200, None, "https://a.example/end")]))
        c._follow_redirects("https://a.example/start")
        self.assertIs(False, c._session.calls[0]["allow_redirects"])


# ────────────────────────────── auth token extraction ──────────────────────────────


class ExtractAuthTokensTests(unittest.TestCase):
    def _client_with_final(self, final_url, cookies=None, json_body=None):
        c = _client([FakeResponse(200, "", final_url, cookies=cookies or {},
                                  json_data=json_body if json_body is not None else {})])
        c._current_url = "https://www.paypal.com/pay"
        return c

    def test_access_token_from_cookie_must_be_a_jwt(self):
        """cookie 名含 access 但值不像 JWT → 不算拿到 token。"""
        c = self._client_with_final(
            "https://chatgpt.com/",
            cookies={"__Secure-next-auth.session-token": "notajwt"},
        )
        result = c._extract_auth_tokens()
        self.assertFalse(result.ok)
        self.assertEqual("auth_token", result.failed_step)

    def test_jwt_session_token_cookie_is_accepted(self):
        c = self._client_with_final(
            "https://chatgpt.com/",
            cookies={"__Secure-next-auth.session-token": "eyJhbGciOiJIUzI1NiJ9.x.y"},
        )
        result = c._extract_auth_tokens()
        self.assertTrue(result.ok, result.error)
        self.assertEqual("eyJhbGciOiJIUzI1NiJ9.x.y", result.access_token)
        self.assertEqual("no_rt", result.refresh_token_status)
        self.assertEqual("https://chatgpt.com/", result.redirect_url)

    def test_access_cookie_is_paired_with_refresh_cookie(self):
        c = self._client_with_final(
            "https://chatgpt.com/",
            cookies={"access_token": "eyJAAA", "__Secure-refresh-token": "RT-1"},
        )
        result = c._extract_auth_tokens()
        self.assertTrue(result.ok)
        self.assertEqual("eyJAAA", result.access_token)
        self.assertEqual("RT-1", result.oauth_refresh_token)
        self.assertEqual("oauth_present", result.refresh_token_status)

    def test_refresh_cookie_does_not_need_to_be_a_jwt(self):
        """refresh 只按名字匹配，不校验 eyJ 前缀。"""
        c = self._client_with_final(
            "https://chatgpt.com/",
            cookies={"access": "eyJAAA", "refresh": "opaque-rt"},
        )
        self.assertEqual("opaque-rt", c._extract_auth_tokens().oauth_refresh_token)

    def test_token_in_url_fragment_is_used_when_no_cookie(self):
        c = self._client_with_final("https://chatgpt.com/#access_token=FRAG&x=1")
        result = c._extract_auth_tokens()
        self.assertTrue(result.ok)
        self.assertEqual("FRAG", result.access_token)

    def test_camel_case_fragment_key_is_recognised(self):
        c = self._client_with_final("https://chatgpt.com/#accessToken=FRAG2")
        self.assertEqual("FRAG2", c._extract_auth_tokens().access_token)

    def test_cookie_strategy_wins_over_fragment(self):
        c = self._client_with_final(
            "https://chatgpt.com/#access_token=FRAG",
            cookies={"access": "eyJCOOKIE"},
        )
        self.assertEqual("eyJCOOKIE", c._extract_auth_tokens().access_token)

    def test_fragment_is_only_parsed_from_the_fragment_not_the_query(self):
        """⚠️ 钉住现状：只解析 fragment，query 里的 access_token 一律看不见。"""
        c = self._client_with_final("https://chatgpt.com/?access_token=QUERY")
        self.assertFalse(c._extract_auth_tokens().ok)

    def test_auth_session_poll_is_the_last_resort(self):
        c = self._client_with_final(
            "https://chatgpt.com/",
            json_body={"accessToken": "AT-POLL", "refreshToken": "RT-POLL"},
        )
        with patch("sms_tool.paypal_reverse.CFG", {"chatgpt": {"chat_base_url": "https://chatgpt.com"}}):
            result = c._extract_auth_tokens()
        self.assertTrue(result.ok)
        self.assertEqual("AT-POLL", result.access_token)
        self.assertEqual("RT-POLL", result.oauth_refresh_token)
        urls = [call["url"] for call in c._session.calls]
        self.assertIn("https://chatgpt.com/api/auth/session", urls)

    def test_auth_session_poll_uses_configured_base_url(self):
        c = self._client_with_final("https://chatgpt.com/", json_body={"accessToken": "AT"})
        with patch("sms_tool.paypal_reverse.CFG", {"chatgpt": {"chat_base_url": "https://proxy.example/"}}):
            c._extract_auth_tokens()
        self.assertIn("https://proxy.example/api/auth/session", c._session.urls)

    def test_auth_session_poll_defaults_to_chatgpt_com(self):
        c = self._client_with_final("https://chatgpt.com/", json_body={"accessToken": "AT"})
        with patch("sms_tool.paypal_reverse.CFG", {}):
            c._extract_auth_tokens()
        self.assertIn("https://chatgpt.com/api/auth/session", c._session.urls)

    def test_poll_response_without_a_token_is_ignored(self):
        c = self._client_with_final("https://chatgpt.com/", json_body={"user": {"name": "x"}})
        result = c._extract_auth_tokens()
        self.assertFalse(result.ok)
        self.assertEqual("auth_token", result.failed_step)

    def test_poll_failure_is_swallowed_and_reported_as_auth_failure(self):
        """poll 抛异常不能炸穿流程，必须收敛成「没拿到 token」。"""
        c = self._client_with_final(
            "https://chatgpt.com/",
            json_body=None,
        )
        c._session = FakeSession([
            FakeResponse(200, "", "https://chatgpt.com/"),
            FakeResponse(200, "", "https://chatgpt.com/api/auth/session",
                         raise_on_json=ValueError("bad json")),
        ])
        result = c._extract_auth_tokens()
        self.assertFalse(result.ok)
        self.assertEqual("auth_token", result.failed_step)

    def test_poll_without_token_discards_refresh_found_in_cookies(self):
        """⚠️ 真实 bug（钉住现状）：poll 成功后 refresh 被无条件重赋值。

        Strategy 1 已经从 cookie 拿到 RT，Strategy 3 又把它无条件覆盖成空串，
        于是明明有 refresh token 却报 `no_rt`，下游拿不到续期凭据。
        触发前提是 Strategy 1 没拿到 access（这里 access cookie 不是 JWT）。
        要修就先改这个用例。
        """
        c = self._client_with_final(
            "https://chatgpt.com/",
            cookies={"access": "not-a-jwt", "refresh": "RT-FROM-COOKIE"},
            json_body={"accessToken": "AT-POLL"},
        )
        result = c._extract_auth_tokens()
        self.assertTrue(result.ok)
        self.assertEqual("", result.oauth_refresh_token)
        self.assertEqual("no_rt", result.refresh_token_status)

    def test_failure_result_carries_no_partial_token(self):
        c = self._client_with_final("https://chatgpt.com/", cookies={"refresh": "RT-SECRET"})
        d = c._extract_auth_tokens().to_dict()
        self.assertFalse(d["ok"])
        self.assertNotIn("RT-SECRET", repr(d))


# ────────────────────────────── cookies & requests ──────────────────────────────


class LoadCookiesTests(unittest.TestCase):
    def test_cookie_header_is_split_into_the_session(self):
        c = _client(cookie_header="a=1; b=2")
        c._load_cookies()
        self.assertEqual({"a": "1", "b": "2"}, c._session.cookies.get_dict())

    def test_empty_header_is_a_noop(self):
        c = _client(cookie_header="")
        c._load_cookies()
        self.assertEqual({}, c._session.cookies.get_dict())

    def test_host_prefixed_cookies_are_skipped(self):
        """`__Host-` cookie 与 domain 绑定，塞进 `.paypal.com` 会失效。"""
        c = _client(cookie_header="__Host-session=abc; keep=1")
        c._load_cookies()
        self.assertEqual({"keep": "1"}, c._session.cookies.get_dict())

    def test_segments_without_equals_are_skipped(self):
        c = _client(cookie_header="garbage; keep=1")
        c._load_cookies()
        self.assertEqual({"keep": "1"}, c._session.cookies.get_dict())

    def test_empty_value_is_skipped(self):
        c = _client(cookie_header="empty=; keep=1")
        c._load_cookies()
        self.assertEqual({"keep": "1"}, c._session.cookies.get_dict())

    def test_whitespace_around_name_and_value_is_trimmed(self):
        c = _client(cookie_header="  a  =  1  ")
        c._load_cookies()
        self.assertEqual({"a": "1"}, c._session.cookies.get_dict())

    def test_value_containing_equals_is_preserved(self):
        c = _client(cookie_header="token=abc==/def")
        c._load_cookies()
        self.assertEqual({"token": "abc==/def"}, c._session.cookies.get_dict())


class SafeRequestTests(unittest.TestCase):
    def test_defaults_are_applied(self):
        c = _client([FakeResponse(200, "", "https://a.example/")])
        c._safe_request("GET", "https://a.example/")
        call = c._session.calls[0]
        self.assertEqual(60, call["timeout"])
        self.assertIs(True, call["allow_redirects"])

    def test_explicit_kwargs_win(self):
        c = _client([FakeResponse(200, "", "https://a.example/")])
        c._safe_request("GET", "https://a.example/", timeout=5, allow_redirects=False)
        call = c._session.calls[0]
        self.assertEqual(5, call["timeout"])
        self.assertIs(False, call["allow_redirects"])

    def test_transport_error_becomes_browser_fallback(self):
        """网络抖动不是终局失败 —— 转成 fallback 交给浏览器兜底。"""
        c = _client([ConnectionResetError("connection reset")])
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._safe_request("GET", "https://a.example/very/long/path/that/is/truncated")
        self.assertEqual("request", ctx.exception.step)
        self.assertIn("GET https://a.example/very/long/path/that/is/truncated", str(ctx.exception))

    def test_url_in_the_error_message_is_truncated_to_60_chars(self):
        """URL 只留前 60 字符（硬截断，不加省略号），避免把长 query 写进日志。"""
        c = _client([ConnectionResetError("boom")])
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._safe_request("GET", "https://a.example/" + "x" * 200)
        msg = str(ctx.exception)
        self.assertIn("https://a.example/" + "x" * 42, msg)
        self.assertNotIn("x" * 43, msg)


class SubmitFormTests(unittest.TestCase):
    def test_csrf_token_is_sent_as_a_header(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")])
        c._csrf_token = "CSRF-1"
        c._submit_form("https://www.paypal.com/post", {"a": "1"})
        call = c._session.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual("CSRF-1", call["headers"]["X-CSRF-Token"])
        self.assertEqual({"a": "1"}, call["data"])

    def test_no_csrf_header_when_token_unknown(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")])
        c._submit_form("https://www.paypal.com/post", {})
        self.assertNotIn("X-CSRF-Token", c._session.calls[0]["headers"])

    def test_referer_and_origin_come_from_current_url(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")], redirect_url=_PP)
        c._submit_form("https://www.paypal.com/post", {})
        headers = c._session.calls[0]["headers"]
        self.assertEqual(_PP, headers["Referer"])
        self.assertEqual("https://www.paypal.com", headers["Origin"])

    def test_none_values_are_dropped_but_empty_strings_kept(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")])
        c._submit_form("https://www.paypal.com/post", {"drop": None, "keep": "", "k": "v"})
        self.assertEqual({"keep": "", "k": "v"}, c._session.calls[0]["data"])

    def test_captcha_token_is_injected_then_cleared(self):
        """CAPTCHA token 用一次就丢 —— 复用旧 token 会被 PayPal 拒。"""
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")])
        c._captcha_token = "TOK"
        c._captcha_ekey = "EKEY"
        c._submit_form("https://www.paypal.com/post", {"a": "1"})
        data = c._session.calls[0]["data"]
        self.assertEqual("TOK", data["g-recaptcha-response"])
        self.assertEqual("TOK", data["h-captcha-response"])
        self.assertEqual("EKEY", data["recaptcha-ekey"])
        self.assertEqual("", c._captcha_token)
        self.assertEqual("", c._captcha_ekey)

    def test_second_submit_does_not_replay_the_captcha_token(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")] * 2)
        c._captcha_token = "TOK"
        c._submit_form("https://www.paypal.com/post", {})
        c._submit_form("https://www.paypal.com/post", {})
        self.assertNotIn("g-recaptcha-response", c._session.calls[1]["data"])

    def test_ekey_is_omitted_when_absent(self):
        c = _client([FakeResponse(200, "", "https://www.paypal.com/ok")])
        c._captcha_token = "TOK"
        c._submit_form("https://www.paypal.com/post", {})
        self.assertNotIn("recaptcha-ekey", c._session.calls[0]["data"])


# ────────────────────────────── payment submission ──────────────────────────────


class SubmitPaymentTests(unittest.TestCase):
    def test_agreement_checkboxes_are_forced_on(self):
        html = ('<form action="/confirm"><input type="hidden" name="csrf" value="T">'
                '<input type="checkbox" name="agree" value="0">'
                '<input type="checkbox" name="termsOfService" value="0"></form>')
        c = _client([FakeResponse(200, "clean", "https://www.paypal.com/done")])
        c._current_html = html
        c._submit_payment()
        data = c._session.calls[0]["data"]
        self.assertEqual("true", data["agree"])
        self.assertEqual("true", data["termsOfService"])
        self.assertEqual("T", data["csrf"])

    def test_agreement_keys_are_only_set_when_already_present(self):
        """没有 agree 字段时不能凭空造一个 —— 多余字段会让 PayPal 拒收。

        表单靠 action 里的 "terms" 字样被选中，但内部并没有同名字段。
        """
        html = '<form action="/terms-confirm"><input type="hidden" name="csrf" value="T"></form>'
        c = _client([FakeResponse(200, "clean", "https://www.paypal.com/done")])
        c._current_html = html
        c._submit_payment()
        self.assertEqual({"csrf": "T"}, c._session.calls[0]["data"])

    def test_submit_form_by_button_text_when_no_agreement_form(self):
        html = '<form action="/pay"><button type="submit">Pay Now</button></form>'
        c = _client([FakeResponse(200, "clean", "https://www.paypal.com/done")])
        c._current_html = html
        c._submit_payment()
        self.assertEqual("https://www.paypal.com/pay", c._session.calls[0]["url"])

    def test_direct_post_fallback_when_no_form_at_all(self):
        """没有可识别的表单 → 对当前 URL 直接 POST，而不是抛异常中断。"""
        c = _client([FakeResponse(200, "clean", "https://www.paypal.com/done")], redirect_url=_PP)
        c._current_html = "<div>nothing to submit</div>"
        c._submit_payment()
        self.assertEqual(1, len(c._session.calls))
        call = c._session.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual(_PP, call["url"])

    def test_state_is_updated_after_submit(self):
        html = '<form action="/confirm"><input name="agree" value="0"></form>'
        c = _client([FakeResponse(200, "<html>next</html>", "https://www.paypal.com/done")])
        c._current_html = html
        c._submit_payment()
        self.assertEqual("<html>next</html>", c._current_html)
        self.assertEqual("https://www.paypal.com/done", c._current_url)

    def test_block_page_after_submit_escalates_to_browser(self):
        html = '<form action="/confirm"><input name="agree" value="0"></form>'
        c = _client([FakeResponse(200, "<p>Access Denied</p>", "https://www.paypal.com/denied")])
        c._current_html = html
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._submit_payment()
        self.assertEqual("submit", ctx.exception.step)


# ────────────────────────────── session setup & entry point ──────────────────────────────


class NewSessionTests(unittest.TestCase):
    def test_falls_back_to_requests_when_curl_cffi_is_missing(self):
        """curl_cffi 缺失时退化成 requests.Session，而不是直接崩。"""
        c = _client()
        with patch("sms_tool.paypal_reverse.CurlSession", None):
            session = c._new_session()
        try:
            self.assertTrue(hasattr(session, "request"))
            self.assertEqual("curl_cffi" not in type(session).__module__, True)
        finally:
            session.close()

    def test_proxy_is_applied_to_both_schemes(self):
        c = _client(proxy="http://user:pw@127.0.0.1:8080")
        with patch("sms_tool.paypal_reverse.CurlSession", None):
            session = c._new_session()
        try:
            self.assertEqual({"http": "http://user:pw@127.0.0.1:8080",
                               "https": "http://user:pw@127.0.0.1:8080"}, session.proxies)
        finally:
            session.close()

    def test_no_proxy_leaves_proxies_untouched(self):
        c = _client(proxy=None)
        with patch("sms_tool.paypal_reverse.CurlSession", None):
            session = c._new_session()
        try:
            self.assertEqual({}, session.proxies)
        finally:
            session.close()

    def test_chrome_impersonation_is_requested_when_curl_cffi_present(self):
        seen = {}

        class _FakeCurl:
            def __init__(self, impersonate=None):
                seen["impersonate"] = impersonate
                self.proxies = {}
                self.headers = {}

        c = _client(proxy="http://127.0.0.1:8080")
        with patch("sms_tool.paypal_reverse.CurlSession", _FakeCurl):
            c._new_session()
        self.assertTrue(str(seen["impersonate"]).startswith("chrome"))

    def test_browser_identity_headers_are_installed(self):
        c = _client()
        with patch("sms_tool.paypal_reverse.CurlSession", None):
            session = c._new_session()
        try:
            self.assertIn("User-Agent", session.headers)
            self.assertEqual("?0", session.headers["sec-ch-ua-mobile"])
            self.assertEqual('"Windows"', session.headers["sec-ch-ua-platform"])
            self.assertEqual("document", session.headers["Sec-Fetch-Dest"])
        finally:
            session.close()


class AuthorizeTests(unittest.TestCase):
    def test_browser_fallback_is_reported_with_its_step(self):
        c = _client()
        with patch.object(c, "_new_session", return_value=FakeSession()), \
             patch.object(c, "_do_authorize", side_effect=_NeedBrowserFallback("card", "captcha wall")):
            result = c.authorize()
        self.assertFalse(result.ok)
        self.assertEqual("card", result.failed_step)
        self.assertEqual("[card] captcha wall", result.error)

    def test_unexpected_exception_is_reported_as_unknown_step(self):
        c = _client()
        with patch.object(c, "_new_session", return_value=FakeSession()), \
             patch.object(c, "_do_authorize", side_effect=RuntimeError("boom")):
            result = c.authorize()
        self.assertFalse(result.ok)
        self.assertEqual("unknown", result.failed_step)
        self.assertEqual("boom", result.error)

    def test_session_is_closed_even_on_failure(self):
        c = _client()
        fake = FakeSession()
        with patch.object(c, "_new_session", return_value=fake), \
             patch.object(c, "_do_authorize", side_effect=RuntimeError("boom")):
            c.authorize()
        self.assertTrue(fake.closed)

    def test_session_close_failure_does_not_mask_the_result(self):
        c = _client()
        fake = FakeSession()
        fake.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
        with patch.object(c, "_new_session", return_value=fake), \
             patch.object(c, "_do_authorize", side_effect=RuntimeError("boom")):
            result = c.authorize()
        self.assertEqual("boom", result.error)

    def test_success_is_passed_through(self):
        from sms_tool.paypal_reverse import ReversePayResult

        c = _client()
        expected = ReversePayResult(ok=True, access_token="eyJ1")
        with patch.object(c, "_new_session", return_value=FakeSession()), \
             patch.object(c, "_do_authorize", return_value=expected):
            self.assertIs(expected, c.authorize())


class TryReversePayTests(unittest.TestCase):
    def test_returns_a_plain_dict_with_error_shape(self):
        with patch.object(PayPalReverseClient, "authorize",
                          return_value=_result(ok=False, error="[card] captcha", failed_step="card")):
            out = try_reverse_pay(_PP, {}, {}, "A", "B", "a@b.c", "pw", "+81", {})
        self.assertEqual({
            "ok": False,
            "email": "",
            "error": "[card] captcha",
            "failed_step": "card",
        }, out)

    def test_success_shape_carries_tokens(self):
        with patch.object(PayPalReverseClient, "authorize",
                          return_value=_result(ok=True, access_token="eyJ9")):
            out = try_reverse_pay(_PP, {}, {}, "A", "B", "a@b.c", "pw", "+81", {})
        self.assertTrue(out["ok"])
        self.assertEqual("eyJ9", out["access_token"])
        self.assertNotIn("error", out)

    def test_arguments_reach_the_client(self):
        seen = {}

        def _fake_authorize(self):
            seen.update(redirect_url=self.redirect_url, alias_email=self.alias_email,
                        proxy=self.proxy, timeout=self.timeout)
            return _result(ok=True, access_token="eyJ")

        with patch.object(PayPalReverseClient, "authorize", _fake_authorize):
            try_reverse_pay(_PP, {"number": "1"}, {"city": "Tokyo"}, "A", "B",
                            "taro@example.com", "pw", "+81", {},
                            proxy="http://127.0.0.1:8080", timeout=11)
        self.assertEqual(_PP, seen["redirect_url"])
        self.assertEqual("taro@example.com", seen["alias_email"])
        self.assertEqual("http://127.0.0.1:8080", seen["proxy"])
        self.assertEqual(11, seen["timeout"])


if __name__ == "__main__":
    unittest.main()
