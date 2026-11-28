"""``scripts/scan_hardcoded_secrets.py`` 的行为契约。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import typing
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "scan_hardcoded_secrets",
    Path(__file__).resolve().parents[2] / "scripts" / "scan_hardcoded_secrets.py",
)
assert _spec is not None and _spec.loader is not None
scanner = typing.cast(typing.Any, importlib.util.module_from_spec(_spec))
_spec.loader.exec_module(scanner)

_guard_path = Path(__file__).resolve().parents[2] / "scripts" / "precommit_guard.py"
_guard_spec = importlib.util.spec_from_file_location("precommit_guard_for_parity", _guard_path)
assert _guard_spec is not None and _guard_spec.loader is not None
_guard = typing.cast(typing.Any, importlib.util.module_from_spec(_guard_spec))
_guard_spec.loader.exec_module(_guard)


@contextlib.contextmanager
def _scanning(root: str | Path, dirs: list[str] | tuple[str, ...]):
    original = (scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS)
    scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS = str(root), list(dirs), []
    try:
        yield
    finally:
        scanner.ROOT, scanner.SCAN_DIRS, scanner.ROOT_SCRIPTS = original


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8", newline="\n")
    return src


def test_a_password_with_symbols_is_caught():
    line = "    password: 'Zq3Xk9Mv7Rt2Lp5Wb8!',\n"
    kinds = [item[0] for item in scanner.scan_line(line)]
    assert "symbol-literal" in kinds


def test_the_old_patterns_cannot_see_that_password():
    assert scanner._PLAIN_VALUE.match("Zq3Xk9Mv7Rt2Lp5Wb8!") is None
    assert scanner._SYMBOL_VALUE.match("Zq3Xk9Mv7Rt2Lp5Wb8!") is not None


def test_a_hardcoded_public_endpoint_is_caught():
    found = scanner.scan_line('const AUTH = "http://64.90.20.244:8443";\n')
    assert ("public-endpoint", "64.90.20.244", "64.90.20.244", "8443") in found


def test_a_value_that_the_example_file_declares_blank_is_caught():
    keys = {"password": ("config.example.json", "")}
    found = scanner.scan_line('    "password": "Zq3Xk9Mv7Rt2Lp5Wb8",\n', keys)
    assert "inline-vs-example" in [item[0] for item in found]


def test_the_cross_file_check_reports_which_example_file_disagrees():
    keys = {"api_token": ("config.example.json", "")}
    found = scanner.scan_line('api_token = "abcdEFGH1234ijkl"\n', keys)
    reasons = [item[3] for item in found if item[0] == "inline-vs-example"]
    assert reasons and "config.example.json" in reasons[0]


def test_a_backtick_fenced_password_sample_is_not_a_finding():
    line = "   ``[A-Za-z0-9_\\\\-\\\\.]``，所以 `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'`\n"
    assert scanner.scan_line(line) == []


def test_a_backtick_fenced_endpoint_sample_is_not_a_finding():
    line = "   明文 IP ``http://64.90.20.244:8443`` 写死在源码里\n"
    assert scanner.scan_line(line) == []


def test_a_backtick_fenced_sample_inside_a_comment_is_not_a_finding():
    line = "# `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'` 整条漏掉\n"
    assert scanner.scan_line(line) == []


def test_a_fenced_sample_is_still_caught():
    found = scanner.scan_line("    http://64.90.20.244:8443\n")
    assert "public-endpoint" in [item[0] for item in found]


def test_a_quoted_but_unfenced_sample_is_still_caught():
    found = scanner.scan_line('AUTH = "http://64.90.20.244:8443"\n')
    assert "public-endpoint" in [item[0] for item in found]


def test_the_backtick_must_be_immediately_adjacent():
    line = 'password = "Zq3Xk9Mv7Rt2Lp5Wb8!" `'
    assert not scanner.is_documentation_sample(line, scanner._ASSIGN.search(line))


@pytest.mark.parametrize(
    "name",
    (
        "password",
        "PASSWORD",
        "Password",
        "secret",
        "SECRET",
        "token",
        "TOKEN",
        "pwd",
        "key",
        "KEY",
        "auth",
        "credential",
        "api_token",
        "API_TOKEN",
        "SESSION_TOKEN_ENV",
        "client_secret",
        "MY-PASSWORD",
        "db.pwd",
        "oauth_credentials",
        "pwd_hash_salt",
        "accessToken",
        "refreshToken",
        "apiKey",
        "clientSecret",
        "idToken",
        "apikey",
        "secretKey",
        "privateKey",
    ),
)
def test_credential_names_are_recognised(name: str):
    assert scanner.credential_name(name)


@pytest.mark.parametrize("name", ("password", "secret", "token", "pwd", "key"))
def test_a_bare_credential_word_assignment_is_now_reported(name: str):
    line = f'{name} = "abcdEFGH1234ijkl"\n'
    assert "literal" in [item[0] for item in scanner.scan_line(line)]


@pytest.mark.parametrize(
    "name",
    (
        "keyboard",
        "keyboard_layout",
        "author",
        "authors",
        "authorization",
        "tokenizer",
        "secretary",
        "passwordless",
        "keyspace",
        "",
        None,
        "x",
        "count",
        "index",
    ),
)
def test_non_credential_names_are_rejected(name: typing.Any):
    assert not scanner.credential_name(name)


@pytest.mark.parametrize(
    "line",
    (
        'keyboard = "abcdEFGH1234ijkl"\n',
        'author = "abcdEFGH1234ijkl"\n',
        'tokenizer = "abcdEFGH1234ijkl"\n',
        'secretary = "abcdEFGH1234ijkl"\n',
    ),
)
def test_a_word_that_merely_starts_with_a_credential_word_is_silent(line: str):
    assert scanner.scan_line(line) == []


def test_a_docstring_example_is_skipped():
    text = (
        "def f():\n"
        '    """Do a thing.\n'
        "\n"
        "    Returns:\n"
        '        {"ok": True, "totp_secret": "JBSWY3DPEHPK3PXP"}\n'
        '    """\n'
        "    return 1\n"
    )
    lines = list(scanner.iter_scannable_lines(text, ".py"))
    assert [number for number, _ in lines] == [1, 7]
    assert scanner.scan_line(lines[0][1]) == []
    assert scanner.scan_line(lines[1][1]) == []


def test_a_single_quoted_docstring_is_skipped_too():
    text = "def f():\n    '''Example: totp_secret = \"JBSWY3DPEHPK3PXP\"'''\n    return 1\n"
    assert [number for number, _ in scanner.iter_scannable_lines(text, ".py")] == [1, 3]


def test_a_comment_line_is_skipped():
    text = '# totp_secret = "JBSWY3DPEHPK3PXP"\nvalue = 1\n'
    assert [number for number, _ in scanner.iter_scannable_lines(text, ".py")] == [2]


def test_code_after_a_docstring_is_scanned_again():
    text = '"""Module."""\ntotp_secret = "JBSWY3DPEHPK3PXP"\n'
    assert [number for number, _ in scanner.iter_scannable_lines(text, ".py")] == [2]
    assert len(scanner.scan_line('totp_secret = "JBSWY3DPEHPK3PXP"\n')) == 1


def test_a_csharp_comment_is_skipped():
    text = '// token = "abcdEFGH1234ijkl"\nint x = 1;\n'
    assert [number for number, _ in scanner.iter_scannable_lines(text, ".cs")] == [2]


def test_markdown_is_not_skipped():
    text = '# Title\ntoken = "abcdEFGH1234ijkl"\n'
    assert [number for number, _ in scanner.iter_scannable_lines(text, ".md")] == [1, 2]


@pytest.mark.parametrize(
    "suffix,text",
    (
        (".py", '"""Module."""\nvalue = 1\n'),
        (".py", 'def f():\n    """Doc.\n\n    Example: totp_secret = "JBSWY3DPEHPK3PXP"\n    """\n    return 1\n'),
        (".py", "def f():\n    '''Doc.'''\n    return 1\n"),
        (".py", '# comment\ntotp_secret = "JBSWY3DPEHPK3PXP"\n'),
        (".py", 'x = 1\n"""a"""\ny = 2\n"""b"""\nz = 3\n'),
        (".cs", "// c\n/* d */\n* e\n/// f\ng();\n"),
        (".md", '# Title\ntoken = "abcdEFGH1234ijkl"\n'),
        (".sh", '# c\ntoken = "abcdEFGH1234ijkl"\n'),
        (".yml", '# c\ntoken: "abcdEFGH1234ijkl"\n'),
    ),
)
def test_precommit_guard_and_scanner_scannable_lines_agree(suffix: str, text: str):
    assert list(scanner.iter_scannable_lines(text, suffix)) == list(_guard.iter_scannable_lines(text, suffix))


@pytest.mark.parametrize(
    "name",
    (
        "client_id",
        "CLIENT_ID",
        "oauth_client_id",
        "OAuth-Client-Id",
        "app_id",
        "APP_ID",
        "application_id",
        "tenant_id",
        "project_id",
    ),
)
def test_a_public_client_id_is_recognised(name: str):
    assert scanner.is_public_identifier(name)


def test_the_codex_client_id_is_not_reported():
    keys = {"client_id": ("config.example.json", "")}
    line = 'CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"\n'
    assert scanner.scan_line(line, keys) == []


@pytest.mark.parametrize("name", ("client_id_secret", "CLIENT_ID_SECRET", "client_id_token", "app_id_key"))
def test_a_client_id_shaped_key_holding_a_secret_is_still_caught(name: str):
    assert not scanner.is_public_identifier(name)


def test_a_real_secret_is_never_excused_by_name():
    assert scanner.scan_line('CLIENT_SECRET = "Zq3Xk9Mv7Rt2Lp5Wb8"\n')


@pytest.mark.parametrize(
    "address", ("8.8.8.8", "64.90.20.244", "1.1.1.1", "203.0.114.1", "172.15.255.255", "172.32.0.1")
)
def test_routable_addresses_are_public(address: str):
    assert scanner.is_public_ip(address)


@pytest.mark.parametrize(
    "address",
    (
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.5",
        "169.254.1.1",
        "0.0.0.0",
        "172.16.0.1",
        "172.31.255.254",
        "192.0.2.1",
        "198.51.100.7",
        "203.0.113.9",
        "224.0.0.1",
        "255.255.255.255",
        "999.1.1.1",
        "1.2.3",
        "1.2.3.4.5",
        "",
        "not-an-ip",
    ),
)
def test_non_public_or_invalid_addresses_are_rejected(address: str):
    assert not scanner.is_public_ip(address)


@pytest.mark.parametrize(
    "line",
    (
        'BASE = "http://127.0.0.1:8080"\n',
        'PROXY = "http://192.168.1.10:7897"\n',
        'DOC = "http://192.0.2.1:80"\n',
    ),
)
def test_a_local_endpoint_in_source_is_silent(line: str):
    assert scanner.scan_line(line) == []


@pytest.mark.parametrize(
    "value",
    (
        "http://example.com/abcdefgh",
        "https://x.test/y",
        "/usr/local/share/thing",
        "\\server\\share\\thing",
        "./relative/path/thing",
        "../up/and/away/thing",
        "abcdefghijklmnop",
        "1234567890123456",
    ),
)
def test_urls_paths_and_homogeneous_values_are_rejected(value: str):
    assert not scanner.looks_like_secret_value(value)


def test_a_mixed_value_is_accepted():
    assert scanner.looks_like_secret_value("Zq3Xk9Mv7Rt2Lp5Wb8!")


@pytest.mark.parametrize(
    "line",
    (
        'password = "not-a-real-secret"\n',
        '"auth_state": "auth_state_failed"\n',
        'auth_state = "authenticated"\n',
        'client_secret = "unset"\n',
    ),
)
def test_prose_and_status_identifiers_are_not_secrets(line: str):
    assert scanner.scan_line(line) == []


def test_the_digit_requirement_is_what_rejects_status_values():
    assert scanner.credential_name("auth_state")
    assert not scanner.looks_like_secret_value("auth_state_failed")
    assert not scanner.looks_like_secret_value("authenticated")


@pytest.mark.parametrize(
    "value",
    (
        "your_token_here",
        "CHANGEME123456",
        "placeholder_value",
        "test_value_12345",
        "dummy_value_12345",
        "REDACTED1234567",
    ),
)
def test_placeholder_values_are_skipped(value: str):
    assert scanner.scan_line(f'api_token = "{value}"\n') == []


@pytest.mark.parametrize(
    "name",
    (
        "site_key",
        "PROBE_TOKEN",
        "PLACEHOLDER_KEY",
        "PERSISTENCE_KEY",
        "FALLBACK_TOKEN",
        "UNAUTHORIZED_TOKEN",
        "STATUS_TOKEN",
        "PASSWORDLESS_SIGNUP_CODE",
    ),
)
def test_non_credential_variable_names_are_skipped(name: str):
    assert scanner.scan_line(f'{name} = "abcdEFGH1234ijkl"\n') == []


def test_environment_variable_names_are_skipped_but_values_are_not():
    assert scanner.scan_line('SESSION_TOKEN_ENV = "PP_SESSION_TOKEN"\n') == []
    assert scanner.scan_line('SESSION_TOKEN = "abcdEFGH1234ijkl"\n')


def test_a_leak_exits_nonzero(tmp_path: Path):
    _tree(tmp_path, {"leak.js": 'const AUTH = "http://64.90.20.244:8443";\n'})
    buffer = io.StringIO()
    with _scanning(tmp_path, ["src"]), contextlib.redirect_stdout(buffer):
        code = scanner.main()
    assert code == 1
    assert "64.90.20.244" in buffer.getvalue()


def test_a_clean_tree_exits_zero(tmp_path: Path):
    _tree(tmp_path, {"clean.js": 'const AUTH = "http://127.0.0.1:8443";\n'})
    buffer = io.StringIO()
    with _scanning(tmp_path, ["src"]), contextlib.redirect_stdout(buffer):
        code = scanner.main()
    assert code == 0
    assert "total findings: 0" in buffer.getvalue()


def test_only_the_prefix_of_a_secret_is_printed(tmp_path: Path):
    secret = "Zq3Xk9Mv7Rt2Lp5Wb8"
    _tree(tmp_path, {"leak.py": f'password = "{secret}"\n'})
    buffer = io.StringIO()
    with _scanning(tmp_path, ["src"]), contextlib.redirect_stdout(buffer):
        scanner.main()
    output = buffer.getvalue()
    assert secret[:3] in output
    assert secret not in output


def test_the_scanned_tree_has_no_findings():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = scanner.main()
    assert code == 0, f"扫描器在本仓报了 findings:\n{buffer.getvalue()}"
    assert "total findings: 0" in buffer.getvalue()


def test_the_scanner_reports_how_many_example_keys_it_tracked():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        scanner.main()
    line = [row for row in buffer.getvalue().splitlines() if row.startswith("example keys tracked:")]
    assert line
    assert int(line[0].split(":")[1].strip()) > 0


def test_a_wrong_root_is_a_hard_error_not_a_silent_pass():
    with _scanning(os.path.join(os.sep, "definitely-not-here"), scanner.SCAN_DIRS):
        with pytest.raises(SystemExit) as raised:
            list(scanner.iter_files())
        assert "FATAL" in str(raised.value)


def test_the_scanner_itself_is_still_scanned():
    target = os.path.normpath(Path(__file__).resolve().parents[2] / "scripts" / "scan_hardcoded_secrets.py")
    assert target in list(scanner.iter_files())
