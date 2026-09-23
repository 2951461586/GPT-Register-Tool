"""桌面端的接码供应商表必须与 Python 注册表逐项一致。

为什么要有这个文件
------------------
2026-09-22 移除静态号池模式、改为注册表驱动供应商时，``SmsWorkbench`` 侧出现了
**第三份手写副本**：``SmsProviderCatalog.cs`` 的供应商表（key / label / 默认端点 /
API Key 环境变量名），以及 ``SettingsCatalog.cs`` 的下拉框选项。

这和 ``SettingsCatalog.cs:56`` 的驱动枚举是同一类缺陷 —— **两侧之间没有编译器**，
所以任何一侧单独改动都会静默漂移。后果按方向不同：

* Python 加了供应商、C# 没加 ⇒ 桌面端**选不到**（功能缺失，看得见）；
* C# 留了 Python 已删的供应商 ⇒ 桌面端**能选中**，后端 ``unsupported SMS provider``
  （配置界面在撒谎，且只有运行时才发现）；
* 🔴 **默认端点漂移最隐蔽** —— 下拉框照常工作、余额也能读，但请求打到了**另一家的
  主机**。上游换了域名而 C# 没跟着改时就是这样，两侧都不报错。

所以这里做**双向**相等，并且把端点与环境变量名也逐项钉住。
"""

import re
import unittest
from pathlib import Path

from sms_tool import sms_providers

# 抽取器直接复用驱动一致性测试的那一份：它用**括号配平**而不是正则，
# 因为 ``Options(...)`` 的选项列表跨行，单行正则取不全、会静默少读几个选项。
from test_settings_catalog_driver_parity import _call_slice

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_CATALOG = REPO_ROOT / "SmsWorkbench" / "SmsProviderCatalog.cs"
SETTINGS_CATALOG = REPO_ROOT / "SmsWorkbench" / "SettingsCatalog.cs"
DIALOG = REPO_ROOT / "SmsWorkbench" / "MainWindow.SmsProvider.cs"

#: C# 供应商表的行锚点。用 ``new SmsProvider(`` 而不是裸字符串。
ROW_PATTERN = re.compile(
    r'new SmsProvider\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')

#: 下拉框那一行。
DROPDOWN_ANCHOR = 'Options("phone_provider"'
CONST_PATTERN = re.compile(r'DefaultPhoneProvider\s*=\s*"([^"]+)"')

#: 密钥形态：>=24 位连续 token 字符。与 ``sms_providers`` 自己的凭据守卫同一判据。
_CREDENTIAL = re.compile(r"[A-Za-z0-9_\-]{24,}")


def csharp_providers(text=None):
    """``SmsProviderCatalog.cs`` 里的 ``(key, label, endpoint, env)`` 四元组。"""
    if text is None:
        text = PROVIDER_CATALOG.read_text(encoding="utf-8")
    return ROW_PATTERN.findall(text)


def dropdown_options(text=None):
    """``SettingsCatalog.cs`` 里供应商下拉框的选项值。

    第四个实参是 ``DefaultPhoneProvider`` **标识符**而不是字面量（这样默认值只有
    一份），所以 ``phone_reuse.source`` 之后剩下的字面量就是全部选项。
    """
    if text is None:
        text = SETTINGS_CATALOG.read_text(encoding="utf-8")
    call = _call_slice(text, DROPDOWN_ANCHOR)
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', call)
    if len(literals) < 3 or literals[2] != "phone_reuse.source":
        raise AssertionError("unexpected phone_provider Options() shape: %r" % call)
    return literals[3:]


def default_provider_constant(text=None):
    if text is None:
        text = SETTINGS_CATALOG.read_text(encoding="utf-8")
    matches = CONST_PATTERN.findall(text)
    if len(matches) != 1:
        raise AssertionError("expected exactly one DefaultPhoneProvider const, got %r" % matches)
    return matches[0]


#: ``Options(key, label, path, <fallback>, ...)`` —— 只取第四个实参的原文。
_FALLBACK_PATTERN = re.compile(
    r'Options\(\s*"phone_provider"\s*,\s*"(?:[^"\\]|\\.)*"\s*,\s*"(?:[^"\\]|\\.)*"\s*,\s*([^,\n]+)')


def dropdown_fallback_argument(text=None):
    """下拉框的默认值实参原文（标识符或字面量）。"""
    if text is None:
        text = SETTINGS_CATALOG.read_text(encoding="utf-8")
    match = _FALLBACK_PATTERN.search(text)
    if match is None:
        raise AssertionError("could not locate the phone_provider fallback argument")
    return match.group(1).strip()


class ExtractorTests(unittest.TestCase):
    """抽取器本身 —— 它读源码文本，解析失败会静默改变结论。"""

    def test_the_three_row_parses(self):
        rows = csharp_providers()
        self.assertEqual(len(rows), 3, rows)

    def test_a_reformatted_row_still_parses(self):
        text = 'new SmsProvider(\n    "k",\n    "L",\n    "https://h/x",\n    "K_ENV")\n'
        self.assertEqual(csharp_providers(text), [("k", "L", "https://h/x", "K_ENV")])

    def test_a_row_with_a_missing_field_is_not_silently_skipped(self):
        """少一个字段时正则不匹配 ⇒ 行数变少 ⇒ 上面的 ``len == 3`` 会红。
        这条钉住「不匹配就是缺陷」，而不是「匹配到几个算几个」。"""
        text = 'new SmsProvider("k", "L", "https://h/x")\n'
        self.assertEqual(csharp_providers(text), [])

    def test_the_dropdown_extractor_rejects_an_unexpected_shape(self):
        with self.assertRaises(AssertionError):
            dropdown_options('Options("phone_provider", "L", "not.the.path", "a"),\n')


class ProviderParityTests(unittest.TestCase):
    def test_the_csharp_keys_match_the_python_registry(self):
        """双向相等：少一个（选不到）和多一个（选了必失败）都是缺陷。"""
        csharp = {row[0] for row in csharp_providers()}
        python = set(sms_providers.available_provider_keys())
        self.assertEqual(csharp - python, set(),
                         "C# 提供了 Python 不认的供应商：%s" % sorted(csharp - python))
        self.assertEqual(python - csharp, set(),
                         "C# 缺少 Python 已注册的供应商：%s" % sorted(python - csharp))

    def test_the_default_endpoints_match(self):
        """🔴 这条是最隐蔽的漂移面：端点写错时下拉框照常工作、余额也读得到，
        只是请求打到了别家主机。"""
        for key, _label, endpoint, _env in csharp_providers():
            with self.subTest(key=key):
                self.assertEqual(endpoint, sms_providers.default_endpoint(key))

    def test_the_api_key_env_names_match(self):
        """C# 用环境变量名做 key 回退，名字写错 ⇒ 回退静默失效。"""
        for key, _label, _endpoint, env in csharp_providers():
            with self.subTest(key=key):
                self.assertEqual(env, sms_providers.api_key_env(key))

    def test_the_labels_are_not_empty_and_unique(self):
        labels = [row[1] for row in csharp_providers()]
        self.assertEqual(len(labels), len(set(labels)), labels)
        self.assertTrue(all(label.strip() for label in labels), labels)

    def test_the_dropdown_offers_exactly_the_csharp_table(self):
        self.assertEqual(dropdown_options(), [row[0] for row in csharp_providers()])

    def test_the_default_provider_constant_matches_python(self):
        self.assertEqual(default_provider_constant(), sms_providers.DEFAULT_PROVIDER)

    def test_the_dropdown_fallback_is_the_constant_not_a_literal(self):
        """默认值只许有一份。写成字面量就会出现「改了一处漏了一处」。

        注意 ``"smsbower"`` 在选项列表里**本来就应该出现**（它是三个选项之一），
        所以要判的是**第四个实参**，不是整段文本里有没有这个字符串 —— 第一版写成
        ``assertNotIn('"smsbower"', call)`` 就是这个错。
        """
        fallback = dropdown_fallback_argument()
        self.assertEqual(fallback, "DefaultPhoneProvider",
                         "supply the constant, not a literal: %r" % fallback)
        self.assertEqual(default_provider_constant(), sms_providers.DEFAULT_PROVIDER)

    def test_nexsms_stays_out_of_the_desktop_surface(self):
        """``nexsms`` 在 Python 里是保留条目（协议未验证、``client_available``
        为假），不能出现在下拉框里 —— 否则操作员能选中一个必然失败的供应商。"""
        self.assertNotIn("nexsms", {row[0] for row in csharp_providers()})
        self.assertNotIn("nexsms", dropdown_options())
        # 反向：Python 侧必须还留着它。真删了这条会红，提醒有人来重新评估。
        self.assertIn("nexsms", sms_providers.provider_keys())


class NoCredentialsInCsharpCatalogTests(unittest.TestCase):
    """与 ``sms_providers`` 自己的守卫同一条判据：注册表里不许有凭据。"""

    def test_no_field_contains_a_credential_shaped_token(self):
        for row in csharp_providers():
            for field in row:
                with self.subTest(field=field):
                    self.assertIsNone(_CREDENTIAL.search(field),
                                      "credential-shaped literal in SmsProviderCatalog.cs: %r" % field)

    def test_the_file_declares_no_key_like_literal(self):
        text = PROVIDER_CATALOG.read_text(encoding="utf-8")
        offenders = [line.strip() for line in text.splitlines()
                     if _CREDENTIAL.search(line) and not line.lstrip().startswith("//")]
        self.assertEqual(offenders, [], "key-like literal in SmsProviderCatalog.cs: %s" % offenders)


class DialogUsesTheSelectedProviderTests(unittest.TestCase):
    def test_the_dialog_no_longer_hardcodes_the_smsbower_section(self):
        """弹窗曾经把 ``phone_reuse.smsbower.*`` 写死在 5 处，于是把供应商切成
        ``herosms`` 之后，弹窗仍然读/写 smsbower 的键 —— 看起来保存成功，后端
        读的是另一个 section。这条钉住它不再回来。"""
        text = DIALOG.read_text(encoding="utf-8")
        self.assertNotIn("phone_reuse.smsbower.", text)

    def test_the_dialog_builds_the_section_from_the_selected_provider(self):
        text = DIALOG.read_text(encoding="utf-8")
        self.assertIn('string section = "phone_reuse." + provider.Key;', text)

    def test_the_dialog_resolves_the_provider_from_the_config(self):
        text = DIALOG.read_text(encoding="utf-8")
        self.assertIn('SmsProviderCatalog.Resolve(settingsService.GetString("phone_reuse.source"))', text)


#: 允许出现供应商键字面量的**生产**文件，只有目录本身：表与下拉框。
CATALOG_FILES = frozenset({
    (REPO_ROOT / "SmsWorkbench" / "SmsProviderCatalog.cs").resolve(),
    (REPO_ROOT / "SmsWorkbench" / "SettingsCatalog.cs").resolve(),
})

#: 只扫生产工程。测试工程**允许**写供应商键 —— 它本来就要构造具体取值。
CSHARP_ROOTS = (REPO_ROOT / "SmsWorkbench", REPO_ROOT / "SmsWorkbench.Contracts")

_PROVIDER_KEYS = frozenset(sms_providers.available_provider_keys())
_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def provider_literals(text):
    """``text`` 里**注释之外**、且恰好等于某个供应商键的字符串字面量。

    只认**完全相等**，不认子串：``RemovePath(root, "phone_reuse.smsbower.pool_size")``
    这类旧键清理必须写出旧键才能删掉它，那是正确的，不该被判为缺陷。
    """
    text = _BLOCK_COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    found = []
    for match in _STRING_LITERAL.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if "//" in text[line_start:match.start()]:
            continue
        if match.group(1) in _PROVIDER_KEYS:
            found.append(match.group(1))
    return found


class NoHardcodedProviderOutsideTheCatalogTests(unittest.TestCase):
    """供应商键只能来自注册表，不能出现在业务逻辑里。

    2026-09-22 实测出三处同源缺陷，形态各不相同但根因一致 —— 把厂商名当成
    逻辑里的取值：``phone_reuse.py`` 的 12 个 ``provider == "smsbower"`` 守卫、
    ``_should_retry_with_new_provider_number`` 把 ``"smsbower_prepare_failed"``
    写进错误集合、以及 ``BackendCommandPlanner`` 的 ``"--phone-source", "smsbower"``。

    C# 侧那一处后果最重：一键接码弹窗里选中的供应商**从来没有传到后端**，
    跑批永远租 SMSBower 的号，而界面显示的是 HeroSMS。所以这里按**取值**判，
    不按调用形态判 —— 换一个供应商名、换一处调用点，同样会被抓住。
    """

    def test_the_scanner_finds_a_planted_literal(self):
        """先证明扫描器会红。不会红的守卫等于没有守卫。"""
        planted = 'var args = new[] { "--phone-source", "herosms" };\n'
        self.assertEqual(provider_literals(planted), ["herosms"])

    def test_the_scanner_ignores_commented_out_literals(self):
        text = '// "--phone-source", "smsbower" was the old default\nvar x = 1;\n'
        self.assertEqual(provider_literals(text), [])

    def test_the_scanner_ignores_a_longer_key_that_merely_starts_with_one(self):
        """旧键清理要写出旧键才删得掉，那不是缺陷。"""
        text = 'RemovePath(root, "phone_reuse.smsbower.pool_size");\n'
        self.assertEqual(provider_literals(text), [])

    def test_no_provider_key_literal_outside_the_catalog_files(self):
        offenders = []
        for root in CSHARP_ROOTS:
            for path in sorted(root.rglob("*.cs")):
                relative = path.relative_to(REPO_ROOT)
                if any(part in ("obj", "bin") for part in relative.parts):
                    continue
                if path.resolve() in CATALOG_FILES:
                    continue
                found = provider_literals(path.read_text(encoding="utf-8"))
                if found:
                    offenders.append("%s: %s" % (relative.as_posix(), found))
        self.assertEqual(
            offenders, [],
            "供应商键被写死在注册表之外（应改从 SmsProviderCatalog 取）：%s" % offenders)


if __name__ == "__main__":
    unittest.main()
