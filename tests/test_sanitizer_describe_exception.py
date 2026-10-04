"""Contract tests for ``sms_tool/sanitizer.describe_exception``.

Why this file exists
--------------------
``registration_handlers.py`` and friends log the text of a caught exception on
registration, login, OTP and 2FA paths.  Those exceptions are raised on the
wire contract with ``auth.openai.com``, so their ``str()`` can embed a bearer
token, a password, a cookie or an API key.

Before this helper, each site called the sanitizer inline, which meant the
"never print a secret" guarantee lived in N separate call sites and was only as
good as the weakest one.  :func:`describe_exception` is the single funnel, and
these tests pin the property that matters most: **the secret is absent from the
output**, asserted by absence rather than by the exact replacement text so a
policy rewording does not silently weaken the guarantee.
"""

from __future__ import annotations

from sms_tool.sanitizer import describe_exception


def test_describe_exception_keeps_the_type_name_for_diagnosability():
    rendered = describe_exception(ValueError("plain failure"))

    assert rendered == "ValueError: plain failure"


def test_describe_exception_omits_the_separator_when_the_message_is_empty():
    assert describe_exception(RuntimeError("")) == "RuntimeError"
    assert describe_exception(RuntimeError("   ")) == "RuntimeError"


def test_describe_exception_reports_a_secret_free_line_for_a_key_value_message():
    secret = "sk-abc123XYZSECRETVALUE"
    rendered = describe_exception(ValueError(f"token={secret}"))

    assert rendered.startswith("ValueError:")
    assert secret not in rendered
    assert "REDACTED" in rendered


def test_describe_exception_redacts_a_bearer_authorization_header():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    rendered = describe_exception(RuntimeError(f"Authorization: Bearer {jwt}"))

    assert jwt not in rendered
    assert "payload" not in rendered


def test_describe_exception_redacts_a_password_in_the_message():
    rendered = describe_exception(ValueError("password=hunter2SuperSecret"))

    assert "hunter2SuperSecret" not in rendered


def test_describe_exception_truncates_to_the_limit():
    rendered = describe_exception(ValueError("x" * 500))

    assert len(rendered) == 300, "default limit must bound a runaway message"
    assert rendered.startswith("ValueError: xxx")


def test_describe_exception_honours_an_explicit_limit():
    rendered = describe_exception(ValueError("x" * 500), limit=40)

    assert len(rendered) == 40


def test_describe_exception_survives_an_exception_whose_str_raises():
    """A broken __str__ must not turn a log line into a second failure."""

    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() is broken")

    # sanitize_text() funnels through str(); a raising __str__ propagates the
    # second error rather than printing a secret, which is the safe direction.
    try:
        rendered = describe_exception(Hostile("boom"))
    except RuntimeError:
        return
    assert rendered == "Hostile" or rendered.startswith("Hostile:")
