"""Behaviour tests for sms_tool/paypal_reverse.py -- pure parsing layer (P1).

Target: a 1146-line module that, before this file, had **zero** tests importing
it (AST-verified with coverage_audit.py, not by grepping for function names).
It sits on the money path (PayPal signup + card + authorization), so the
parsing helpers are where a silent mismatch turns into a lost payment.

House rules for this file
-------------------------
* No network, no browser, no real money, no real credentials.
  `_try_solve_captcha` is patched in **every** CAPTCHA-path test: the real one
  imports captcha_solver and will happily launch a browser.
* These tests **pin current behaviour**, they do not ratify it. Where current
  behaviour is surprising, the docstring says so explicitly and marks the test
  as the thing to change first if the semantics are ever fixed.
* Assertions target the returned value, never the source text of the module.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sms_tool.paypal_reverse import (
    _BLOCK_PATTERNS,
    _CAPTCHA_PATTERNS,
    _NeedBrowserFallback,
    PayPalReverseClient,
    ReversePayResult,
)

from paypal_reverse_fakes import FakeCookieJar, FakeResponse, FakeSession, make_client

_CURRENT = "https://www.paypal.com/cgi-bin/webscr?cmd=_express-checkout"


def _client(html: str = "", current_url: str = _CURRENT):
    c = make_client()
    c._session = FakeSession()
    c._current_html = html
    c._current_url = current_url
    return c


def _no_solver(c):
    """CAPTCHA detected but unsolvable -- the branch that escalates to fallback.

    Returning None is what the real `_try_solve_captcha` does when it cannot
    get a token; patching it keeps a browser from ever being launched.
    """
    return patch.object(c, "_try_solve_captcha", return_value=None)


# ────────────────────────────── result contract ──────────────────────────────


class ReversePayResultContractTests(unittest.TestCase):
    def test_success_dict_carries_tokens_and_no_error_fields(self):
        r = ReversePayResult(
            ok=True,
            email="taro@example.com",
            access_token="eyJhbGciOi",
            oauth_refresh_token="rt-1",
            refresh_token_status="oauth_present",
            paypal_status="completed",
            redirect_url="https://chatgpt.com/",
        )
        self.assertEqual(
            r.to_dict(),
            {
                "ok": True,
                "email": "taro@example.com",
                "access_token": "eyJhbGciOi",
                "oauth_refresh_token": "rt-1",
                "refresh_token_status": "oauth_present",
                "paypal_status": "completed",
                "redirect_url": "https://chatgpt.com/",
            },
        )

    def test_failure_dict_omits_every_token_field(self):
        """失败结果里绝不能带 token —— to_dict() 的输出会进日志和落库。"""
        r = ReversePayResult(
            ok=False,
            email="taro@example.com",
            error="captcha wall",
            failed_step="card",
            access_token="eyJLEAK",
            oauth_refresh_token="rt-LEAK",
        )
        d = r.to_dict()
        self.assertEqual(d, {
            "ok": False,
            "email": "taro@example.com",
            "error": "captcha wall",
            "failed_step": "card",
        })
        for leaky in ("eyJLEAK", "rt-LEAK"):
            self.assertNotIn(leaky, repr(d))

    def test_defaults_are_empty_strings(self):
        r = ReversePayResult(ok=True)
        self.assertEqual("", r.email)
        self.assertEqual("", r.paypal_status)
        self.assertEqual("", r.access_token)


# ────────────────────────────── token extraction ──────────────────────────────


class ExtractTokenTests(unittest.TestCase):
    def test_first_non_empty_key_wins(self):
        data = {"accessToken": "", "access_token": "AT-2"}
        self.assertEqual("AT-2", PayPalReverseClient._extract_token(data, "accessToken", "access_token"))

    def test_top_level_takes_precedence_over_nested_session(self):
        data = {"accessToken": "TOP", "session": {"accessToken": "NESTED"}}
        self.assertEqual("TOP", PayPalReverseClient._extract_token(data, "accessToken"))

    def test_falls_back_to_nested_session(self):
        data = {"session": {"accessToken": "NESTED"}}
        self.assertEqual("NESTED", PayPalReverseClient._extract_token(data, "accessToken"))

    def test_non_dict_input_returns_empty_string(self):
        for bad in (None, [], "eyJ", 42):
            with self.subTest(bad=bad):
                self.assertEqual("", PayPalReverseClient._extract_token(bad, "accessToken"))

    def test_non_string_value_is_skipped(self):
        """值为非 str（例如 dict / None）不能当成 token 返回。"""
        data = {"accessToken": {"nested": 1}, "access_token": "OK"}
        self.assertEqual("OK", PayPalReverseClient._extract_token(data, "accessToken", "access_token"))

    def test_empty_string_is_not_a_token(self):
        self.assertEqual("", PayPalReverseClient._extract_token({"accessToken": ""}, "accessToken"))

    def test_session_that_is_not_a_dict_is_ignored(self):
        self.assertEqual("", PayPalReverseClient._extract_token({"session": "nope"}, "accessToken"))

    def test_all_keys_missing_returns_empty(self):
        self.assertEqual("", PayPalReverseClient._extract_token({"user": {}}, "a", "b"))

    def test_session_keys_are_tried_in_the_same_order(self):
        data = {"session": {"refresh_token": "RT-NESTED"}}
        self.assertEqual("RT-NESTED", PayPalReverseClient._extract_token(data, "refreshToken", "refresh_token"))


# ────────────────────────────── HTML attribute helpers ──────────────────────────────


class GetAttrTests(unittest.TestCase):
    def test_double_and_single_quotes_both_parse(self):
        self.assertEqual("v1", PayPalReverseClient._get_attr('<input name="v1">', "name"))
        self.assertEqual("v2", PayPalReverseClient._get_attr("<input name='v2'>", "name"))

    def test_missing_attribute_returns_empty_string(self):
        self.assertEqual("", PayPalReverseClient._get_attr('<input value="x">', "name"))

    def test_lookup_is_case_insensitive(self):
        self.assertEqual("v", PayPalReverseClient._get_attr('<input NAME="v">', "name"))

    def test_unquoted_attribute_is_not_recognised(self):
        """裸写 `name=x` 的 HTML 取不到值 —— 正则只认带引号的。"""
        self.assertEqual("", PayPalReverseClient._get_attr("<input name=x>", "name"))

    def test_suffix_attribute_shadows_the_real_one(self):
        """⚠️ 钉住现状：`data-name="x"` 排在前面时，`name` 取到的是 x。

        正则是裸 `name=["']...["']`，不要求前面是空白，所以任何以 `name` 结尾的
        属性名都会先命中。真实页面如果带 data-* 属性就会取错字段。
        要修语义就先改这个用例。
        """
        tag = '<input data-name="shadow" name="real" value="1">'
        self.assertEqual("shadow", PayPalReverseClient._get_attr(tag, "name"))


class FormActionTests(unittest.TestCase):
    def test_absolute_action_is_returned_verbatim(self):
        c = _client()
        self.assertEqual(
            "https://www.paypal.com/submit",
            c._extract_form_action('<form action="https://www.paypal.com/submit">'),
        )

    def test_root_relative_action_is_absolutised_against_current_url(self):
        c = _client()
        self.assertEqual(
            "https://www.paypal.com/cgi-bin/confirm",
            c._extract_form_action('<form action="/cgi-bin/confirm">'),
        )

    def test_missing_action_falls_back_to_current_url(self):
        c = _client()
        self.assertEqual(_CURRENT, c._extract_form_action("<form>"))

    def test_relative_action_without_leading_slash_is_also_dropped(self):
        """⚠️ 钉住现状：只有 `/` 开头和 `http` 开头的 action 被接受。

        `action="confirm"` 这种相对路径既没被拼成绝对 URL 也没被返回，
        静默落回当前 URL —— 会把表单 POST 到错的地方。
        """
        c = _client()
        self.assertEqual(_CURRENT, c._extract_form_action('<form action="confirm">'))

    def test_action_matching_is_case_insensitive_on_the_tag(self):
        c = _client()
        self.assertEqual(
            "https://www.paypal.com/x",
            c._extract_form_action('<FORM ACTION="https://www.paypal.com/x">'),
        )

    def test_root_relative_uses_scheme_and_host_of_current_url(self):
        c = _client(current_url="http://localhost:8080/a/b?c=1")
        self.assertEqual("http://localhost:8080/go", c._extract_form_action('<form action="/go">'))


class HiddenAndInputFieldTests(unittest.TestCase):
    def test_hidden_fields_are_collected_by_name(self):
        c = _client()
        html = (
            '<form><input type="hidden" name="csrf" value="T1">'
            '<input type="hidden" name="step" value="review"></form>'
        )
        self.assertEqual({"csrf": "T1", "step": "review"}, c._extract_hidden_fields(html))

    def test_hidden_input_without_name_is_skipped(self):
        c = _client()
        self.assertEqual({}, c._extract_hidden_fields('<input type="hidden" value="orphan">'))

    def test_hidden_value_defaults_to_empty_string(self):
        c = _client()
        self.assertEqual({"flag": ""}, c._extract_hidden_fields('<input type="hidden" name="flag">'))

    def test_type_can_appear_after_name(self):
        c = _client()
        self.assertEqual({"a": "1"}, c._extract_hidden_fields('<input name="a" value="1" type="hidden">'))

    def test_non_hidden_inputs_are_not_collected(self):
        c = _client()
        self.assertEqual({}, c._extract_hidden_fields('<input type="text" name="a" value="1">'))

    def test_visible_inputs_are_collected_too(self):
        c = _client()
        html = '<input name="email" value="a@b.c"><input name="password" value="">'
        self.assertEqual({"email": "a@b.c", "password": ""}, c._extract_input_fields(html))

    def test_submit_button_and_image_inputs_are_excluded(self):
        """提交控件不是数据字段，混进去会被当成表单值 POST 出去。"""
        c = _client()
        html = (
            '<input type="submit" name="btn" value="Pay">'
            '<input type="button" name="b2" value="x">'
            '<input type="image" name="b3" value="y">'
            '<input type="text" name="keep" value="z">'
        )
        self.assertEqual({"keep": "z"}, c._extract_input_fields(html))

    def test_input_type_comparison_is_case_insensitive(self):
        c = _client()
        self.assertEqual({}, c._extract_input_fields('<input TYPE="Submit" name="btn" value="x">'))

    def test_input_without_type_attribute_is_kept(self):
        c = _client()
        self.assertEqual({"anon": "v"}, c._extract_input_fields('<input name="anon" value="v">'))

    def test_hidden_inputs_are_also_returned_by_input_fields(self):
        """`_extract_input_fields` 不过滤 hidden —— 两类提取器有重叠。"""
        c = _client()
        self.assertEqual({"a": "1"}, c._extract_input_fields('<input type="hidden" name="a" value="1">'))


class SelectFieldTests(unittest.TestCase):
    def test_selected_option_wins(self):
        c = _client()
        html = '<select name="country"><option value="US">US</option><option selected value="JP">JP</option></select>'
        self.assertEqual({"country": "JP"}, c._extract_select_fields(html))

    def test_first_option_used_when_nothing_selected(self):
        c = _client()
        html = '<select name="country"><option value="US">US</option><option value="JP">JP</option></select>'
        self.assertEqual({"country": "US"}, c._extract_select_fields(html))

    def test_select_without_options_yields_empty_value(self):
        c = _client()
        self.assertEqual({"country": ""}, c._extract_select_fields('<select name="country"></select>'))

    def test_value_written_before_selected_is_not_recognised(self):
        """⚠️ 钉住现状：`selected` 必须写在 `value` **之前**才算选中。

        `<option value="JP" selected>` 会被当成「没有选中」，静默回退到第一个
        option。这种属性顺序在 HTML 里完全合法，属于真实的选错国家风险。
        """
        c = _client()
        html = '<select name="country"><option value="US">US</option><option value="JP" selected>JP</option></select>'
        self.assertEqual({"country": "US"}, c._extract_select_fields(html))

    def test_select_without_name_is_skipped(self):
        c = _client()
        self.assertEqual({}, c._extract_select_fields('<select><option value="x"></option></select>'))

    def test_multiple_selects_are_all_collected(self):
        c = _client()
        html = ('<select name="a"><option value="1" selected>1</option></select>'
                '<select name="b"><option value="2" selected>2</option></select>')
        self.assertEqual({"a": "1", "b": "2"}, c._extract_select_fields(html))


class FindFormByFieldsTests(unittest.TestCase):
    def test_returns_none_when_page_has_no_form(self):
        c = _client("<html><body>no forms here</body></html>")
        self.assertIsNone(c._find_form_by_fields(["email"]))

    def test_matching_form_returns_action_and_fields(self):
        c = _client(
            '<form action="/signup"><input type="hidden" name="csrf" value="T">'
            '<input name="email" value=""></form>'
        )
        action, fields = c._find_form_by_fields(["email"])
        self.assertEqual("https://www.paypal.com/signup", action)
        self.assertEqual({"csrf": "T", "email": ""}, fields)

    def test_field_match_is_case_insensitive(self):
        c = _client('<form action="/a"><input name="Login_Email" value=""></form>')
        self.assertIsNotNone(c._find_form_by_fields(["login_email"]))

    def test_first_form_containing_any_requested_name_wins(self):
        c = _client(
            '<form action="/one"><input name="phone" value=""></form>'
            '<form action="/two"><input name="email" value=""></form>'
        )
        action, _ = c._find_form_by_fields(["email"])
        self.assertEqual("https://www.paypal.com/two", action)

    def test_select_fields_are_merged_last_and_win(self):
        """合并顺序 hidden → input → select，后者覆盖前者。"""
        c = _client(
            '<form action="/a"><input type="hidden" name="country" value="HID">'
            '<select name="country"><option selected value="JP">JP</option></select></form>'
        )
        _, fields = c._find_form_by_fields(["country"])
        self.assertEqual("JP", fields["country"])

    def test_substring_match_means_wrong_form_can_be_picked(self):
        """⚠️ 钉住现状：匹配用的是 `name.lower() in form_lower`，是子串匹配。

        搜 "email" 会被表单任意位置出现的 "email"（比如一段文案）命中，
        于是可能选中不含 email 输入框的表单。
        """
        c = _client('<form action="/wrong"><span>contact email support</span></form>'
                    '<form action="/right"><input name="login_email" value=""></form>')
        action, _ = c._find_form_by_fields(["email"])
        self.assertEqual("https://www.paypal.com/wrong", action)


class FindSubmitFormTests(unittest.TestCase):
    def test_form_with_agree_button_text_is_accepted(self):
        c = _client('<form action="/a"><button type="submit">Agree and Continue</button></form>')
        action, fields = c._find_submit_form()
        self.assertEqual("https://www.paypal.com/a", action)
        self.assertEqual({}, fields)

    def test_form_without_submit_hint_is_rejected(self):
        c = _client('<form action="/a"><input name="x" value="y"></form>')
        self.assertIsNone(c._find_submit_form())

    def test_returns_none_without_any_form(self):
        c = _client("<div>nothing</div>")
        self.assertIsNone(c._find_submit_form())

    def test_every_submit_keyword_is_recognised(self):
        for word in ("Agree", "Pay Now", "Continue", "Submit", "Confirm", "Authorize"):
            with self.subTest(word=word):
                c = _client(f'<form action="/a"><button>{word}</button></form>')
                self.assertIsNotNone(c._find_submit_form(), word)


class SignupFormTests(unittest.TestCase):
    def test_signup_form_is_found_by_every_email_alias(self):
        for name in ("email", "login_email", "signup_email", "login_emailcopy"):
            with self.subTest(name=name):
                c = _client(f'<form action="/s"><input name="{name}" value=""></form>')
                action, _ = c._find_signup_form()
                self.assertEqual("https://www.paypal.com/s", action)

    def test_signup_form_absent_returns_none(self):
        c = _client('<form action="/s"><input name="phone" value=""></form>')
        self.assertIsNone(c._find_signup_form())


# ────────────────────────────── page classification ──────────────────────────────


class IsJsOnlyPageTests(unittest.TestCase):
    def test_very_short_html_is_treated_as_js_rendered(self):
        c = _client("<div id='app'></div>")
        self.assertTrue(c._is_js_only_page())

    def test_empty_mount_point_is_treated_as_js_rendered(self):
        for mount in ("app", "root", "__next"):
            with self.subTest(mount=mount):
                body = f'<div id="{mount}"></div>' + "x" * 600
                c = _client(body)
                self.assertTrue(c._is_js_only_page())

    def test_mount_point_with_children_is_a_real_page(self):
        c = _client('<div id="root"><span>hello</span></div>' + "x" * 600)
        self.assertFalse(c._is_js_only_page())

    def test_long_plain_html_is_a_real_page(self):
        c = _client("<html><body>" + "y" * 700 + "</body></html>")
        self.assertFalse(c._is_js_only_page())

    def test_boundary_at_500_chars(self):
        """499 → JS-only，500 → 正常。边界值必须钉死，改了就是改判定口径。"""
        self.assertTrue(_client("z" * 499)._is_js_only_page())
        self.assertFalse(_client("z" * 500)._is_js_only_page())


class CheckBlockedTests(unittest.TestCase):
    def test_clean_page_raises_nothing(self):
        c = _client()
        c._check_blocked("<html><body>Welcome</body></html>", "load_page")

    def test_captcha_wall_raises_fallback_with_step(self):
        c = _client()
        with _no_solver(c):
            with self.assertRaises(_NeedBrowserFallback) as ctx:
                c._check_blocked('<div class="g-recaptcha"></div>', "card")
        self.assertEqual("card", ctx.exception.step)
        self.assertIn("CAPTCHA detected at card step", str(ctx.exception))

    def test_block_page_raises_fallback_with_step(self):
        c = _client()
        with self.assertRaises(_NeedBrowserFallback) as ctx:
            c._check_blocked("<p>Access Denied</p>", "submit")
        self.assertEqual("submit", ctx.exception.step)
        self.assertIn("page blocked at submit step", str(ctx.exception))

    def test_captcha_takes_precedence_over_block_wording(self):
        """同一页面同时命中两组模式时，先按 CAPTCHA 处理。"""
        c = _client()
        with _no_solver(c):
            with self.assertRaises(_NeedBrowserFallback) as ctx:
                c._check_blocked("<p>Access denied</p><div class='recaptcha'></div>", "card")
        self.assertIn("CAPTCHA", str(ctx.exception))

    def test_nodriver_flag_short_circuits_without_any_request(self):
        """nodriver 已解过 CAPTCHA 时，整个检查必须跳过 —— 连请求都不许发。

        `_try_solve_captcha` 也用哨兵替掉：一旦短路失效，真实的 solver 会被
        调起来并真的去开浏览器（实测一次 3 分 40 秒），那样这个用例就不是
        失败而是挂住。哨兵保证它立刻失败。
        """
        c = _client()
        c._captcha_solved_by_nodriver = True
        with patch.object(c, "_try_solve_captcha",
                          side_effect=AssertionError("CAPTCHA 检查没有被短路")):
            c._check_blocked('<div class="g-recaptcha"></div>', "card")
        self.assertEqual([], c._session.calls)

    def test_every_captcha_pattern_is_reachable(self):
        samples = [
            "data-app='authchallenge_response",
            'id="captcha-standalone"',
            "data-enable-ads-captcha='true'",
            "adsddcaptcha",
            "ngrlCaptcha",
            "g-recaptcha",
            "recaptcha",
            "are you a human",
            "verify you are human",
        ]
        self.assertEqual(len(samples), len(_CAPTCHA_PATTERNS))
        for sample in samples:
            with self.subTest(sample=sample):
                c = _client()
                with _no_solver(c):
                    with self.assertRaises(_NeedBrowserFallback):
                        c._check_blocked(f"<div>{sample}</div>", "load_page")

    def test_every_block_pattern_is_reachable(self):
        samples = [
            "unusual activity",
            "temporarily locked",
            "try again later",
            "access denied",
            "unable to process",
        ]
        self.assertEqual(len(samples), len(_BLOCK_PATTERNS))
        for sample in samples:
            with self.subTest(sample=sample):
                c = _client()
                with self.assertRaises(_NeedBrowserFallback):
                    c._check_blocked(f"<div>{sample}</div>", "load_page")

    def test_solved_captcha_clears_the_wall(self):
        """解出来且复检干净 → 不抛，页面被重新拉过一次，挑战被提交过一次。"""
        c = _client()
        c._session = FakeSession([FakeResponse(200, "<html>clean now</html>", _CURRENT)])
        with patch.object(c, "_try_solve_captcha", return_value="tok"), \
             patch.object(c, "_submit_captcha_challenge") as submit:
            c._check_blocked('<div class="g-recaptcha"></div>', "card")
        submit.assert_called_once()
        self.assertEqual("<html>clean now</html>", c._current_html)
        self.assertEqual(1, len(c._session.calls))

    def test_captcha_persisting_after_solve_still_falls_back(self):
        c = _client()
        c._session = FakeSession([FakeResponse(200, '<div class="g-recaptcha"></div>', _CURRENT)])
        with patch.object(c, "_try_solve_captcha", return_value="tok"), \
             patch.object(c, "_submit_captcha_challenge"):
            with self.assertRaises(_NeedBrowserFallback):
                c._check_blocked('<div class="g-recaptcha"></div>', "card")

    def test_nodriver_cookies_without_flag_falls_back(self):
        """`__nodriver_cookies__` 但标志没置位 → 视为没解成功，继续走 fallback。"""
        c = _client()
        c._session = FakeSession([FakeResponse(200, '<div class="g-recaptcha"></div>', _CURRENT)])
        with patch.object(c, "_try_solve_captcha", return_value="__nodriver_cookies__"):
            with self.assertRaises(_NeedBrowserFallback):
                c._check_blocked('<div class="g-recaptcha"></div>', "card")
        self.assertFalse(c._captcha_solved_by_nodriver)

    def test_nodriver_cookies_with_flag_set_continues(self):
        c = _client()
        c._session = FakeSession([FakeResponse(200, '<div class="g-recaptcha"></div>', _CURRENT)])

        def _solve(html, step):
            c._captcha_solved_by_nodriver = True
            return "__nodriver_cookies__"

        with patch.object(c, "_try_solve_captcha", side_effect=_solve):
            c._check_blocked('<div class="g-recaptcha"></div>', "card")
        self.assertTrue(c._captcha_solved_by_nodriver)


# ────────────────────────────── CSRF / cookies ──────────────────────────────


class UpdateCsrfTests(unittest.TestCase):
    def test_meta_tag_wins(self):
        c = _client()
        c._update_csrf(FakeResponse(200, '<meta name="csrf-token" content="META">'))
        self.assertEqual("META", c._csrf_token)

    def test_meta_tag_variant_with_underscore(self):
        c = _client()
        c._update_csrf(FakeResponse(200, "<meta name='csrf_token' content='M2'>"))
        self.assertEqual("M2", c._csrf_token)

    def test_hidden_input_used_when_no_meta(self):
        c = _client()
        c._update_csrf(FakeResponse(200, '<input name="_token" value="INP">'))
        self.assertEqual("INP", c._csrf_token)

    def test_meta_takes_precedence_over_hidden_input(self):
        c = _client()
        html = '<input name="_token" value="INP"><meta name="csrf-token" content="META">'
        c._update_csrf(FakeResponse(200, html))
        self.assertEqual("META", c._csrf_token)

    def test_cookie_used_as_last_resort(self):
        c = _client()
        c._update_csrf(FakeResponse(200, "nothing", cookies={"csrf_token": "CK"}))
        self.assertEqual("CK", c._csrf_token)

    def test_csrf_cookie_name_match_is_case_insensitive(self):
        c = _client()
        c._update_csrf(FakeResponse(200, "nothing", cookies={"csrfToken": "CK2"}))
        self.assertEqual("CK2", c._csrf_token)

    def test_dashed_xsrf_token_cookie_is_not_recognised(self):
        """⚠️ 真实 bug（本用例钉住现状，不是认可它）。

        判定只认 ``"csrf"`` 和 ``"xsrftoken"`` 两个子串，而 Django / Express
        默认下发的 cookie 名是带连字符的 ``XSRF-TOKEN`` —— 两个子串都不匹配，
        CSRF 静默取不到，后续表单提交会少 X-CSRF-Token 头。
        要修语义就先改这个用例。
        """
        c = _client()
        c._update_csrf(FakeResponse(200, "nothing", cookies={"XSRF-TOKEN": "CK"}))
        self.assertEqual("", c._csrf_token)

    def test_token_is_untouched_when_nothing_matches(self):
        c = _client()
        c._csrf_token = "PREV"
        c._update_csrf(FakeResponse(200, "<html>plain</html>"))
        self.assertEqual("PREV", c._csrf_token)

    def test_response_without_text_attribute_is_treated_as_empty(self):
        """`hasattr(response, "text")` 为假时按空 HTML 处理，不抛异常。"""
        c = _client()
        c._csrf_token = "PREV"

        class _NoText:
            cookies = FakeCookieJar()

        c._update_csrf(_NoText())
        self.assertEqual("PREV", c._csrf_token)


class CookieDictTests(unittest.TestCase):
    def test_requests_style_jar_goes_through_get_dict(self):
        resp = FakeResponse(200, cookies={"a": "1"})
        self.assertTrue(hasattr(resp.cookies, "get_dict"))
        self.assertEqual({"a": "1"}, PayPalReverseClient._cookie_dict(resp))

    def test_dict_like_jar_is_copied(self):
        class _Resp:
            cookies = {"k": "v"}

        self.assertEqual({"k": "v"}, PayPalReverseClient._cookie_dict(_Resp()))

    def test_empty_jar_returns_empty_dict(self):
        self.assertEqual({}, PayPalReverseClient._cookie_dict(FakeResponse(200)))

    def test_result_is_a_snapshot_not_the_live_jar(self):
        resp = FakeResponse(200, cookies={"a": "1"})
        snapshot = PayPalReverseClient._cookie_dict(resp)
        resp.cookies.set("b", "2")
        self.assertEqual({"a": "1"}, snapshot)


if __name__ == "__main__":
    unittest.main()
