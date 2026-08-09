from sms_tool.diagnostics import SanitizingTextIO, safe_print
from sms_tool.sanitizer import POLICY_SCHEMA, SENSITIVE_POLICY, sanitize, sanitize_command_args, sanitize_text


def test_sanitizer_removes_complete_token_secret_and_card_values():
    text = "access_token=eyJabcdefgh.ijklmnop.qrstuvwx refresh_token=rt_super-secret BA-abcDEF123456 totp_secret=JBSWY3DPEHPK3PXP card_number=4242424242424242"
    safe = sanitize_text(text)
    for fragment in ("eyJabcdefgh", "rt_super", "BA-abc", "JBSWY3", "424242"):
        assert fragment not in safe


def test_sanitizer_recurses_for_ipc_and_reports_without_prefixes():
    safe = sanitize({"access_token": "at-visible-prefix", "nested": {"totp_secret": "totp-visible-prefix", "error": "Bearer bearer-value"}, "cardNumber": "4111111111111111"})
    assert safe["access_token"] == "[REDACTED]"
    assert safe["nested"]["totp_secret"] == "[REDACTED]"
    assert safe["nested"]["error"] == "Bearer [REDACTED]"
    assert safe["cardNumber"] == "[REDACTED]"


def test_shared_sensitive_policy_schema_is_loaded():
    assert SENSITIVE_POLICY["schema"] == POLICY_SCHEMA
    assert any(item["name"] == "named_secret" for item in SENSITIVE_POLICY["text_patterns"])


def test_command_arguments_use_shared_sensitive_option_policy():
    assert sanitize_command_args(["--proxy", "http://user:pass@example:80", "--count", "2"]) == [
        "--proxy", "[REDACTED]", "--count", "2",
    ]
    assert sanitize_command_args(["--access-token=secret", "--email=user@example.com"])[0] == "--access-token=[REDACTED]"


def test_safe_print_sanitizes_operator_output(capsys):
    safe_print("proxy=http://user:pass@example:80")
    output = capsys.readouterr().out
    assert "user:pass" not in output
    assert "[REDACTED]" in output


def test_sanitizing_stdio_enforces_policy_for_legacy_prints():
    import io

    target = io.StringIO()
    stream = SanitizingTextIO(target)
    print("access_token=raw-secret", file=stream)
    assert target.getvalue() == "access_token=[REDACTED]\n"
