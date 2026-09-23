"""``scripts/scan_hardcoded_secrets.py`` 的行为契约。

为什么要有这个文件
------------------
2026-09-22 之前这个闸门**零测试**，而它的失败模式全部是静默的：

* 少一条模式 ⇒ 泄漏直接过闸，没有任何信号；
* 多一条模式 ⇒ 误报，而误报的处置方式是 ``--no-verify``，门禁等于没有。

所以两个方向都要钉死：**该抓的抓得住**（用对标项目 `cxqc168-wq/gpt-register-pro`
实际泄漏出去的三种形态做回归样本），**不该抓的不吵**（本仓全树必须 0 findings）。

改写动因（2026-09-22）
----------------------
读原实现时发现三个整类漏检，全部是「静默」性质的：

1. ``PAT`` / ``PAT2`` 的值字符集是 ``[A-Za-z0-9_\\-\\.]``，**不含符号**，
   于是 ``password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'`` 这种带 ``!`` 的明文密码整条漏掉 ——
   而带符号的密码恰恰是最常见的一类。
2. 原实现有 ``if val.startswith('http'): continue``，把 URL 形态**整类短路**，
   于是硬编码的授权服务器公网 IP 一个都报不出来。
3. 🔴 **名字恰好等于凭据词的变量三个模式全漏。** 三个正则的前缀
   ``[A-Za-z_][A-Za-z0-9_]*`` 都是**必填**的，`password` 没有字符可以让给前缀，
   于是小写 ``password`` / ``secret`` / ``token`` / ``pwd`` / ``key`` 一个都匹配不上
   （只有全大写形态靠 PAT2 兜住）。而 ``password = "..."`` 恰恰是最可能的写法。
   ``NamePredicateTests`` 就是这条的回归。
"""

import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "scan_hardcoded_secrets",
    Path(__file__).resolve().parents[1] / "scripts" / "scan_hardcoded_secrets.py",
)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


@contextlib.contextmanager
def _scanning(root, dirs):
    """把扫描器指向一棵临时树，且保证退出时还原。

    不这样做就只能扫真实仓库，而真实仓库是干净的 —— 永远验证不到「抓到」这一侧。
    """
    original = (scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS)
    scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS = str(root), list(dirs), []
    try:
        yield
    finally:
        scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS = original


class RegressionTests(unittest.TestCase):
    """对标项目真实泄漏出去的三类形态 —— 改写前它们全部漏检。"""

    def test_a_password_with_symbols_is_caught(self):
        """PAT/PAT2 的字符集不含符号，这类密码只有 PAT3 抓得住。"""
        line = "    password: 'Zq3Xk9Mv7Rt2Lp5Wb8!',\n"
        kinds = [item[0] for item in scanner.scan_line(line)]
        self.assertIn("symbol-literal", kinds)

    def test_the_old_patterns_cannot_see_that_password(self):
        """这条是 PAT3 存在的理由：证明旧值字符集确实漏，不是冗余。"""
        line = "    password: 'Zq3Xk9Mv7Rt2Lp5Wb8!',\n"
        self.assertIsNone(scanner._PLAIN_VALUE.match("Zq3Xk9Mv7Rt2Lp5Wb8!"))
        self.assertIsNotNone(scanner._SYMBOL_VALUE.match("Zq3Xk9Mv7Rt2Lp5Wb8!"))

    def test_a_hardcoded_public_endpoint_is_caught(self):
        """``val.startswith('http')`` 的短路已移除，公网 IP 端点现在报得出来。"""
        found = scanner.scan_line('const AUTH = "http://64.90.20.244:8443";\n')
        self.assertIn(("public-endpoint", "64.90.20.244", "64.90.20.244", "8443"), found)

    def test_a_value_that_the_example_file_declares_blank_is_caught(self):
        """跨文件检查：示例里留空、源码里是真值 —— 最强的结构性信号。"""
        keys = {"password": ("config.example.json", "")}
        found = scanner.scan_line('    "password": "Zq3Xk9Mv7Rt2Lp5Wb8",\n', keys)
        self.assertIn("inline-vs-example", [item[0] for item in found])

    def test_the_cross_file_check_reports_which_example_file_disagrees(self):
        keys = {"api_token": ("config.example.json", "")}
        found = scanner.scan_line('api_token = "abcdEFGH1234ijkl"\n', keys)
        reasons = [item[3] for item in found if item[0] == "inline-vs-example"]
        self.assertTrue(reasons and "config.example.json" in reasons[0])


class DocumentationSampleTests(unittest.TestCase):
    """反引号包围的样例是文档，不是泄漏。

    没有这条豁免，本脚本每次记录自己新增的检测形态都会自我命中；下一个维护者
    会用「豁免整个文件」来"修复"，而那样真凭据粘进该文件就再也没人看得见。
    """

    def test_a_backtick_fenced_password_sample_is_not_a_finding(self):
        line = "   ``[A-Za-z0-9_\\\\-\\\\.]``，所以 `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'`\n"
        self.assertEqual(scanner.scan_line(line), [])

    def test_a_backtick_fenced_endpoint_sample_is_not_a_finding(self):
        line = "   明文 IP ``http://64.90.20.244:8443`` 写死在源码里\n"
        self.assertEqual(scanner.scan_line(line), [])

    def test_a_backtick_fenced_sample_inside_a_comment_is_not_a_finding(self):
        line = "# `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'` 整条漏掉\n"
        self.assertEqual(scanner.scan_line(line), [])

    def test_a_fenced_sample_is_still_caught(self):
        """豁免必须窄：围栏代码块里的裸值不是行内代码，照样要抓。"""
        found = scanner.scan_line("    http://64.90.20.244:8443\n")
        self.assertIn("public-endpoint", [item[0] for item in found])

    def test_a_quoted_but_unfenced_sample_is_still_caught(self):
        found = scanner.scan_line('AUTH = "http://64.90.20.244:8443"\n')
        self.assertIn("public-endpoint", [item[0] for item in found])

    def test_the_backtick_must_be_immediately_adjacent(self):
        """隔了空格就不算行内代码 —— 否则行尾补一个反引号就能把结果洗白。"""
        line = 'password = "Zq3Xk9Mv7Rt2Lp5Wb8!" `'
        self.assertFalse(scanner.is_documentation_sample(line, scanner._ASSIGN.search(line)))


class NamePredicateTests(unittest.TestCase):
    """🔴 名字恰好等于凭据词的变量，改写前三个模式全漏。

    三个旧正则的前缀 ``[A-Za-z_][A-Za-z0-9_]*`` 是必填的，所以名字就是关键字本身时
    没有字符可以让给前缀 —— 实测 ``password`` / ``secret`` / ``token`` / ``pwd`` / ``key``
    在 PAT/PAT2/PAT3 上全部为 False，只有全大写形态被 PAT2 兜住。
    """

    def test_a_bare_credential_word_is_recognised(self):
        for name in ("password", "PASSWORD", "Password", "secret", "SECRET",
                     "token", "TOKEN", "pwd", "key", "KEY", "auth", "credential"):
            with self.subTest(name=name):
                self.assertTrue(scanner.credential_name(name))

    def test_a_bare_credential_word_assignment_is_now_reported(self):
        """端到端：这是改写前会漏掉的那一行。"""
        for name in ("password", "secret", "token", "pwd", "key"):
            with self.subTest(name=name):
                line = '%s = "abcdEFGH1234ijkl"\n' % name
                self.assertIn("literal", [item[0] for item in scanner.scan_line(line)])

    def test_a_separated_credential_word_is_recognised(self):
        for name in ("api_token", "API_TOKEN", "SESSION_TOKEN_ENV", "client_secret",
                     "MY-PASSWORD", "db.pwd", "oauth_credentials",
                     "pwd_hash_salt"):  # 含 `pwd` 段就值得看一眼，由值判定决定去留
            with self.subTest(name=name):
                self.assertTrue(scanner.credential_name(name))

    def test_a_camel_case_credential_word_is_recognised(self):
        for name in ("accessToken", "refreshToken", "apiKey", "clientSecret", "idToken"):
            with self.subTest(name=name):
                self.assertTrue(scanner.credential_name(name))

    def test_a_compound_credential_word_is_recognised(self):
        """`apikey` 是一整段，分段看不见它，所以词表里必须单列。"""
        for name in ("apikey", "apiKey", "secretKey", "privateKey", "accessToken"):
            with self.subTest(name=name):
                self.assertTrue(scanner.credential_name(name))

    def test_a_word_that_merely_starts_with_a_credential_word_is_not(self):
        """子串匹配会把这一整类判成凭据，而误报的处置方式是 --no-verify。"""
        for name in ("keyboard", "keyboard_layout", "author", "authors", "authorization",
                     "tokenizer", "secretary", "passwordless", "keyspace"):
            with self.subTest(name=name):
                self.assertFalse(scanner.credential_name(name))

    def test_a_word_that_merely_starts_with_a_credential_word_is_silent(self):
        for line in ('keyboard = "abcdEFGH1234ijkl"\n',
                     'author = "abcdEFGH1234ijkl"\n',
                     'tokenizer = "abcdEFGH1234ijkl"\n',
                     'secretary = "abcdEFGH1234ijkl"\n'):
            with self.subTest(line=line):
                self.assertEqual(scanner.scan_line(line), [])

    def test_an_empty_or_non_string_name_is_not_a_credential(self):
        for name in ("", None, "x", "count", "index"):
            with self.subTest(name=name):
                self.assertFalse(scanner.credential_name(name))


class CommentAndDocstringTests(unittest.TestCase):
    """docstring 示例是本类扫描最大的假阳性来源。

    ``{"totp_secret": "JBSWY3DPEHPK3PXP"}`` 是 RFC 4226 教科书里的示例密钥，长度和
    字符分布与真密钥毫无区别 —— **只有上下文能区分**，所以只能按上下文跳过。
    """

    def test_a_docstring_example_is_skipped(self):
        text = (
            'def f():\n'
            '    """Do a thing.\n'
            '\n'
            '    Returns:\n'
            '        {"ok": True, "totp_secret": "JBSWY3DPEHPK3PXP"}\n'
            '    """\n'
            '    return 1\n'
        )
        lines = list(scanner.iter_scannable_lines(text, ".py"))
        self.assertEqual([number for number, _ in lines], [1, 7])
        self.assertEqual(scanner.scan_line(lines[0][1]), [])
        self.assertEqual(scanner.scan_line(lines[1][1]), [])

    def test_a_single_quoted_docstring_is_skipped_too(self):
        text = "def f():\n    '''Example: totp_secret = \"JBSWY3DPEHPK3PXP\"'''\n    return 1\n"
        numbers = [number for number, _ in scanner.iter_scannable_lines(text, ".py")]
        self.assertEqual(numbers, [1, 3])

    def test_a_comment_line_is_skipped(self):
        text = '# totp_secret = "JBSWY3DPEHPK3PXP"\nvalue = 1\n'
        numbers = [number for number, _ in scanner.iter_scannable_lines(text, ".py")]
        self.assertEqual(numbers, [2])

    def test_code_after_a_docstring_is_scanned_again(self):
        """三引号奇数次才切换状态 —— 一次切换写错就会让文件后半段全部隐形。"""
        text = (
            '"""Module."""\n'
            'totp_secret = "JBSWY3DPEHPK3PXP"\n'
        )
        numbers = [number for number, _ in scanner.iter_scannable_lines(text, ".py")]
        self.assertEqual(numbers, [2])
        self.assertEqual(len(scanner.scan_line("totp_secret = \"JBSWY3DPEHPK3PXP\"\n")), 1)

    def test_a_csharp_comment_is_skipped(self):
        text = '// token = "abcdEFGH1234ijkl"\nint x = 1;\n'
        numbers = [number for number, _ in scanner.iter_scannable_lines(text, ".cs")]
        self.assertEqual(numbers, [2])

    def test_markdown_is_not_skipped(self):
        """`.md` 没有注释语法，跳过会连真凭据一起丢。"""
        text = '# Title\ntoken = "abcdEFGH1234ijkl"\n'
        numbers = [number for number, _ in scanner.iter_scannable_lines(text, ".md")]
        self.assertEqual(numbers, [1, 2])


class PrecommitGuardParityTests(unittest.TestCase):
    """两份 ``iter_scannable_lines`` 必须给出同样的行。

    ``scripts/`` 不是包、也没有 ``common``，所以 ``scan_hardcoded_secrets.py`` 里的
    实现是**有意重复**的（详见那边的 docstring）。重复的代价是漂移 —— 这个测试就是
    防漂移的：改动任一份而不同步另一份，CI 会红。
    """

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "precommit_guard.py"
        spec = importlib.util.spec_from_file_location("precommit_guard_for_parity", path)
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    CASES = (
        (".py", '"""Module."""\nvalue = 1\n'),
        (".py", 'def f():\n    """Doc.\n\n    Example: totp_secret = "JBSWY3DPEHPK3PXP"\n    """\n    return 1\n'),
        (".py", "def f():\n    '''Doc.'''\n    return 1\n"),
        (".py", '# comment\ntotp_secret = "JBSWY3DPEHPK3PXP"\n'),
        (".py", 'x = 1\n"""a"""\ny = 2\n"""b"""\nz = 3\n'),
        (".cs", '// c\n/* d */\n* e\n/// f\ng();\n'),
        (".md", '# Title\ntoken = "abcdEFGH1234ijkl"\n'),
        (".sh", '# c\ntoken = "abcdEFGH1234ijkl"\n'),
        (".yml", '# c\ntoken: "abcdEFGH1234ijkl"\n'),
    )

    def test_both_implementations_agree(self):
        for suffix, text in self.CASES:
            with self.subTest(suffix=suffix, text=text[:32]):
                mine = list(scanner.iter_scannable_lines(text, suffix))
                theirs = list(self.guard.iter_scannable_lines(text, suffix))
                self.assertEqual(mine, theirs)


class PublicIdentifierTests(unittest.TestCase):
    """PKCE 公开客户端的 ``client_id`` 设计上就该在源码里。

    本仓 ``codex_oauth.py:35`` 是 Codex CLI 的公开 client id，``codex_export.py:333``
    还拿它当 fallback；``config.example.json`` 的同名键留空 —— 跨文件检查会把它判成
    泄漏，而它恰恰是唯一正确的写法。
    """

    def test_a_public_client_id_is_recognised(self):
        for name in ("client_id", "CLIENT_ID", "oauth_client_id", "OAuth-Client-Id",
                     "app_id", "APP_ID", "application_id", "tenant_id", "project_id"):
            with self.subTest(name=name):
                self.assertTrue(scanner.is_public_identifier(name))

    def test_the_codex_client_id_is_not_reported(self):
        keys = {"client_id": ("config.example.json", "")}
        line = 'CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"\n'
        self.assertEqual(scanner.scan_line(line, keys), [])

    def test_a_client_id_shaped_key_holding_a_secret_is_still_caught(self):
        """后缀匹配而非子串 —— 加了后缀的键不再是「公开标识符」。"""
        for name in ("client_id_secret", "CLIENT_ID_SECRET", "client_id_token", "app_id_key"):
            with self.subTest(name=name):
                self.assertFalse(scanner.is_public_identifier(name))

    def test_a_real_secret_is_never_excused_by_name(self):
        line = 'CLIENT_SECRET = "Zq3Xk9Mv7Rt2Lp5Wb8"\n'
        self.assertTrue(scanner.scan_line(line))


class PublicIpTests(unittest.TestCase):
    """端点检测必须只报「看起来可路由」的地址，否则日志与本地配置会淹没结果。"""

    def test_routable_addresses_are_public(self):
        for address in ("8.8.8.8", "64.90.20.244", "1.1.1.1", "203.0.114.1"):
            with self.subTest(address=address):
                self.assertTrue(scanner.is_public_ip(address))

    def test_local_and_documentation_ranges_are_not_public(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.1.1", "0.0.0.0",
                        "172.16.0.1", "172.31.255.254", "192.0.2.1", "198.51.100.7",
                        "203.0.113.9", "224.0.0.1", "255.255.255.255"):
            with self.subTest(address=address):
                self.assertFalse(scanner.is_public_ip(address))

    def test_172_32_is_public_but_172_16_through_31_is_not(self):
        """172 的私网段边界最容易写错 —— 172.0.0.0/12 覆盖的是 16..31。"""
        self.assertFalse(scanner.is_public_ip("172.16.0.1"))
        self.assertFalse(scanner.is_public_ip("172.31.0.1"))
        self.assertTrue(scanner.is_public_ip("172.15.255.255"))
        self.assertTrue(scanner.is_public_ip("172.32.0.1"))

    def test_an_out_of_range_octet_is_not_an_address(self):
        for address in ("999.1.1.1", "1.2.3", "1.2.3.4.5", "", "not-an-ip"):
            with self.subTest(address=address):
                self.assertFalse(scanner.is_public_ip(address))

    def test_a_local_endpoint_in_source_is_silent(self):
        for line in ('BASE = "http://127.0.0.1:8080"\n',
                     'PROXY = "http://192.168.1.10:7897"\n',
                     'DOC = "http://192.0.2.1:80"\n'):
            with self.subTest(line=line):
                self.assertEqual(scanner.scan_line(line), [])


class SecretValueHeuristicTests(unittest.TestCase):
    """``looks_like_secret_value`` 是 PAT3 的降噪闸 —— 字符集放宽后必须补的这一道。"""

    def test_urls_paths_and_prose_are_rejected(self):
        for value in ("http://example.com/abcdefgh", "https://x.test/y",
                      "/usr/local/share/thing", "\\server\\share\\thing",
                      "./relative/path/thing", "../up/and/away/thing"):
            with self.subTest(value=value):
                self.assertFalse(scanner.looks_like_secret_value(value))

    def test_all_alpha_or_all_digit_values_are_rejected(self):
        for value in ("abcdefghijklmnop", "1234567890123456"):
            with self.subTest(value=value):
                self.assertFalse(scanner.looks_like_secret_value(value))

    def test_a_mixed_value_is_accepted(self):
        self.assertTrue(scanner.looks_like_secret_value("Zq3Xk9Mv7Rt2Lp5Wb8!"))

    def test_a_prose_password_is_not_treated_as_a_secret(self):
        line = 'password = "not-a-real-secret"\n'
        self.assertEqual(scanner.scan_line(line), [])

    def test_a_status_identifier_value_is_not_treated_as_a_secret(self):
        """两个值分支都要过 `looks_like_secret_value`。

        只给符号分支加这一道，``"auth_state": "auth_state_failed"`` 这种普通映射
        就会漏进来 —— 实测在 ``accounts/account_scan.py:53`` 报过一条这样的假阳性。
        """
        for line in ('"auth_state": "auth_state_failed"\n',
                     'auth_state = "authenticated"\n',
                     'client_secret = "unset"\n'):
            with self.subTest(line=line):
                self.assertEqual(scanner.scan_line(line), [])

    def test_the_digit_requirement_is_what_rejects_those_values(self):
        """证明上面的静默来自值判定，而不是名字判定 —— 名字是认得的。"""
        self.assertTrue(scanner.credential_name("auth_state"))
        self.assertFalse(scanner.looks_like_secret_value("auth_state_failed"))
        self.assertFalse(scanner.looks_like_secret_value("authenticated"))


class NoiseGuardTests(unittest.TestCase):
    """这些豁免此前已存在，一并钉住 —— 改动 scan_line 最容易把它们弄丢。"""

    def test_placeholder_values_are_skipped(self):
        for value in ("your_token_here", "CHANGEME123456", "placeholder_value",
                      "test_value_12345", "dummy_value_12345", "REDACTED1234567"):
            with self.subTest(value=value):
                line = 'api_token = "%s"\n' % value
                self.assertEqual(scanner.scan_line(line), [])

    def test_non_credential_variable_names_are_skipped(self):
        for name in ("site_key", "PROBE_TOKEN", "PLACEHOLDER_KEY",
                     "PERSISTENCE_KEY", "FALLBACK_TOKEN", "UNAUTHORIZED_TOKEN",
                     "STATUS_TOKEN", "PASSWORDLESS_SIGNUP_CODE"):
            with self.subTest(name=name):
                line = '%s = "abcdEFGH1234ijkl"\n' % name
                self.assertEqual(scanner.scan_line(line), [])

    def test_environment_variable_names_are_skipped_but_values_are_not(self):
        """``_ENV`` 后缀存的是查找键；同样含 TOKEN 的真值仍必须抓。"""
        self.assertEqual(scanner.scan_line('SESSION_TOKEN_ENV = "PP_SESSION_TOKEN"\n'), [])
        self.assertTrue(scanner.scan_line('SESSION_TOKEN = "abcdEFGH1234ijkl"\n'))


class OutputContractTests(unittest.TestCase):
    """脚本承诺「绝不输出完整值」。它同时被 CI 日志和人工排查读到。"""

    def _tree(self, tmp_path, files):
        src = tmp_path / "src"
        src.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (src / name).write_text(text, encoding="utf-8", newline="\n")
        return src

    def test_a_leak_exits_nonzero(self):
        """没有这一步脚本永远 exit 0，在 CI 上就是个装饰品。"""
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._tree(root, {"leak.js": 'const AUTH = "http://64.90.20.244:8443";\n'})
            buffer = io.StringIO()
            with _scanning(root, ["src"]), contextlib.redirect_stdout(buffer):
                code = scanner.main()
            self.assertEqual(code, 1)
            self.assertIn("64.90.20.244", buffer.getvalue())

    def test_a_clean_tree_exits_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._tree(root, {"clean.js": 'const AUTH = "http://127.0.0.1:8443";\n'})
            buffer = io.StringIO()
            with _scanning(root, ["src"]), contextlib.redirect_stdout(buffer):
                code = scanner.main()
            self.assertEqual(code, 0)
            self.assertIn("total findings: 0", buffer.getvalue())

    def test_only_the_prefix_of_a_secret_is_printed(self):
        """完整值一旦进了 CI 日志就等于又泄漏一次。"""
        import tempfile
        secret = "Zq3Xk9Mv7Rt2Lp5Wb8"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._tree(root, {"leak.py": 'password = "%s"\n' % secret})
            buffer = io.StringIO()
            with _scanning(root, ["src"]), contextlib.redirect_stdout(buffer):
                scanner.main()
            output = buffer.getvalue()
            self.assertIn(secret[:3], output)
            self.assertNotIn(secret, output)


class RepositoryIntegrationTests(unittest.TestCase):
    """真实仓库必须干净 —— 这条失败意味着门禁开始吵，接着就会被绕过。"""

    def test_the_scanned_tree_has_no_findings(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = scanner.main()
        self.assertEqual(code, 0, "扫描器在本仓报了 findings:\n%s" % buffer.getvalue())
        self.assertIn("total findings: 0", buffer.getvalue())

    def test_the_scanner_reports_how_many_example_keys_it_tracked(self):
        """跨文件检查的参考集为空时它会静默失效，所以这个数字必须可见。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            scanner.main()
        line = [row for row in buffer.getvalue().splitlines() if row.startswith("example keys tracked:")]
        self.assertTrue(line)
        self.assertGreater(int(line[0].split(":")[1].strip()), 0)

    def test_a_wrong_root_is_a_hard_error_not_a_silent_pass(self):
        """原实现 ROOT 算错时所有 SCAN_DIRS 都 isdir 失败，静默产出空列表。"""
        with _scanning(os.path.join(os.sep, "definitely-not-here"), scanner.SCAN_DIRS):
            with self.assertRaises(SystemExit) as raised:
                list(scanner.iter_files())
            self.assertIn("FATAL", str(raised.exception))

    def test_the_scanner_itself_is_still_scanned(self):
        """不能靠豁免文件来解决自我命中 —— 真凭据粘进去必须看得见。"""
        target = os.path.normpath(
            Path(__file__).resolve().parents[1] / "scripts" / "scan_hardcoded_secrets.py")
        self.assertIn(target, list(scanner.iter_files()))


if __name__ == "__main__":
    unittest.main()
