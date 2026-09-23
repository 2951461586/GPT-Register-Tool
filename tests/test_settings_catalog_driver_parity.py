"""``SmsWorkbench/SettingsCatalog.cs`` 的驱动下拉框必须与 Python 注册表一致。

为什么要有这个文件
------------------
2026-09-22 量测「新增供应商改动成本」（对标 `cxqc168-wq/gpt-register-pro` 的
「1 provider file + 1 registry entry + 1 UI card」）时发现：

* **Python 侧已经全部派生** —— CLI 的 ``--registration-driver`` choices 走
  ``driver_choices()``，配置校验走 ``KNOWN_DRIVER_ALIASES`` /
  ``BROWSER_REGISTRATION_DRIVERS``，连会话工厂表都有
  ``external_sessions/__init__.py:22`` 的 ``assert`` 钉在注册表上。
* 🔴 **C# 侧没有** —— ``SettingsCatalog.cs`` 把驱动枚举手写了第二份
  （``"protocol", "playwright", "roxy", "cloak", "camoufox"``），而
  ``scripts/config_key_baseline.json`` 棘轮只管**键名**、不管**枚举选项值**，
  所以这份副本此前不受任何守卫。

两个方向的漂移后果不同，且都不轻：

* Python 加了驱动、C# 没加 ⇒ 桌面端下拉框里**选不到**该驱动（功能缺失，看得见）；
* C# 留了 Python 已删的驱动 ⇒ 桌面端**能选中**，但后端 ``create_browser_session``
  抛 ``unsupported_registration_driver``（配置界面在撒谎，且只有运行时才发现）。
"""

import re
import unittest
from pathlib import Path

from sms_tool.registration_drivers.base import DRIVERS

CATALOG = Path(__file__).resolve().parents[1] / "SmsWorkbench" / "SettingsCatalog.cs"

#: 驱动下拉框那一行。用 ``Options`` 而不是裸字符串，避免匹配到别处的字面量。
ANCHOR = 'Options("registration_driver"'


def _skip_string(text, index):
    """Return the index just past the string literal starting at ``index``."""
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return index


def _call_slice(text, anchor):
    """Return the full ``Options(...)`` call text starting at ``anchor``.

    用括号配平而不是正则：``registration_driver`` 的选项列表跨了行
    （选项在续行上），单行正则取不全，会静默少读几个驱动。
    """
    start = text.index(anchor)
    depth = 0
    index = text.index("(", start)
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
        index += 1
    raise AssertionError("unbalanced parentheses in %r" % anchor)


def catalog_driver_options(text=None):
    """驱动下拉框里的选项，按出现顺序。"""
    if text is None:
        text = CATALOG.read_text(encoding="utf-8")
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', _call_slice(text, ANCHOR))
    # Options(key, label, path, fallback, params options) —— 前四个是元数据
    return literals[4:]


class SettingsCatalogDriverParityTests(unittest.TestCase):
    def test_the_catalog_exists_and_declares_the_driver_dropdown(self):
        """锚点找不到时必须炸 —— 否则改名会让下面几条测试变成空转。"""
        text = CATALOG.read_text(encoding="utf-8")
        self.assertEqual(text.count(ANCHOR), 1)
        self.assertTrue(catalog_driver_options(text))

    def test_the_dropdown_offers_exactly_the_registered_drivers(self):
        """双向相等：少一个（选不到）和多一个（选了必失败）都是缺陷。"""
        options = set(catalog_driver_options())
        registered = set(DRIVERS)
        self.assertEqual(
            options - registered, set(),
            "桌面端下拉框提供了后端不认的驱动：%s" % sorted(options - registered))
        self.assertEqual(
            registered - options, set(),
            "桌面端下拉框缺少已注册的驱动：%s" % sorted(registered - options))

    def test_the_fallback_is_a_registered_driver(self):
        """``Options`` 的第四个参数是默认值，写错会让新配置直接落在非法驱动上。"""
        literals = re.findall(r'"((?:[^"\\]|\\.)*)"', _call_slice(
            CATALOG.read_text(encoding="utf-8"), ANCHOR))
        self.assertIn(literals[3], DRIVERS)
        self.assertIn(literals[3], catalog_driver_options())

    def test_the_extractor_is_not_trivially_empty(self):
        """配平逻辑写坏会返回空列表，而空列表会让上面几条断言以一种假方式通过。"""
        options = catalog_driver_options()
        self.assertGreaterEqual(len(options), 2)
        self.assertEqual(len(options), len(set(options)), "选项有重复：%s" % options)


class ExtractorTests(unittest.TestCase):
    """抽取器本身的行为 —— 它读的是源码文本，解析错误会静默改变结论。"""

    def test_options_spanning_lines_are_all_collected(self):
        text = (
            'Options("registration_driver", "L", "p", "a",\n'
            '    "a", "b", "c"),\n'
        )
        self.assertEqual(catalog_driver_options(text), ["a", "b", "c"])

    def test_an_escaped_quote_does_not_end_the_string(self):
        text = 'Options("registration_driver", "L\\"x", "p", "a", "a", "b"),\n'
        self.assertEqual(catalog_driver_options(text), ["a", "b"])

    def test_an_unbalanced_call_raises_instead_of_returning_garbage(self):
        with self.assertRaises(AssertionError):
            catalog_driver_options('Options("registration_driver", "L", "p", "a", "a"\n')


if __name__ == "__main__":
    unittest.main()
