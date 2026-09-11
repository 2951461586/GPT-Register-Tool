"""扫描状态 / 失败分类的契约门禁（round3 P2-14）。

两条不变量：

1. **failure_class 覆盖**：``error_classification.classify_error`` 能产出的每个
   failure_class，``account_scan._SCAN_STATUS_BY_FAILURE_CLASS`` 要么显式映射，
   要么在 ``_FALLTHROUGH_CLASSES`` 里登记并写清理由。防止新增分类时静默掉进
   ``scan_failed``。

2. **扫描状态词 → C#**：``_SCAN_STATUS_BY_FAILURE_CLASS`` 的每个**值**，
   C# ``BackendResultInterpreter.ScanStatusLabel`` 都要认得。

   注意 C# 侧有**两个**状态解释器，消费的是不同字段：
   - ``AccountStatusInterpreter.DisplayAccountStatus`` —— 账号 status
     （由 ``store/normalize._status`` 产出，见 ``test_account_status_vocabulary.py``）
   - ``BackendResultInterpreter.ScanStatusLabel`` —— 扫描 status
     （由 ``account_scan._scan_failure_status`` 产出，本文件）
   两者都要覆盖；只查一个会漏掉一半漂移。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CLASSIFICATION = PROJECT_ROOT / "sms_tool" / "error_classification.py"
_REGISTRY = PROJECT_ROOT / "sms_tool" / "failure_registry.py"
_SCAN = PROJECT_ROOT / "sms_tool" / "accounts" / "account_scan.py"
_INTERPRETER = PROJECT_ROOT / "SmsWorkbench" / "BackendResultInterpreter.cs"

# 有意不作映射、直接 fallthrough 到 relogin_failed / scan_failed 的分类。
# 每一条都必须写清理由 —— 白名单是决策记录，不是垃圾桶。
_FALLTHROUGH_CLASSES = {
    # 账号本身的问题（掉号等），要靠 :481 的 token_probe 推断提升成 at_invalid，
    # 映射成独立状态反而会绕过那条提升路径。``account_scan.py:59-61`` 有说明。
    "account",
    # 无法归类。保持与 account 相同的 fallthrough 语义。
    "unknown",
    # 本地程序缺陷或静态配置错误不代表账号状态；保留扫描层的
    # relogin_failed/scan_failed fallthrough，避免伪造账号故障状态。
    "internal",
    "configuration",
}

_STATUS_WORD = re.compile(r"[a-z][a-z0-9_]*\Z")
_CS_SWITCH_ARM = re.compile(r'"([a-z0-9_]+)"\s*=>')


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _returned_strings(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out.add(node.value.value)
    return out


def failure_class_vocabulary(py_path: Path = _CLASSIFICATION, registry_path: Path = _REGISTRY) -> set[str]:
    """``classify_error`` 能返回的 failure_class 全集。

    2026-09-13 起分类词汇的单源在 ``failure_registry.FAILURE_CLASSES``，而
    ``classify_error`` 里的字面 Return 只剩 internal 的特殊 demotion 分支与
    "unknown"。全集 = 注册表 FailureClass(code=...) 关键字 ∪ classify_error
    的直返字面量。
    """
    vocab: set[str] = set()
    fn = _function_node(ast.parse(py_path.read_text(encoding="utf-8")), "classify_error")
    if fn is None:
        raise AssertionError(f"{py_path} 里找不到 classify_error —— 提取器必须立刻失败")
    vocab |= {word for word in _returned_strings(fn) if _STATUS_WORD.match(word)}
    tree = ast.parse(registry_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "FailureClass":
            code_value = None
            if node.args:
                first = node.args[0]
                code_value = first.value if isinstance(first, ast.Constant) else None
            for kw in node.keywords:
                if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                    code_value = kw.value.value
            if isinstance(code_value, str) and code_value:
                vocab.add(code_value)
    if not vocab:
        raise AssertionError("failure_class 全集提取为空 —— 提取器或注册表已腐烂")
    return vocab


def scan_status_map(py_path: Path = _SCAN) -> dict[str, str]:
    """``_SCAN_STATUS_BY_FAILURE_CLASS`` 的 {failure_class: scan_status}。"""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_SCAN_STATUS_BY_FAILURE_CLASS":
                    if not isinstance(node.value, ast.Dict):
                        raise AssertionError("_SCAN_STATUS_BY_FAILURE_CLASS 不是 dict 字面量")
                    out: dict[str, str] = {}
                    for key, value in zip(node.value.keys, node.value.values):
                        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                            out[str(key.value)] = str(value.value)
                    return out
    raise AssertionError(f"{py_path} 里找不到 _SCAN_STATUS_BY_FAILURE_CLASS")


def csharp_scan_status_labels(cs_text: str) -> set[str]:
    """``ScanStatusLabel`` 的 switch 分支键。"""
    start = cs_text.find("ScanStatusLabel")
    if start < 0:
        raise AssertionError("C# 里找不到 ScanStatusLabel —— 提取器必须立刻失败")
    # 只取该函数体，避免抓到别的 switch
    body = cs_text[start : cs_text.find("}", cs_text.find("switch", start))]
    return set(_CS_SWITCH_ARM.findall(body))


class FailureClassCoverage(unittest.TestCase):
    """failure_class 必须被显式映射，或登记为有意 fallthrough。"""

    def setUp(self):
        self.classes = failure_class_vocabulary()
        self.mapped = scan_status_map()

    def test_vocabularies_are_not_empty(self):
        """防假绿：任一提取器变空都会让下面的差集断言退化成恒真。"""
        self.assertTrue(self.classes, "failure_class 提取为空")
        self.assertIn("network", self.classes)
        self.assertTrue(self.mapped, "扫描状态映射表提取为空")

    def test_every_failure_class_is_mapped_or_registered(self):
        unmapped = sorted(self.classes - set(self.mapped) - _FALLTHROUGH_CLASSES)
        self.assertEqual(
            [],
            unmapped,
            "classify_error 会产出但扫描映射表没处理的 failure_class —— "
            "它们会静默掉进 scan_failed：\n" + "\n".join(f"  - {c}" for c in unmapped),
        )

    def test_fallthrough_list_has_no_dead_entries(self):
        """负向测试：白名单里的分类必须仍在产出，否则条目已腐烂。"""
        stale = sorted(_FALLTHROUGH_CLASSES - self.classes)
        self.assertEqual([], stale, f"_FALLTHROUGH_CLASSES 已不再产出，应删掉: {stale}")

    def test_mapping_keys_are_real_failure_classes(self):
        """负向测试：映射表的键不能是拼错/已弃用的 failure_class。"""
        bogus = sorted(set(self.mapped) - self.classes)
        self.assertEqual([], bogus, f"映射表里不是 classify_error 产出的键: {bogus}")

    def test_cancelled_is_not_collapsed_into_a_failure(self):
        """用户主动取消 ≠ 失败。

        ``CANCELLED_ERROR_MARKERS`` 字面就是 ``("registration_cancelled",
        "cancelled_by_user")``，且 ``registration_cancelled`` 同时被列为
        terminal（不可重试）。把它算成 scan_failed 会同时污染失败计数和
        ``account_scan.py:481`` 的 at_invalid 提升判定。
        """
        self.assertIn("cancelled", self.classes)
        self.assertNotEqual("scan_failed", self.mapped.get("cancelled"))
        self.assertIn("cancelled", self.mapped)


class ScanStatusCrossLanguage(unittest.TestCase):
    """扫描状态词必须被 C# ``ScanStatusLabel`` 认得。"""

    def setUp(self):
        self.statuses = set(scan_status_map().values())
        self.labels = csharp_scan_status_labels(_INTERPRETER.read_text(encoding="utf-8"))

    def test_extraction_is_not_empty(self):
        self.assertTrue(self.statuses, "扫描状态词提取为空")
        self.assertTrue(self.labels, "C# ScanStatusLabel 分支提取为空")
        self.assertIn("scan_failed", self.labels)

    def test_every_scan_status_has_a_csharp_label(self):
        missing = sorted(self.statuses - self.labels)
        self.assertEqual(
            [],
            missing,
            "Python 会产出但 C# ScanStatusLabel 不认得的扫描状态 —— UI 会显示原始英文：\n"
            + "\n".join(f"  - {m}" for m in missing),
        )

    def test_cancelled_status_is_labelled(self):
        """与上一条呼应：取消必须在 C# 侧也有标签，否则显示成裸 ``scan_cancelled``。"""
        self.assertIn("scan_cancelled", self.statuses)
        self.assertIn("scan_cancelled", self.labels)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
