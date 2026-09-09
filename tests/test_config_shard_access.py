"""配置分片访问门禁。

根 ``config.json`` 只是**历史迁移源**：一旦 ``runtime/proxy.json`` /
``runtime.json`` / ``payment.json`` 三个分片存在，``load_merged_config``
就**不再读它**（``config.py:164``）。

因此任何按字面量路径直读 ``config.json`` 的写法都有两个独立缺陷：

1. **读到过时数据** —— 分片改了它不跟着变。实证：``proxy.registration``
   在分片里是字符串、在根 ``config.json`` 里是 96 项列表；而
   ``probe_account_liveness.load_config_proxy`` 会 ``str(proxy.get(key))``，
   于是把整个列表的字符串形式当成代理 URL 返回。
2. **依赖 cwd** —— 相对路径在非项目根目录下静默返回 ``{}``。

本模块的门禁不 import 被检查的脚本（避免顶层副作用），全部走
AST / 正则静态分析。
"""

from __future__ import annotations

import ast
import os
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 允许直读 config.json 的地方（都**不是**项目根那份）:
#   - sms_tool/config.py       : 分片加载器本体，它负责把 root 迁移进分片
#   - sms_tool/config_usage.py : 配置使用面盘点工具
#   - services/                : 独立服务，有自己的 config.json
#   - tests/                   : 在 tmp 目录里造临时文件
_ALLOWED_PREFIXES = (
    "sms_tool/config.py",
    "sms_tool/config_usage.py",
    "services/",
    "tests/",
)

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".workbuddy-ai",
        "__pycache__",
        "node_modules",
        "dist",
        "runtime",
        "logs",
        "sessions",
    }
)

# 语句级：同一行里同时出现「读取动作」和「config.json 字面量」。
# 变量路径（如 ``_load_json(path)``）不匹配，因此不会误伤分片加载器。
_READ_RE = re.compile(r"(open\s*\(|\bPath\s*\(|read_text\s*\(|json\.load\s*\()")
_LITERAL_RE = re.compile(r"""["'][^"']*config\.json["']""")


def _iter_python_files(root: Path):
    """Yield ``(rel_posix, abs_path)`` for every ``.py`` under ``root``."""
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            full = Path(base) / name
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - 防御性
                continue
            yield rel, full


def find_direct_config_reads(root: Path) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, line)`` for every un-allowlisted direct read."""
    hits: list[tuple[str, int, str]] = []
    for rel, full in _iter_python_files(root):
        if rel.startswith(_ALLOWED_PREFIXES):
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 读不动就跳过
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _LITERAL_RE.search(line) and _READ_RE.search(line):
                hits.append((rel, lineno, line.strip()))
    return hits


def module_level_names(path: Path) -> set[str]:
    """模块顶层赋值的名字集合（不含函数体内的局部名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class NoDirectConfigJsonReads(unittest.TestCase):
    """除白名单外，任何地方都不许按字面量路径直读根 config.json。"""

    def test_no_direct_reads_outside_the_allowlist(self):
        hits = find_direct_config_reads(PROJECT_ROOT)
        self.assertEqual(
            [],
            hits,
            "下列位置绕过分片直读 config.json，必须改用 load_merged_config()：\n"
            + "\n".join(f"  {p}:{n}: {line}" for p, n, line in hits),
        )

    def test_scanner_actually_matches_a_direct_read(self):
        """负向测试：扫描器本身不能是瞎的。

        在一个临时根里放入直读写法，必须被抓出来 —— 否则上面那条断言
        会因为「扫描器永远返回空」而假绿。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "bad.py").write_text(
                'cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))\n',
                encoding="utf-8",
            )
            hits = find_direct_config_reads(root)
            self.assertEqual(1, len(hits), f"扫描器没抓到明显的直读: {hits}")
            self.assertIn("bad.py", hits[0][0])

    def test_scanner_honours_the_allowlist(self):
        """负向测试：白名单内的路径必须被跳过（否则扫描器恒真）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "services").mkdir()
            (root / "services" / "app.py").write_text(
                'CONFIG_PATH = Path(BASE_DIR / "config.json")\n',
                encoding="utf-8",
            )
            self.assertEqual([], find_direct_config_reads(root))


class CanonicalConfigPathIsReal(unittest.TestCase):
    """``DEFAULT_CONFIG_PATH`` 必须指向一个真实存在的文件。

    ``paypal_link/gen_link.py`` 位于 ``sms_tool/paypal_link/``，项目根要上
    **两层**。曾经只上了一层，指向 ``sms_tool/config.json`` —— 一个被
    .gitignore 排除的遗留影子文件。因为生产调用都传这个常量本身（哨兵
    比较恒真），错误的取值一直没暴露。
    """

    def test_gen_link_canonical_path_points_at_the_project_root(self):
        from sms_tool.paypal_link import gen_link

        path = Path(gen_link.DEFAULT_CONFIG_PATH)
        self.assertEqual(
            "config.json",
            path.name,
            f"DEFAULT_CONFIG_PATH 文件名不对: {path}",
        )
        self.assertEqual(
            PROJECT_ROOT / "config.json",
            path.resolve(),
            f"DEFAULT_CONFIG_PATH 应指向项目根: {path}",
        )

    def test_gen_link_canonical_path_exists(self):
        """哨兵值指向的文件必须真实存在，否则一旦非哨兵路径传入就静默 {}。"""
        from sms_tool.paypal_link import gen_link

        self.assertTrue(
            os.path.exists(gen_link.DEFAULT_CONFIG_PATH),
            f"DEFAULT_CONFIG_PATH 指向不存在的文件: {gen_link.DEFAULT_CONFIG_PATH}",
        )

    def test_sibling_modules_share_the_same_root(self):
        """同层/上层的兄弟模块算出来的项目根必须一致。"""
        from sms_tool import omakse_client, upi_link
        from sms_tool.paypal_link import gen_link

        roots = {
            Path(omakse_client.DEFAULT_CONFIG_PATH).resolve().parent,
            Path(upi_link.DEFAULT_CONFIG_PATH).resolve().parent,
            Path(gen_link.DEFAULT_CONFIG_PATH).resolve().parent,
        }
        self.assertEqual(1, len(roots), f"三个模块算出的项目根不一致: {roots}")


class DeadConfigConstantsStayDead(unittest.TestCase):
    """``paypal_protocol.DEFAULT_CONFIG_PATH`` 是零引用死常量，删了不许复活。"""

    def test_paypal_protocol_has_no_config_path_constants(self):
        path = PROJECT_ROOT / "sms_tool" / "paypal_protocol.py"
        names = module_level_names(path)
        self.assertEqual(
            set(),
            names & {"DEFAULT_CONFIG_PATH", "SCRIPT_DIR", "PROJECT_ROOT"},
            "paypal_protocol 走 .config.load_merged_config，不需要自己的路径常量",
        )


class ShardLoaderIsWired(unittest.TestCase):
    """canonical 路径必须经分片加载器，不能退化成裸 json.load。"""

    def test_load_json_routes_the_canonical_path_through_the_shards(self):
        from unittest.mock import patch

        from sms_tool.paypal_link import gen_link

        with patch("sms_tool.config.load_merged_config", return_value={"merged": True}) as loader:
            out = gen_link._load_json(gen_link.DEFAULT_CONFIG_PATH)
        loader.assert_called_once_with()
        self.assertEqual({"merged": True}, out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
