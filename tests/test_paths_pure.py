"""``sms_tool/paths.py`` 的行为测试（零覆盖 → 全覆盖 + 变异验证）。

**为什么这个 27 行的模块值得单独立一份测试**：它是整个测试隔离层的地基。

20 个模块用的是 ``from .paths import runtime_file`` —— 按名绑定了一份副本，
所以 **patch ``paths.runtime_file`` 对它们一个都不生效**（绑定复制陷阱）。
``isolated_runtime`` 之所以能靠一句 ``monkeypatch.setattr(paths, "runtime_dir", ...)``
把整棵 runtime 树挪走，唯一的理由是 ``runtime_file`` 内部**通过模块全局名查找**
``runtime_dir``。哪天有人把它改成构造期缓存、或者内联展开，
**沙箱会静默失效，全量测试重新写进真实的 ``runtime/``（含 accounts.sqlite3）**。
``test_runtime_file_resolves_its_directory_through_the_module_global`` 就是钉这个的。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from sms_tool import paths

# 故意按名绑定：这就是 20 个生产模块的用法，patch ``paths.runtime_dir`` 之后
# 这两个副本**不应该**跟着变（绑定复制），但 ``runtime_file`` 内部的调用会跟着变。
from sms_tool.paths import runtime_dir as bound_runtime_dir
from sms_tool.paths import runtime_file as bound_runtime_file


class ProjectRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_it_is_the_repository_root(self):
        self.assertEqual(paths.PROJECT_ROOT,
                         Path(paths.__file__).resolve().parent.parent)

    def test_it_is_absolute_and_free_of_dot_components(self):
        self.assertTrue(paths.PROJECT_ROOT.is_absolute())
        self.assertNotIn("..", paths.PROJECT_ROOT.parts)
        self.assertNotIn(".", paths.PROJECT_ROOT.parts)

    def test_it_actually_holds_the_package(self):
        self.assertTrue((paths.PROJECT_ROOT / "sms_tool").is_dir())


class ProjectPathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)

    def test_a_relative_value_resolves_against_the_project_root_not_the_cwd(self):
        """🔴 关键契约：相对路径的基准是 PROJECT_ROOT，**不是进程 cwd**。
        pytest 恰好是从仓库根跑的，所以不 chdir 的话这条断言区分不出两种实现。"""
        os.chdir(self.tmp_path)
        self.assertEqual(paths.project_path("sessions"), paths.PROJECT_ROOT / "sessions")

    def test_an_absolute_value_is_returned_unchanged(self):
        target = self.tmp_path / "data"
        self.assertEqual(paths.project_path(str(target)), target)
        self.assertEqual(paths.project_path(target), target)

    def test_the_default_is_used_and_is_also_root_relative(self):
        os.chdir(self.tmp_path)
        self.assertEqual(paths.project_path(None, "runtime"),
                         paths.PROJECT_ROOT / "runtime")

    def test_the_built_in_default_is_the_project_itself(self):
        """``default="."`` → 解析成 ``PROJECT_ROOT / "."``，点和点都在。"""
        self.assertEqual(paths.project_path(None), paths.PROJECT_ROOT / ".")

    def test_a_falsy_value_falls_back_to_the_default(self):
        """``str(value or default)`` 里的 ``or`` **是有语义的**（和"假值归一化纯装饰"
        那类不同）：去掉它，``0`` 会变成 ``PROJECT_ROOT / "0"``。"""
        for label, value in {"none": None, "empty": "", "zero": 0,
                             "false": False, "empty list": []}.items():
            with self.subTest(label=label):
                self.assertEqual(paths.project_path(value, "runtime"),
                                 paths.PROJECT_ROOT / "runtime")

    def test_a_blank_value_falls_back_to_the_default_after_stripping(self):
        """第二层防御：``.strip()`` 之后再用一次 ``or default``。
        ``"   "`` 是真值，第一层 ``or`` 拦不住，只有第二层拦得住。"""
        for value in ("   ", "\t\n ", "  \r\n  "):
            with self.subTest(value=repr(value)):
                self.assertEqual(paths.project_path(value, "runtime"),
                                 paths.PROJECT_ROOT / "runtime")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(paths.project_path("  sessions  "),
                         paths.PROJECT_ROOT / "sessions")

    def test_a_user_home_prefix_is_expanded(self):
        self.assertEqual(paths.project_path("~/sessions"), Path.home() / "sessions")

    def test_non_string_values_are_stringified(self):
        for label, (value, expected) in {
            "path": (Path("sessions"), paths.PROJECT_ROOT / "sessions"),
            "int": (123, paths.PROJECT_ROOT / "123"),
            "true": (True, paths.PROJECT_ROOT / "True"),
        }.items():
            with self.subTest(label=label):
                self.assertEqual(paths.project_path(value), expected)

    def test_dot_and_dotdot_components_are_preserved_not_resolved(self):
        """⚠️ 没有 ``.resolve()`` —— 结果是拼接出来的，不是规整过的。
        钉住它：谁加 ``.resolve()`` 谁改变返回值（且会去碰真实文件系统）。"""
        self.assertEqual(paths.project_path("a/../b"),
                         paths.PROJECT_ROOT / "a" / ".." / "b")
        self.assertNotEqual(paths.project_path("a/../b"),
                            paths.PROJECT_ROOT / "b")

    def test_the_result_is_always_a_path(self):
        for value in (None, "", "x", 0, Path("y")):
            with self.subTest(value=repr(value)):
                self.assertIsInstance(paths.project_path(value), Path)


class ConfigDirectoryTest(unittest.TestCase):
    """``output_dir`` / ``runtime_dir``：从配置桶里取目录名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_runtime_reads_the_runtime_bucket(self):
        target = self.tmp_path / "rt"
        self.assertEqual(bound_runtime_dir({"runtime": {"directory": str(target)}}),
                         target)

    def test_output_reads_the_output_bucket(self):
        target = self.tmp_path / "out"
        self.assertEqual(paths.output_dir({"output": {"directory": str(target)}}),
                         target)

    def test_the_two_buckets_do_not_cross_read(self):
        out, rt = self.tmp_path / "out", self.tmp_path / "rt"
        cfg = {"output": {"directory": str(out)}, "runtime": {"directory": str(rt)}}
        self.assertEqual(paths.output_dir(cfg), out)
        self.assertEqual(bound_runtime_dir(cfg), rt)

    def test_a_unix_style_absolute_path_is_not_absolute_on_windows(self):
        """🔴 可移植性陷阱，钉住别改。

        ``Path("/srv/out").is_absolute()`` 在 Windows 上是 **False**（单斜杠开头
        算"驱动器相对"，不是绝对路径），所以 ``project_path`` 会把它当相对路径
        去和 PROJECT_ROOT 拼；而 ``WindowsPath`` 的 ``/`` 拼接又会把带前导斜杠的
        那一截**重置到当前盘符根目录** —— 结果项目目录整段丢失：
        ``PROJECT_ROOT / "/srv/out"`` == ``F:/srv/out``。
        不报错，只是静默指到别的地方去。配置里写 Unix 路径就会踩到。
        """
        self.assertFalse(Path("/srv/out").is_absolute())
        self.assertEqual(paths.PROJECT_ROOT / "/srv/out", Path("F:/srv/out"))
        self.assertEqual(paths.project_path("/srv/out"), Path("F:/srv/out"))
        self.assertNotIn("GPT-Register-Tool", str(paths.project_path("/srv/out")))

    def test_a_missing_bucket_falls_back_to_the_default(self):
        self.assertEqual(bound_runtime_dir({}), paths.PROJECT_ROOT / "runtime")
        self.assertEqual(paths.output_dir({}), paths.PROJECT_ROOT / "sessions")

    def test_a_none_bucket_falls_back_to_the_default(self):
        """``or {}`` 这一层：桶可以是 JSON 里的显式 ``null``。"""
        self.assertEqual(bound_runtime_dir({"runtime": None}),
                         paths.PROJECT_ROOT / "runtime")
        self.assertEqual(paths.output_dir({"output": None}),
                         paths.PROJECT_ROOT / "sessions")

    def test_a_missing_directory_key_falls_back_to_the_default(self):
        self.assertEqual(bound_runtime_dir({"runtime": {}}),
                         paths.PROJECT_ROOT / "runtime")

    def test_a_null_or_blank_directory_falls_back_to_the_default(self):
        for value in (None, "", "   "):
            with self.subTest(value=repr(value)):
                self.assertEqual(bound_runtime_dir({"runtime": {"directory": value}}),
                                 paths.PROJECT_ROOT / "runtime")

    def test_a_non_dict_bucket_raises(self):
        """``(cfg.get("runtime") or {}).get(...)`` —— ``or {}`` 只挡假值，
        挡不住真值里的非 dict。字符串/数字进来就是 ``AttributeError``。"""
        for value in ("runtime", 42, [1]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(AttributeError):
                    bound_runtime_dir({"runtime": value})

    def test_a_config_without_get_raises(self):
        with self.assertRaises(AttributeError):
            bound_runtime_dir(None)


class RuntimeFileTest(unittest.TestCase):
    """这几个用例要的是**真实**的目录解析，所以 opt out 掉 autouse 的
    ``isolated_runtime``（它会把 ``paths.runtime_dir`` 顶成沙箱 —— 那是隔离层
    该干的事，但会盖掉我这批断言）。磁盘写入全部落在 tmp 目录里。"""

    pytestmark = pytest.mark.allow_real_runtime

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_it_returns_directory_over_filename(self):
        cfg = {"runtime": {"directory": str(self.tmp_path / "rt")}}
        self.assertEqual(bound_runtime_file(cfg, "x.json"),
                         self.tmp_path / "rt" / "x.json")

    def test_it_creates_the_directory_as_a_side_effect(self):
        """⚠️ 这个函数**会建目录**，不只是拼路径。调用它就有磁盘副作用。"""
        target = self.tmp_path / "rt" / "nested"
        self.assertFalse(target.exists())
        cfg = {"runtime": {"directory": str(target)}}
        bound_runtime_file(cfg, "x.json")
        self.assertTrue(target.is_dir())

    def test_it_creates_missing_parents(self):
        cfg = {"runtime": {"directory": str(self.tmp_path / "a" / "b" / "c")}}
        bound_runtime_file(cfg, "x.json")
        self.assertTrue((self.tmp_path / "a" / "b" / "c").is_dir())

    def test_it_is_idempotent_and_tolerates_an_existing_directory(self):
        cfg = {"runtime": {"directory": str(self.tmp_path / "rt")}}
        bound_runtime_file(cfg, "a.json")
        bound_runtime_file(cfg, "b.json")
        self.assertEqual(sorted(p.name for p in (self.tmp_path / "rt").iterdir()),
                         [])

    def test_it_does_not_create_the_file_itself(self):
        cfg = {"runtime": {"directory": str(self.tmp_path / "rt")}}
        target = bound_runtime_file(cfg, "x.json")
        self.assertFalse(target.exists())
        self.assertTrue(target.parent.is_dir())

    def test_a_nested_filename_does_not_get_its_parent_created(self):
        """只建目录桶本身，不管文件名里带的子目录。"""
        cfg = {"runtime": {"directory": str(self.tmp_path / "rt")}}
        target = bound_runtime_file(cfg, "sub/x.json")
        self.assertEqual(target, self.tmp_path / "rt" / "sub" / "x.json")
        self.assertFalse((self.tmp_path / "rt" / "sub").exists())

    def test_runtime_file_resolves_its_directory_through_the_module_global(self):
        """🔴 整个测试隔离层的承重墙。

        20 个生产模块写的是 ``from .paths import runtime_file``，所以我 patch
        ``paths.runtime_file`` 对它们毫无作用；``isolated_runtime`` 靠 patch
        ``paths.runtime_dir`` 生效，**前提是 ``runtime_file`` 在调用时通过模块
        全局名去找 ``runtime_dir``**，而不是用了某个构造期快照或内联展开。
        本文件顶部的 ``bound_runtime_file`` 就是"按名绑定"的副本 ——
        它必须照样跟着 patch 走。"""
        sentinel = self.tmp_path / "sandbox"
        original = paths.runtime_dir
        paths.runtime_dir = lambda cfg=None: sentinel
        try:
            # patch 只换掉了模块属性，按名绑定的副本原地不动 ——
            # 但 runtime_file 内部走的是模块全局查找，所以它必须跟着走。
            self.assertIsNot(paths.runtime_dir, bound_runtime_dir)
            self.assertEqual(bound_runtime_file({}, "x.json"), sentinel / "x.json")
            self.assertTrue(sentinel.is_dir(), "patch 之后副作用也要落到新目录")
        finally:
            paths.runtime_dir = original

    def test_patching_runtime_file_itself_does_not_redirect_the_bound_copies(self):
        """对照上一条：patch ``runtime_file`` **不**能重定向已经按名绑定的调用方。
        这就是为什么隔离层必须打在 ``runtime_dir`` 上。"""
        sentinel = self.tmp_path / "nope"
        original = paths.runtime_file
        paths.runtime_file = lambda cfg, name: sentinel / name
        try:
            cfg = {"runtime": {"directory": str(self.tmp_path / "rt")}}
            self.assertNotEqual(bound_runtime_file(cfg, "x.json"), sentinel / "x.json")
        finally:
            paths.runtime_file = original


if __name__ == "__main__":
    unittest.main()
