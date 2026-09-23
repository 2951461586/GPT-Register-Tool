"""``sms_tool/codex_phone.py`` 的行为测试（零覆盖 → 全覆盖 + 变异验证）。

**为什么值得测**：手机验证是注册流程里唯一会把**真钱**和**账号存亡**绑在一起的环节。

- 走错分支 = 租来的号码既没完成也没取消 → **号码一直挂着继续计费**，注册却已经往下走了。
- ``_verify_with_reuse_pool`` 的成功/失败整形共用 ``result.get("ok")`` 一个判据，
  池子返回 ``{"ok": 0}`` 和 ``{"ok": True}`` 走的是完全不同的一组字段 —— 键名对不上，
  调用方拿到的就是一堆空串。

2026-09-22 之前这个模块有**两条**路径：池子路径和"legacy 单号"路径（从
``paypal_auto.phone_number`` / ``sms_api_url`` 取一个固定号码，轮询一个固定 URL）。
legacy 路径随静态号池模式一起被移除，理由是它**绕过租号生命周期**：两次 POST 之间
没有任何激活 id，所以既不会 ``complete`` 也不会 ``cancel``。它的用例（``LegacyConfigTest``
/ ``LegacyFlowTest``）与随之删除的 ``_next_url`` / ``_oai_headers`` 用例一并消失，
代之以 ``DispatchTest`` 里"没池子但要求接码"必须**响亮失败**的契约。

模块级依赖都是 ``from X import f``，所以 patch 目标**必须打在 ``codex_phone`` 自己的
命名空间上**；池子入口是**函数内 import**，所以打在**源模块**上才生效。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import codex_phone

POOL_SUCCESS_KEYS = {
    "ok", "next_url", "phone", "provider", "activation_id",
    "reuse_count", "max_reuse_count", "remaining",
}
POOL_FAILURE_KEYS = {"ok", "error", "phone", "body", "message"}


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every ``Constant`` node that *is* a docstring.

    Docstrings are the only string literals that describe rather than act, so
    the residue guard excludes exactly these and nothing else.
    """
    found: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


class _FakeSession:
    """身份对象。``post`` 直接抛 —— 这个模块自己不该发任何 HTTP 请求。"""

    def post(self, *args, **kwargs):
        raise AssertionError("codex_phone must not perform HTTP itself")


class CodexPhoneTestBase(unittest.TestCase):
    def setUp(self):
        self.sentinel = {"sentinel_token": "ST", "sentinel_so_token": "SO"}
        self.pool_result = {"ok": True}
        self.pool_calls = []

        self._start(mock.patch.object(codex_phone, "load_cached_sentinel",
                                      return_value=self.sentinel))
        # 函数内 import → 打在源模块上，调用时才重新读属性。
        self._start(mock.patch(
            "sms_tool.phone_reuse.complete_phone_verification_with_reuse",
            side_effect=self._pool))

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _pool(self, **kwargs):
        self.pool_calls.append(kwargs)
        return self.pool_result

    def _session(self):
        return _FakeSession()


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

    def test_without_a_pool_and_enabled_the_failure_names_the_config_keys(self):
        """🔴 这条取代了原来的 legacy 路径。

        以前"要求接码但没给池子"会退回读 ``paypal_auto`` 的固定号码 —— 那条路已经
        没有了。**不能**退回默认供应商（会拿操作员没写过的配置去花钱租号），
        所以必须响亮失败，并且把该改哪个键写进 message。"""
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur", enabled=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "phone_pool_unavailable")
        self.assertIn("phone_reuse.source", result["message"])
        self.assertIn("phone_reuse.smsbower.api_key", result["message"])
        self.assertEqual(self.pool_calls, [], "没有池子就不该走到池子入口")

    def test_the_unavailable_result_has_exactly_three_keys(self):
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur", enabled=True)
        self.assertEqual(set(result), {"ok", "error", "message"})

    def test_the_disabled_path_is_taken_by_default(self):
        """``enabled`` 的默认值是 ``False`` —— 没传就是不开。"""
        result = codex_phone.complete_phone_verification(
            _FakeSession(), "did", "https://cur")
        self.assertEqual(result["error"], "add_phone_required")

    def test_an_empty_pool_is_still_no_pool(self):
        """⚠️ 判据是 ``if phone_pool:`` 的真值，不是 ``is not None``。
        空字典/空列表/0 都会被当成"没给池子"。"""
        for label, pool in {"empty dict": {}, "empty list": [], "zero": 0,
                            "empty str": ""}.items():
            with self.subTest(label=label):
                self.pool_calls.clear()
                result = codex_phone.complete_phone_verification(
                    _FakeSession(), "did", "https://cur",
                    enabled=True, phone_pool=pool)
                self.assertEqual(self.pool_calls, [],
                                 f"{label!r} 应当被当成「没给池子」")
                self.assertEqual(result["error"], "phone_pool_unavailable")

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


class NoStaticResidueTest(unittest.TestCase):
    """The static phone-pool mode is gone from this module, structurally."""

    def test_the_legacy_helper_is_gone(self):
        self.assertFalse(hasattr(codex_phone, "_verify_with_legacy"))

    def test_the_static_url_helpers_are_gone(self):
        for name in ("_next_url", "_oai_headers"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(codex_phone, name))

    def test_the_module_no_longer_reads_the_paypal_auto_section(self):
        """``CFG`` was imported only for the legacy branch. Keeping the import
        would let a future edit reach for ``paypal_auto`` again without noticing
        that the section is no longer a supported number source."""
        self.assertFalse(hasattr(codex_phone, "CFG"))

    def test_the_module_reads_no_removed_config_key(self):
        """🔴 判据必须打在**代码**上，不是原文上。

        第一版用的是 ``assertNotIn(needle, source)``，它把两样东西当成了回归：

        - 模块自己的 docstring（**说明**了这些键被移除，正是我们要写的东西）；
        - 模块自己的错误码 ``"phone_pool_unavailable"``（含子串 ``phone_pool``）。

        放宽成"豁免这个文件"会把假阳性换成盲区，所以改成解析 AST、只看**会被执行**
        的字符串字面量（排除模块/类/函数 docstring），并且用**精确相等**而不是子串 ——
        因为这里要守的契约是"不再**读**这些配置键"，而读配置在这套代码里就写作
        ``cfg.get("phone_pool")`` / ``cfg["sms_api_url"]``，字面量与键名逐字相等。
        错误码 ``"phone_pool_unavailable"`` 与键名不相等，因此不再误报。
        """
        tree = ast.parse(Path(codex_phone.__file__).read_text(encoding="utf-8"))
        docstrings = _docstring_node_ids(tree)
        executed = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }
        removed_keys = {
            "phone_pool", "phone_number", "phone_numbers",
            "sms_api_url", "phone_index_file",
        }
        for needle in sorted(removed_keys):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, executed)

    def test_the_module_imports_no_removed_config_section(self):
        """配套判据：``paypal_auto`` 这个 section 本身也不许再被 import 进来。"""
        tree = ast.parse(Path(codex_phone.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertEqual(
            {name for name in imported if "paypal_auto" in name}, set())


if __name__ == "__main__":
    unittest.main()
