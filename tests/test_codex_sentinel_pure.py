"""Behaviour tests for :mod:`sms_tool.codex_sentinel`.

为什么写这个模块
================
``codex_sentinel.py`` 只有 67 行，却是**注册 / 鉴权链路上所有 HTTP 出口的公共前缀**：

- ``account_creation.py:66``  注册校验头
- ``codex_oauth.py``          OAuth 全流程（7 处 ``with_sentinel`` / ``attach_sentinel``）
- ``codex_phone.py:87,113``   接码平台登录
- ``phone_reuse.py:755,803``  手机号复用判定
- ``sentinel_tokens.py:148``  Cloudflare cookie 回灌
- ``registration.py:66``      主注册流程

而且它带一条**账号隔离的安全契约**（源码 :36 的注释）：

    # Keep Cloudflare/auth cookies, but never reuse a global oai-did across accounts.

``oai-did`` 是 OpenAI 的设备指纹 cookie。跨账号复用会让一批注册账号被判定为同一设备，
后果是整个批次的账号一起被风控。这条契约目前**没有任何测试守着**——
``tests/test_codex_oauth.py`` 里 7 处全是 ``patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={})``，
把整个模块 stub 成了空字典，属于典型的伪覆盖。

已钉住的行为（改之前先读）
==========================
1. ``with_sentinel(h)`` 会读缓存，``with_sentinel(h, {})`` **不会**——
   判据是 ``is not None``，不是真值。传空字典等于"明确要求不要哨兵"。
2. ``attach_sentinel`` 两个头用的是**两种不同语义**：
   ``token`` 直接赋值（覆盖），``so_token`` 用 ``setdefault``（保留调用方已有的值）。
3. ``attach_sentinel`` **原地修改**传入的 dict；``with_sentinel`` 才做拷贝。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import codex_sentinel

SENTINEL_FILE = "sentinel_cache.json"


class _FakeCookies:
    """记录每一次 ``set``，并允许按名字模拟失败。"""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def set(self, name, value, domain=None, path=None):
        if name in self.fail_on:
            raise RuntimeError(f"boom: {name}")
        self.calls.append({"name": name, "value": value, "domain": domain, "path": path})


class _FakeSession:
    def __init__(self, fail_on=()):
        self.cookies = _FakeCookies(fail_on)

    @property
    def names(self):
        return [c["name"] for c in self.cookies.calls]


def _write(path, text, *, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


class LoadCachedSentinelTests(unittest.TestCase):
    """``load_cached_sentinel`` 的任何失败都必须退化成 ``{}``，绝不能抛出去——
    它在 7 个调用点上都处在注册主流程里，抛异常等于注册直接中断。"""

    def setUp(self):
        """``tmp_path`` 这类 fixture 参不进 ``unittest.TestCase`` 的方法，自己开目录。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _load(self, text=None, *, encoding="utf-8"):
        target = self.tmp_path / SENTINEL_FILE
        if text is not None:
            _write(target, text, encoding=encoding)
        with mock.patch.object(codex_sentinel, "runtime_file", lambda *_: target):
            return codex_sentinel.load_cached_sentinel()

    def test_missing_file_yields_empty_dict(self):
        self.assertEqual(self._load(), {})

    def test_a_valid_dict_is_returned_as_is(self):
        payload = {"sentinel_token": "tok", "cookie_str": "a=1"}
        self.assertEqual(self._load(json.dumps(payload)), payload)

    def test_a_json_list_is_rejected(self):
        self.assertEqual(self._load(json.dumps([1, 2, 3])), {})

    def test_a_json_string_is_rejected(self):
        self.assertEqual(self._load(json.dumps("tok")), {})

    def test_json_null_is_rejected(self):
        self.assertEqual(self._load(json.dumps(None)), {})

    def test_malformed_json_is_rejected(self):
        self.assertEqual(self._load("{not json"), {})

    def test_an_empty_file_is_rejected(self):
        self.assertEqual(self._load(""), {})

    def test_a_bom_is_tolerated(self):
        payload = {"sentinel_token": "tok"}
        self.assertEqual(
            self._load(json.dumps(payload), encoding="utf-8-sig"), payload
        )

    def test_a_directory_in_place_of_the_file_is_rejected(self):
        """路径被同名的目录占了（手工误建 / 同步软件冲突）时不能炸。"""
        (self.tmp_path / SENTINEL_FILE).mkdir(parents=True, exist_ok=True)
        self.assertEqual(self._load(), {})


class ImportCookieHeaderTests(unittest.TestCase):
    def test_each_semicolon_separated_pair_is_imported(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "a=1; b=2", "chatgpt.com")
        self.assertEqual(
            session.cookies.calls,
            [
                {"name": "a", "value": "1", "domain": "chatgpt.com", "path": "/"},
                {"name": "b", "value": "2", "domain": "chatgpt.com", "path": "/"},
            ],
        )

    def test_surrounding_whitespace_is_stripped(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "  a  =  1  ", "d")
        self.assertEqual(session.cookies.calls[0]["name"], "a")
        self.assertEqual(session.cookies.calls[0]["value"], "1")

    def test_a_value_may_contain_an_equals_sign(self):
        """``split("=", 1)`` 而不是 ``split("=")``——base64 cookie 值里带 ``=``。"""
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "a=b=c=", "d")
        self.assertEqual(session.cookies.calls[0]["value"], "b=c=")

    def test_an_item_without_an_equals_sign_is_skipped(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "flag; a=1", "d")
        self.assertEqual(session.names, ["a"])

    def test_an_empty_name_is_skipped(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "=1; a=2", "d")
        self.assertEqual(session.names, ["a"])

    def test_an_empty_value_is_skipped(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "a=; b=2", "d")
        self.assertEqual(session.names, ["b"])

    def test_a_trailing_semicolon_adds_nothing(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "a=1;", "d")
        self.assertEqual(session.names, ["a"])

    def test_none_and_empty_headers_import_nothing(self):
        for header in (None, "", "   "):
            with self.subTest(header=header):
                session = _FakeSession()
                codex_sentinel.import_cookie_header(session, header, "d")
                self.assertEqual(session.cookies.calls, [])

    def test_cookies_are_scoped_to_the_site_root(self):
        """``path="/"`` 是 Cloudflare cookie 能随后续所有请求带上的前提。
        收窄到某个子路径，后续请求就裸奔了。"""
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "cf=v1", "chatgpt.com")
        self.assertEqual(session.cookies.calls[0]["path"], "/")

    def test_the_domain_passed_in_is_used_verbatim(self):
        session = _FakeSession()
        codex_sentinel.import_cookie_header(session, "cf=v1", "auth.openai.com")
        self.assertEqual(session.cookies.calls[0]["domain"], "auth.openai.com")

    def test_one_bad_cookie_does_not_stop_the_rest(self):
        """⚠️ 每个 cookie 各包一层 ``try``。requests 的 cookie jar 对非法域名/值会抛
        ``CookieConflictError`` 之类，一颗坏 cookie 不该让整批 Cloudflare cookie 全丢。"""
        session = _FakeSession(fail_on=["boom"])
        codex_sentinel.import_cookie_header(session, "a=1; boom=2; c=3", "d")
        self.assertEqual(session.names, ["a", "c"])


class StripCookieNamesTests(unittest.TestCase):
    def test_a_blocked_name_is_removed(self):
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1; oai-did=x; b=2", {"oai-did"}),
            "a=1; b=2",
        )

    def test_blocking_is_case_insensitive(self):
        """cookie 名大小写不敏感，黑名单必须两边都归一。"""
        for header in ("OAI-DID=x; a=1", "oai-did=x; a=1", "Oai-Did=x; a=1"):
            with self.subTest(header=header):
                self.assertEqual(
                    codex_sentinel.strip_cookie_names(header, {"oai-did"}), "a=1"
                )

    def test_blocked_names_may_be_spelled_with_mixed_case(self):
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1; oai-did=x", {"OAI-DID"}), "a=1"
        )

    def test_multiple_names_can_be_blocked_at_once(self):
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1; x=2; b=3; y=4", {"x", "y"}),
            "a=1; b=3",
        )

    def test_the_header_is_rebuilt_with_a_canonical_separator(self):
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1;b=2", set()), "a=1; b=2"
        )

    def test_items_without_an_equals_sign_are_dropped(self):
        self.assertEqual(codex_sentinel.strip_cookie_names("flag; a=1", set()), "a=1")

    def test_names_and_values_are_normalised(self):
        """输出是重新拼的字符串，不是原样切片。"""
        self.assertEqual(codex_sentinel.strip_cookie_names("  a  =  1  ;b=2", set()),
                         "a=1; b=2")

    def test_a_blank_blocklist_entry_cannot_match_an_empty_cookie_name(self):
        """⚠️ 生产会先把黑名单里的空名过滤掉（源码 :57 的 ``if str(name or "").strip()``）。
        少了这层过滤，``{"", None}`` 会变成 ``blocked == {""}``，于是 ``"=1"`` 这种
        畸形条目被当成"命中黑名单"删掉——调用方传进来的黑名单里混个空值，
        就会静默改变哪些 cookie 能过。"""
        self.assertEqual(
            codex_sentinel.strip_cookie_names("=1; a=2", {"", None}), "=1; a=2"
        )

    def test_blank_blocked_names_are_ignored(self):
        """``blocked`` 里会混入 ``None`` / ``""``；若不先过滤，空名字会把
        ``"=1"`` 这类条目整段删掉（虽然它们本来也导不进去）。"""
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1; b=2", {"", None, "  "}), "a=1; b=2"
        )

    def test_empty_and_none_inputs_yield_an_empty_string(self):
        for header in (None, "", "   "):
            with self.subTest(header=header):
                self.assertEqual(codex_sentinel.strip_cookie_names(header, {"a"}), "")

    def test_none_names_blocks_nothing(self):
        self.assertEqual(
            codex_sentinel.strip_cookie_names("a=1; b=2", None), "a=1; b=2"
        )


class ImportCachedAuthCookiesTests(unittest.TestCase):
    """这里是安全契约的落点：Cloudflare cookie 要留，``oai-did`` 必须剔除。"""

    @staticmethod
    def _run(cookie_str, fail_on=()):
        session = _FakeSession(fail_on)
        sentinel = {"cookie_str": cookie_str, "sentinel_token": "tok"}
        with mock.patch.object(codex_sentinel, "load_cached_sentinel", lambda: sentinel):
            returned = codex_sentinel.import_cached_auth_cookies(session)
        return session, returned

    def test_the_device_cookie_is_stripped_before_import(self):
        """🔴 跨账号复用 ``oai-did`` = 一批账号共用设备指纹 = 整批被风控。"""
        session, _ = self._run("cf=v1; oai-did=FINGERPRINT; other=2")
        self.assertEqual(session.names, ["cf", "other"])
        self.assertNotIn("oai-did", session.names)

    def test_cloudflare_cookies_survive(self):
        session, _ = self._run("__cf_bm=abc; cf_clearance=xyz")
        self.assertEqual(session.names, ["__cf_bm", "cf_clearance"])

    def test_cookies_are_bound_to_the_auth_domain(self):
        session, _ = self._run("cf=v1")
        self.assertEqual(session.cookies.calls[0]["domain"], "auth.openai.com")

    def test_the_sentinel_dict_is_returned_to_the_caller(self):
        _, returned = self._run("cf=v1")
        self.assertEqual(returned["sentinel_token"], "tok")

    def test_a_missing_cookie_str_imports_nothing(self):
        session = _FakeSession()
        with mock.patch.object(codex_sentinel, "load_cached_sentinel", lambda: {}):
            codex_sentinel.import_cached_auth_cookies(session)
        self.assertEqual(session.cookies.calls, [])

    def test_a_non_string_cookie_str_does_not_crash(self):
        """缓存被别的代码写坏时 ``cookie_str`` 可能是 dict/list。``str()`` 兜住，
        结果是什么都导不进去，但不抛异常。"""
        session, _ = self._run({"unexpected": 1})
        self.assertEqual(session.cookies.calls, [])

    def test_a_bad_cookie_does_not_abort_the_import(self):
        session, _ = self._run("cf=v1; boom=2; other=3", fail_on=["boom"])
        self.assertEqual(session.names, ["cf", "other"])


class WithSentinelTests(unittest.TestCase):
    TOKEN = {"sentinel_token": "tok", "sentinel_so_token": "so"}

    def _with(self, headers=None, sentinel=None):
        with mock.patch.object(
            codex_sentinel, "load_cached_sentinel", lambda: self.TOKEN
        ):
            return codex_sentinel.with_sentinel(headers, sentinel)

    def test_the_caller_dict_is_never_mutated(self):
        headers = {"accept": "application/json"}
        self._with(headers)
        self.assertEqual(headers, {"accept": "application/json"})

    def test_existing_headers_are_preserved(self):
        merged = self._with({"accept": "application/json"})
        self.assertEqual(merged["accept"], "application/json")

    def test_both_sentinel_headers_are_attached(self):
        merged = self._with({})
        self.assertEqual(merged["openai-sentinel-token"], "tok")
        self.assertEqual(merged["openai-sentinel-so-token"], "so")

    def test_no_sentinel_argument_loads_the_cache(self):
        self.assertEqual(self._with({})["openai-sentinel-token"], "tok")

    def test_an_explicit_empty_dict_does_not_load_the_cache(self):
        """⚠️ 判据是 ``sentinel is not None``，不是真值。
        传 ``{}`` 是调用方在明确说"这次别带哨兵"——这是 ``codex_oauth`` 里
        若干"未登录态"请求依赖的行为。改成真值判定会悄悄给这些请求加上哨兵头。"""
        self.assertNotIn("openai-sentinel-token", self._with({}, {}))

    def test_a_none_headers_argument_is_tolerated(self):
        self.assertEqual(self._with(None)["openai-sentinel-token"], "tok")


class AttachSentinelTests(unittest.TestCase):
    def test_the_token_header_is_written(self):
        headers = {}
        codex_sentinel.attach_sentinel(headers, {"sentinel_token": "tok"})
        self.assertEqual(headers, {"openai-sentinel-token": "tok"})

    def test_the_so_token_header_is_written(self):
        headers = {}
        codex_sentinel.attach_sentinel(headers, {"sentinel_so_token": "so"})
        self.assertEqual(headers, {"openai-sentinel-so-token": "so"})

    def test_values_are_stripped(self):
        headers = {}
        codex_sentinel.attach_sentinel(
            headers, {"sentinel_token": "  tok  ", "sentinel_so_token": " so\n"}
        )
        self.assertEqual(headers["openai-sentinel-token"], "tok")
        self.assertEqual(headers["openai-sentinel-so-token"], "so")

    def test_blank_values_add_no_header(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                headers = {}
                codex_sentinel.attach_sentinel(
                    headers, {"sentinel_token": value, "sentinel_so_token": value}
                )
                self.assertEqual(headers, {})

    def test_missing_keys_add_no_header(self):
        headers = {}
        codex_sentinel.attach_sentinel(headers, {})
        self.assertEqual(headers, {})

    def test_the_two_headers_use_different_write_semantics(self):
        """⚠️ ``token`` 是覆盖赋值，``so_token`` 是 ``setdefault``。
        两行结构几乎一样，只差一个方法名——必须分别断言，否则改坏任何一个都测不出来。"""
        headers = {
            "openai-sentinel-token": "caller-token",
            "openai-sentinel-so-token": "caller-so",
        }
        codex_sentinel.attach_sentinel(
            headers, {"sentinel_token": "cache-token", "sentinel_so_token": "cache-so"}
        )
        self.assertEqual(headers["openai-sentinel-token"], "cache-token",
                         "the plain token overwrites whatever the caller set")
        self.assertEqual(headers["openai-sentinel-so-token"], "caller-so",
                         "the so-token must NOT overwrite the caller's value")

    def test_a_non_string_token_is_stringified(self):
        headers = {}
        codex_sentinel.attach_sentinel(headers, {"sentinel_token": 12345})
        self.assertEqual(headers["openai-sentinel-token"], "12345")

    def test_none_sentinel_is_a_no_op(self):
        headers = {"a": "1"}
        codex_sentinel.attach_sentinel(headers, None)
        self.assertEqual(headers, {"a": "1"})

    def test_headers_are_mutated_in_place(self):
        """``attach_sentinel`` 不拷贝——调用方传进来什么 dict 就被改成什么。
        想保留原值得自己用 ``with_sentinel``。"""
        headers = {}
        self.assertIsNone(codex_sentinel.attach_sentinel(headers, {"sentinel_token": "t"}))
        self.assertEqual(headers, {"openai-sentinel-token": "t"})


if __name__ == "__main__":
    unittest.main()
