"""Refresh-token screening at the pool-import boundary (2026-09-22).

Why this exists
---------------
A supplier card can ship a *template* refresh token (``<refresh_token>``,
``your_refresh_token``, ``changeme``) or a filler run (``xxxxxxxx``). Importing
one produces a mailbox that looks healthy, is handed to a registration run, and
fails with ``invalid_grant`` -- which this repo classifies as "mailbox dead".
The placeholder is then indistinguishable from a genuinely retired account, so
the row is dropped by hand and the supplier is never told.

Two severities, deliberately asymmetric
---------------------------------------
``reject`` is reserved for values no working mailbox can carry, and the row is
dropped.  ``warn`` covers "shorter than this provider's real tokens" and the row
is **kept** -- dropping a working mailbox costs more than the placeholder it
might catch, and the short-token case has never actually been observed here.
``test_a_short_token_is_kept_not_dropped`` pins that asymmetry, so a later
"tighten the rule" edit has to argue with a test instead of a comment.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sms_tool.mailbox_parsers import (
    REFRESH_TOKEN_MIN_LENGTH,
    parse_mailbox_pool_line,
    refresh_token_finding,
)

# Long enough to clear every floor, and obviously synthetic.
GOOD_MS_RT = "sample-ms-refresh-" + "Ab3" * 40
GOOD_GOOGLE_RT = "1//sample-google-refresh-" + "Qw9" * 20
SHORT_RT = "refresh-1"


def _pool(*lines: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "mailbox_tokens.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _parse_one(line: str):
    return parse_mailbox_pool_line(line, "pool.txt", 1)


# ------------------------------------------------------------- the predicate


class RefreshTokenFindingTests(unittest.TestCase):
    def test_a_realistic_microsoft_token_is_accepted(self):
        self.assertEqual(refresh_token_finding(GOOD_MS_RT, provider="microsoft"), ("", ""))

    def test_a_realistic_google_token_is_accepted(self):
        self.assertEqual(refresh_token_finding(GOOD_GOOGLE_RT, provider="google"), ("", ""))

    def test_an_empty_value_reports_nothing(self):
        """Emptiness is the caller's existing malformed-line check, not this one."""
        self.assertEqual(refresh_token_finding("", provider="microsoft"), ("", ""))
        self.assertEqual(refresh_token_finding(None, provider="microsoft"), ("", ""))

    def test_template_brackets_are_rejected(self):
        for value in ("<refresh_token>", "[REFRESH_TOKEN]", "{refresh_token}"):
            with self.subTest(value=value):
                severity, reason = refresh_token_finding(value, provider="microsoft")
                self.assertEqual(severity, "reject")
                self.assertIn("template", reason)

    def test_placeholder_markers_are_rejected(self):
        for value in ("your_refresh_token", "CHANGEME", "replace_me", "placeholder-value",
                      "dummy_token", "refresh_token_here"):
            with self.subTest(value=value):
                severity, _ = refresh_token_finding(value, provider="microsoft")
                self.assertEqual(severity, "reject")

    def test_repeated_character_filler_is_rejected(self):
        for value in ("x" * 200, "0" * 900, "ab" * 300):
            with self.subTest(value=value[:12]):
                severity, reason = refresh_token_finding(value, provider="microsoft")
                self.assertEqual(severity, "reject")
                self.assertIn("filler", reason)

    def test_a_short_token_warns_and_names_the_measured_length(self):
        severity, reason = refresh_token_finding(SHORT_RT, provider="microsoft")
        self.assertEqual(severity, "warn")
        self.assertIn(str(len(SHORT_RT)), reason)
        self.assertIn(str(REFRESH_TOKEN_MIN_LENGTH["microsoft"]), reason)

    def test_google_has_a_lower_floor_than_microsoft(self):
        """Google refresh tokens are ~100 chars, Microsoft's ~800; one shared
        floor would either reject every Google token or catch nothing."""
        self.assertLess(
            REFRESH_TOKEN_MIN_LENGTH["google"], REFRESH_TOKEN_MIN_LENGTH["microsoft"]
        )
        token = "1//" + "aB9" * 15
        self.assertEqual(refresh_token_finding(token, provider="google")[0], "")
        self.assertEqual(refresh_token_finding(token, provider="microsoft")[0], "warn")

    def test_an_unknown_provider_uses_the_low_default_floor(self):
        severity, _ = refresh_token_finding("aB3dE6gH9kL2nP5rS8uV1xY4zC7" * 2, provider="")
        self.assertEqual(severity, "")


# ------------------------------------------------------ behaviour in the parser


class ParserScreeningTests(unittest.TestCase):
    def test_a_template_token_row_is_dropped(self):
        for line in (
            f"box@example.com---pw---<refresh_token>---at",
            f"box@example.com----pw----cid----your_refresh_token",
            f"gmail://box@gmail.com----cid----sec----changeme",
        ):
            with self.subTest(line=line):
                self.assertIsNone(_parse_one(line))

    def test_a_filler_token_row_is_dropped(self):
        self.assertIsNone(_parse_one(f"box@example.com---pw---{'x' * 300}---at"))

    def test_a_short_token_is_kept_not_dropped(self):
        account = _parse_one(f"box@example.com---pw---{SHORT_RT}---at")
        self.assertIsNotNone(account, "a short token must warn, not delete the mailbox")
        self.assertEqual(account.refresh_token, SHORT_RT)

    def test_a_good_token_row_still_parses_with_every_field_intact(self):
        account = _parse_one(f"box@example.com---pw---{GOOD_MS_RT}---at")
        self.assertEqual(account.email, "box@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(account.refresh_token, GOOD_MS_RT)
        self.assertEqual(account.access_token, "at")
        self.assertEqual(account.provider, "graph")

    def test_rows_without_a_refresh_token_are_untouched(self):
        """The screen must not reach the service-token providers."""
        for line, provider in (
            ("remail://box@example.com---tok---ord---pid", "remail"),
            ("smailr://box@example.com---mbx", "smailr"),
            ("cfworker://box@example.com", "cfworker"),
        ):
            with self.subTest(line=line):
                account = _parse_one(line)
                self.assertIsNotNone(account)
                self.assertEqual(account.provider, provider)


class MessageTests(unittest.TestCase):
    def test_the_rejection_message_names_the_file_line_and_measured_length(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            parse_mailbox_pool_line("box@example.com---pw---<refresh_token>---at", "pool.txt", 7)
        message = buffer.getvalue()
        self.assertIn("pool.txt:7", message)
        self.assertIn("len=15", message)
        self.assertIn("Skip", message)

    def test_the_warning_message_names_the_file_and_line(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            parse_mailbox_pool_line(f"box@example.com---pw---{SHORT_RT}---at", "pool.txt", 3)
        message = buffer.getvalue()
        self.assertIn("pool.txt:3", message)
        self.assertIn("Suspicious", message)


if __name__ == "__main__":
    unittest.main()
