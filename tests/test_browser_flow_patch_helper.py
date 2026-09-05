r"""``tests/browser_flow_patch.py`` 的守卫测试。

这个 helper 存在的唯一理由，是**模块拆分后 patch 会静默失效**。
而"静默失效"意味着：helper 自己写错时，也不会有任何测试红 ——
除非这里专门锁住它的三个语义：

1. 副本表与 AST 扫描一致（表不过时）。
2. 多副本符号被**全部**打上，且装的是同一个 mock（跨模块调用记录合并）。
3. 装饰器注入顺序是「由内到外」——写反了会整体错位一位，
   且因为 MagicMock 什么参数都接受，症状只是安静地断言失败。

第 3 条尤其重要：它是本次修复中真正花时间定位的 bug。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from browser_flow_patch import copy_targets, patch_bf

PKG_DIR = Path(__file__).resolve().parents[1] / "sms_tool" / "registration_drivers" / "browser_flow"
PKG_DOTTED = "sms_tool.registration_drivers.browser_flow"


def _ast_holders(symbol: str) -> set[str]:
    """用 AST 独立算一遍：哪些子模块的顶层命名空间里持有该符号。

    顶层定义 + re-export（`__init__.py` 里 `from .x import symbol`）都算。
    与 copy_targets 的运行时 introspection 互为对照 —— 两条独立路径
    得出同一结果，才算证明副本表没过时。
    """
    holders = set()
    for f in sorted(PKG_DIR.glob("*.py")):
        mod = f.stem if f.name != "__init__.py" else "__init__"
        dotted = PKG_DOTTED if mod == "__init__" else f"{PKG_DOTTED}.{mod}"
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法坏了是别的问题
            continue
        for node in tree.body:
            # 注意加的是 "<模块>.<符号>"，不是模块本身 —— 与 copy_targets 同构
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    holders.add(f"{dotted}.{symbol}")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == symbol:
                        holders.add(f"{dotted}.{symbol}")
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == symbol:
                    holders.add(f"{dotted}.{symbol}")
            elif isinstance(node, ast.ImportFrom):
                # re-export: __init__.py 的 `from .dom_fields import _first_visible`
                for a in node.names:
                    if (a.asname or a.name) == symbol:
                        holders.add(f"{dotted}.{symbol}")
    return holders


class CopyTargetsTests(unittest.TestCase):
    def test_known_multi_copy_symbols_are_all_covered(self):
        """多副本符号必须在所有持有者上都被找到。"""
        for symbol in ("_wait_for_registration_state", "_first_visible",
                       "_manual_challenge", "_otp_fields"):
            with self.subTest(symbol=symbol):
                targets = copy_targets(symbol)
                self.assertTrue(
                    len(targets) >= 2,
                    f"{symbol} 现在只有 {len(targets)} 个副本？"
                    " 若拆分回退成单模块，本 helper 就可以删了。",
                )
                for t in targets:
                    self.assertTrue(t.endswith("." + symbol))

    def test_copy_targets_matches_ast_scan(self):
        """运行时 introspection 与 AST 扫描必须对得上（副本表不过时）。"""
        checked = 0
        for f in sorted(PKG_DIR.glob("*.py")):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            names = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(node.name)
            for symbol in names:
                runtime = set(copy_targets(symbol))
                expected = _ast_holders(symbol)
                self.assertEqual(
                    runtime, expected,
                    f"{symbol}: 运行时副本 {sorted(runtime)} 与 AST {sorted(expected)} 不一致。"
                    " 说明有子模块还没被 import（introspection 只看已加载模块），"
                    " 或 __init__ 漏了 re-export。",
                )
                checked += 1
        self.assertGreater(checked, 20, "应至少核对 20 个顶层符号")

    def test_unknown_symbol_raises(self):
        """拼错的符号要响亮失败，而不是静默 no-op。"""
        with self.assertRaises(AttributeError):
            with patch_bf("_definitely_not_a_symbol"):
                pass

    def test_dotted_target_rejected(self):
        """带点的目标不归 patch_bf 管 —— 交给 unittest.mock.patch。"""
        with self.assertRaises(ValueError):
            patch_bf("orchestrator._wait_for_registration_state")


class SameMockAcrossCopiesTests(unittest.TestCase):
    def test_all_copies_share_one_mock(self):
        """所有副本装的是**同一个** mock 对象 —— 否则 call_args 只反映最后一次。"""
        symbol = "_wait_for_registration_state"
        targets = copy_targets(symbol)
        originals = {}
        import importlib

        for t in targets:
            mod_dotted = t[: -len(symbol) - 1]
            mod = importlib.import_module(mod_dotted)
            originals[t] = getattr(mod, symbol)

        with patch_bf(symbol, return_value="otp") as m:
            for t in targets:
                mod = importlib.import_module(t[: -len(symbol) - 1])
                self.assertIs(getattr(mod, symbol), m, f"{t} 没被装上同一个 mock")

        for t, orig in originals.items():
            mod = importlib.import_module(t[: -len(symbol) - 1])
            self.assertIs(getattr(mod, symbol), orig, f"{t} 退出后没还原")

    def test_call_records_are_merged(self):
        """跨模块的多次调用都记在同一个 mock 上。"""
        import importlib

        symbol = "_otp_fields"
        targets = copy_targets(symbol)
        with patch_bf(symbol, return_value=[]) as m:
            for t in targets:
                mod = importlib.import_module(t[: -len(symbol) - 1])
                getattr(mod, symbol)("page-a")
                getattr(mod, symbol)("page-b")
            self.assertEqual(m.call_count, 2 * len(targets))


class DecoratorOrderTests(unittest.TestCase):
    """注入顺序是本次真正的 bug —— 写反会整体错位一位且不报错。"""

    def test_inner_decorator_provides_first_argument(self):
        from unittest.mock import patch as mp

        @patch_bf("_fill_email", return_value=None)
        @patch_bf("_fill_otp", return_value=None)
        def probe(_inner, _outer):
            return _inner, _outer

        inner, outer = probe()
        # 最内层装饰器（最靠近函数的那个）对应第一个参数
        self.assertEqual(inner._mock_name, "_fill_otp")
        self.assertEqual(outer._mock_name, "_fill_email")

    def test_three_level_stack_keeps_order(self):
        """三层纯 patch_bf 堆叠：顺序仍是最内层 -> 第一个参数。"""
        @patch_bf("_fill_email", return_value=None)
        @patch_bf("_fill_otp", return_value=None)
        @patch_bf("_click_resend", return_value=None)
        def probe(a, b, c):
            return a, b, c

        a, b, c = probe()
        self.assertEqual([m._mock_name for m in (a, b, c)],
                         ["_click_resend", "_fill_otp", "_fill_email"])

    def test_mixing_with_mock_patch_keeps_order(self):
        """与 unittest.mock.patch 混用时顺序仍然正确，且不因属性缺失而炸。

        注意这个用例**不**覆盖 patch_bf 自己的顺序逻辑：最内层是 patch() 时，
        wrapper 由 patch() 创建并遍历 patchings，patch_bf 只是被 append 进去。
        它锁的是"混用不炸 + 最终顺序正确"；纯 patch_bf 堆叠的顺序由上面
        两个用例负责，变异验证里也只有它们会对"顺序反转"变异变红。
        """
        from unittest.mock import patch as mp

        @patch_bf("_fill_email", return_value=None)
        @mp("sms_tool.registration_drivers.browser_flow.orchestrator.time.sleep")
        def probe(_inner, _outer):
            return _inner, _outer

        inner, outer = probe()
        self.assertEqual(inner._mock_name, "sleep")
        self.assertEqual(outer._mock_name, "_fill_email")

    def test_context_manager_form_returns_the_mock(self):
        with patch_bf("_fill_email", return_value=None) as m:
            self.assertEqual(m._mock_name, "_fill_email")
            self.assertIsNone(m("page"))


if __name__ == "__main__":
    unittest.main()
