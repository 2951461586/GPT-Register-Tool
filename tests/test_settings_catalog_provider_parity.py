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
#: 第五个实参是**协议族**（`SmsActivateProtocol` / `NexsmsProtocol` 标识符，不是字面量），
#: 所以它按原样捕获，再由 ``protocol_constant`` 解析成值。
ROW_PATTERN = re.compile(
    r'new SmsProvider\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,'
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)')

#: ``internal const string SmsActivateProtocol = "sms_activate_handler";``
PROTOCOL_CONST_PATTERN = re.compile(
    r'internal\s+const\s+string\s+(SmsActivateProtocol|NexsmsProtocol)\s*=\s*"([^"]+)"')

#: 下拉框那一行。
DROPDOWN_ANCHOR = 'Options("phone_provider"'
CONST_PATTERN = re.compile(r'DefaultPhoneProvider\s*=\s*"([^"]+)"')

#: 密钥形态：>=24 位连续 token 字符。与 ``sms_providers`` 自己的凭据守卫同一判据。
_CREDENTIAL = re.compile(r"[A-Za-z0-9_\-]{24,}")

#: C# 注释。
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def strip_csharp_comments(text):
    """剥掉 ``//`` 行注释与 ``/* */`` 块注释。

    🔴 对**切出来的代码块**做 ``assertIn`` 之前必须先剥注释：变异验证实测到过
    「把 ``return true;`` 注释掉，守卫仍然绿」—— 因为 ``assertIn`` 被注释里的
    同名子串满足了。这与 ``test_config_usage`` 那条
    ``test_the_extractor_ignores_commented_out_literals`` 是同一类缺陷。

    只用于断言，**不**用于花括号配平（配平始终在原文上做，保证切块稳定）。
    弹窗文件里没有字符串内含 ``//`` 的字面量（``grep -n '://'`` 为空），
    所以按行剥是安全的。
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def csharp_providers(text=None):
    """``SmsProviderCatalog.cs`` 里的 ``(key, label, endpoint, env, protocol)`` 五元组。

    第五项是**标识符原文**（如 ``SmsActivateProtocol``），用 :func:`protocol_constant`
    解析成它声明的值。
    """
    if text is None:
        text = PROVIDER_CATALOG.read_text(encoding="utf-8")
    return ROW_PATTERN.findall(text)


def protocol_constant(name, text=None):
    """``SmsProviderCatalog.cs`` 里 ``const string <name> = "..."`` 的值。"""
    if text is None:
        text = PROVIDER_CATALOG.read_text(encoding="utf-8")
    table = dict(PROTOCOL_CONST_PATTERN.findall(text))
    if name not in table:
        raise AssertionError("protocol constant %r not declared in SmsProviderCatalog.cs" % name)
    return table[name]


def resolved_protocols(text=None):
    """``{key: 协议值}``，把表里的标识符解析成常量值。"""
    return {row[0]: protocol_constant(row[4], text) for row in csharp_providers(text)}


def brace_block(text, anchor):
    """``anchor`` 之后第一个 ``{`` 起、按花括号配平切出的**原文**块（含两端花括号）。

    配平在原文上做（不剥注释），这样切块位置不受注释里花括号的影响。
    """
    if anchor not in text:
        raise AssertionError("anchor not found: %r" % anchor)
    start = text.index("{", text.index(anchor))
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("unbalanced braces after %r" % anchor)


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

    def test_the_four_rows_parse(self):
        rows = csharp_providers()
        self.assertEqual(len(rows), 4, rows)

    def test_a_reformatted_row_still_parses(self):
        text = ('new SmsProvider(\n    "k",\n    "L",\n    "https://h/x",\n    "K_ENV",\n'
                '    SmsActivateProtocol)\n')
        self.assertEqual(csharp_providers(text), [("k", "L", "https://h/x", "K_ENV", "SmsActivateProtocol")])

    def test_a_row_with_a_missing_field_is_not_silently_skipped(self):
        """少一个字段时正则不匹配 ⇒ 行数变少 ⇒ 上面的 ``len == 4`` 会红。
        这条钉住「不匹配就是缺陷」，而不是「匹配到几个算几个」。"""
        text = 'new SmsProvider("k", "L", "https://h/x")\n'
        self.assertEqual(csharp_providers(text), [])

    def test_a_row_missing_only_the_protocol_is_not_silently_skipped(self):
        """第五个实参是**标识符**，正则一旦放宽成「可选的字符串字面量」，
        漏写协议的那一行就会被静默降级成 4 元组。这条钉住「必须写全 5 项」。"""
        text = 'new SmsProvider("k", "L", "https://h/x", "K_ENV")\n'
        self.assertEqual(csharp_providers(text), [])

    def test_the_protocol_resolver_rejects_an_undeclared_constant(self):
        """标识符拼错（或常量被删）时必须报错，不能静默当空。"""
        text = 'new SmsProvider("k", "L", "https://h/x", "K_ENV", BogusProtocol)\n'
        with self.assertRaises(AssertionError):
            resolved_protocols(text)

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
        for key, _label, endpoint, _env, _protocol in csharp_providers():
            with self.subTest(key=key):
                self.assertEqual(endpoint, sms_providers.default_endpoint(key))

    def test_the_api_key_env_names_match(self):
        """C# 用环境变量名做 key 回退，名字写错 ⇒ 回退静默失效。"""
        for key, _label, _endpoint, env, _protocol in csharp_providers():
            with self.subTest(key=key):
                self.assertEqual(env, sms_providers.api_key_env(key))

    def test_the_protocols_match_python(self):
        """🔴 这一维漏掉过一次，代价是桌面端一键接码对 nexsms 直接 403。

        协议写错时**没有任何可见症状**：下拉框照常、provider 解析照常、section
        也照常拼对，只有真正去读在线目录的那一步才炸，而且报的是 HTTP 错误，
        看不出是「协议不匹配」。所以它必须像端点一样被逐项钉住。
        """
        for key, protocol in resolved_protocols().items():
            with self.subTest(key=key):
                self.assertEqual(protocol, sms_providers.PROVIDERS[key].protocol)

    def test_the_protocol_constants_match_python(self):
        """表里的值是**标识符**，比较逻辑用的是常量 —— 两处都要钉，
        否则常量改了、表里没改（或反过来）会留下一个能通过上面那条的缺口。"""
        self.assertEqual(protocol_constant("SmsActivateProtocol"), sms_providers.PROTOCOL_SMS_ACTIVATE)
        self.assertEqual(protocol_constant("NexsmsProtocol"), sms_providers.PROTOCOL_NEXSMS)

    def test_the_dialog_gates_the_online_catalog_on_the_protocol(self):
        """🔴 这条是本次缺口的**回归守卫**：它把「读在线目录」这一步钉在协议判断**之内**。

        只断言 ``assertIn("CatalogIsSmsActivate", text)`` 是没用的 —— 判断可以写
        在文件里却包不住那次调用。所以这里按花括号配平切出 ``if`` 的**整个块**，
        再要求目录调用落在块内、且块外不再出现（否则说明还有一条未受保护的调用）。
        """
        text = DIALOG.read_text(encoding="utf-8")
        gate = "if (provider.CatalogIsSmsActivate)"
        self.assertIn(gate, text, "the dialog no longer gates on the protocol")

        block = brace_block(text, gate)
        guarded = strip_csharp_comments(block)
        self.assertIn("LoadOpenAiCatalogAsync", guarded,
                      "the catalog call is not inside the protocol gate")
        after = text[text.index(block) + len(block):]
        self.assertNotIn("LoadOpenAiCatalogAsync", after,
                         "a second, ungated catalog call exists after the gate")

    def test_the_fallback_path_reads_the_saved_choice(self):
        """回退路径必须真的从配置读国家与档位 —— 写死一个默认国会让「切到
        nexsms 之后一键接码用错国家」这种错误重新出现，而且没有任何提示。"""
        text = DIALOG.read_text(encoding="utf-8")
        self.assertIn("ReadSavedSmsProviderChoice(section)", text)
        self.assertIn('section + ".country"', text)

    def test_the_fallback_path_does_not_write_the_dead_leaves(self):
        """回退路径的选择项本来就是从配置读出来的，回写只会把
        ``service_name`` / ``country_name_zh`` 这两个**只写不读**的叶子塞进
        当前供应商的 section —— 那会让 ``tests/test_config_usage.py`` 变红
        （见 `docs/TROUBLESHOOTING.md` §13）。

        注意这两行**必须留在 sms-activate 分支里**：``test_config_usage`` 的
        `counter-example moved` 断言要求它们仍出现在本文件中。所以这条只钉
        「回退分支在回写之前就 return」，不钉「文件里没有这两个字面量」。
        """
        text = DIALOG.read_text(encoding="utf-8")
        gate = "if (!provider.CatalogIsSmsActivate)"
        # 先钉条件本身：只检查块内文本的话，把条件改成 `if (false)` 能让块内
        # 一个字都不变，而回写就会真的执行。
        self.assertIn(gate, text, "the fallback path no longer gates the write-back")

        block = strip_csharp_comments(brace_block(text, gate))
        self.assertIn("return true;", block,
                      "the fallback path must return before the write-back")
        self.assertNotIn("UpdateConfig", block,
                         "the fallback path must not write the provider section")

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

    def test_nexsms_is_offered_once_a_client_exists(self):
        """``nexsms`` 曾经是「保留但未接线」的条目，桌面端**故意不列它** ——
        选中一个必然失败的供应商比不提供更糟。协议确认并接上客户端之后，
        它就和其他三家一样必须出现在桌面上，否则操作员选不到。

        这条是上面那条的**反向**：它不再断言「不在」，而是断言「在且端点对」。
        真删掉客户端而忘了把 C# 行拿掉时，上面那条双向相等会红；反过来把
        Python 的 ``client_available`` 关掉时这条会红。
        """
        self.assertIn("nexsms", {row[0] for row in csharp_providers()})
        self.assertIn("nexsms", dropdown_options())
        self.assertIn("nexsms", sms_providers.available_provider_keys())

    def test_the_nexsms_endpoint_carries_no_handler_path(self):
        """🔴 这一家是**基址**，不是 ``/stubs/handler_api.php``。

        上面 ``test_the_default_endpoints_match`` 只做字符串相等，两侧一起写错
        也会通过 —— 而后果是所有请求 404，且要到真跑一次才发现。这条独立钉住
        形状：其余三家都带 handler 路径，唯独这家不能带。
        """
        endpoint = dict(
            (key, url) for key, _label, url, _env, _protocol in csharp_providers()
        )["nexsms"]
        self.assertNotIn("/stubs/", endpoint)
        self.assertFalse(endpoint.rstrip("/").endswith(".php"), endpoint)
        self.assertEqual(endpoint, sms_providers.default_endpoint("nexsms"))


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
