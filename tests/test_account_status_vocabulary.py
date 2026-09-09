"""账号状态词表：Python 真源 ↔ C# 消费方（round3 P2-13）。

**契约是单向的**：``sms_tool/store/normalize._status`` 能产出的状态词，
C# ``AccountStatusInterpreter.DisplayAccountStatus`` 必须认得。反过来不成立
—— C# 还有 ``paypalStatus`` / ``refreshTokenStatus`` 等本地词。

为什么需要这条门禁：``_status`` 的失败词汇（``network_failed`` /
``mailbox_failed`` / ``auth_state_failed`` / ``rate_limited``）是后来加的，
C# 侧一度**一个都不认得**。后果不是显示成空白，而是掉进
``if (hasRt && access.Length > 0) return "已注册";`` —— 网络抖动或限流的账号
**照样有 refresh token 和 access token**，于是被显示成健康账号。
这类漂移完全静默，只能靠门禁拦。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_NORMALIZE = PROJECT_ROOT / "sms_tool" / "store" / "normalize.py"
_INTERPRETER = PROJECT_ROOT / "SmsWorkbench" / "AccountStatusInterpreter.cs"

# Python 会产出、但 C# 不需要显式认得的词 —— 每个都要写清理由。
_CS_EXEMPT = {
    # 落盘的**历史拼写错误**（``normalize.py:316`` 注释明说 "it looks like a
    # typo but it is live data"）。``_status`` 只把它当**输入**并归一化成
    # ``account_deactivated``，从不产出它。
    "account_deatived",
    # 中间态。C# 兜底 ``return access.Length > 0 ? "已注册" : "待处理";``
    # 对它的处理是正确的（无 access 时正是"待处理"）。
    "pending",
    # 有 access token 且无失败的终态，C# 的 ``hasRt && access.Length > 0``
    # 兜底本身就返回"已注册"，语义一致，无需显式分支。
    "registered",
}

_STATUS_WORD = re.compile(r"[a-z][a-z0-9_]*\Z")
_CS_STATUS_EQUALS = re.compile(r'status\.Equals\("([a-z0-9_]+)"')


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def python_status_vocabulary(py_path: Path = _NORMALIZE) -> set[str]:
    """``_status`` 能产出的状态词。

    取两类：① ``return "literal"`` 的常量；② ``explicit in {...}: return explicit``
    里的集合成员（``explicit`` 直接透传，那些字面量就是产出）。
    """
    fn = _function_node(ast.parse(py_path.read_text(encoding="utf-8")), "_status")
    if fn is None:
        raise AssertionError(f"{py_path} 里找不到 _status —— 提取器必须立刻失败，不能静默返回空")

    produced: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                produced.add(value.value)
            # ``return explicit`` —— explicit 来自 status 字段，透传。
            elif isinstance(value, ast.Name) and value.id == "explicit":
                continue
        # ``if explicit in {"k12_joined", ...}:``
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = node.left
            if isinstance(left, ast.Name) and left.id == "explicit" and isinstance(node.ops[0], ast.In):
                comparator = node.comparators[0]
                if isinstance(comparator, (ast.Set, ast.List, ast.Tuple)):
                    for elt in comparator.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            produced.add(elt.value)
    return {word for word in produced if _STATUS_WORD.match(word)}


def csharp_recognised_statuses(cs_text: str) -> set[str]:
    """``DisplayAccountStatus`` 里针对 **status 参数** 的等值判断。

    只匹配小写开头的 ``status.Equals(`` —— ``paypalStatus.Equals`` /
    ``refreshTokenStatus.Equals`` 的首字母是大写，不会被误抓（它们是别的参数）。
    """
    return set(_CS_STATUS_EQUALS.findall(cs_text))


class StatusVocabularyCrossLanguage(unittest.TestCase):
    def setUp(self):
        self.vocab = python_status_vocabulary()
        self.known = csharp_recognised_statuses(_INTERPRETER.read_text(encoding="utf-8"))

    def test_python_vocabulary_is_not_empty(self):
        """防假绿：提取器静默变空会让下面的比较全部退化成空集相等。"""
        self.assertTrue(self.vocab, "Python 状态词提取为空 —— 提取器坏了")
        self.assertIn("network_failed", self.vocab)

    def test_csharp_vocabulary_is_not_empty(self):
        self.assertTrue(self.known, "C# 状态词提取为空 —— 提取器坏了")
        self.assertIn("account_deactivated", self.known)

    def test_every_python_status_is_recognised_by_csharp(self):
        missing = sorted(self.vocab - self.known - _CS_EXEMPT)
        self.assertEqual(
            [],
            missing,
            "Python 会产出但 C# 不认得的账号状态 —— 这些会掉进 "
            '``if (hasRt && access.Length > 0) return "已注册";`` 被误报成健康账号：\n'
            + "\n".join(f"  - {m}" for m in missing),
        )

    def test_exempt_list_has_no_dead_entries(self):
        """负向测试：白名单里的词必须**真的**还在 Python 侧，否则条目已经腐烂。"""
        stale = sorted(_CS_EXEMPT - self.vocab)
        self.assertEqual([], stale, f"_CS_EXEMPT 里的词已不再产出，应删掉: {stale}")

    def test_failure_vocabulary_is_covered(self):
        """P1-5 引入的失败词汇是这次漂移的主角，单独断言一次。"""
        for word in ("network_failed", "mailbox_failed", "auth_state_failed", "rate_limited"):
            self.assertIn(word, self.vocab, f"{word} 不在 Python 产出里了？")
            self.assertIn(word, self.known, f"C# 不认得 {word} —— 会误报成「已注册」")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
