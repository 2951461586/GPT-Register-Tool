"""Behaviour tests for ``sms_tool/mailbox_pool_writer.py`` (2026-09-22).

Why this exists
---------------
Microsoft and Google rotate the OAuth ``refresh_token`` on refresh. Both
providers stored the rotated value on the in-memory ``MailboxAccount`` and
nothing wrote it back, so the pool kept the *first imported* token. The next run
then presented a retired token, got ``invalid_grant``, and the mailbox was
declared dead -- with the ledger showing only a generic fetch failure.

The write-back is a **text edit on a supplier-authored file**, so the tests below
care mostly about what it must *not* touch: line endings, BOM, comments, blank
lines, and the columns this module does not understand. The strongest assertion
in this file is byte-level: after a write-back the file must equal the original
with exactly one substring replaced. That single assertion covers every
"helpfully reformatted the file" regression at once.

The refusal paths matter as much as the happy path: an ambiguous anchor must
leave the file **untouched** rather than rewrite a neighbouring column, because a
corrupted password column is silent and expensive.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import mailbox_gmail, mailbox_graph
from sms_tool.mailbox_pool_writer import (
    STATUS_AMBIGUOUS,
    STATUS_DISABLED,
    STATUS_NOT_FOUND,
    STATUS_NO_POOL,
    STATUS_UNCHANGED,
    STATUS_UPDATED,
    apply_rotation,
    persist_rotated_refresh_token,
    resolve_pool_path,
    writeback_enabled,
)

OLD_RT = "old-refresh-token-000000000000000000000000"
NEW_RT = "new-refresh-token-111111111111111111111111"

# One row per pool format that can carry a refresh token. The values are shapes,
# not credentials.
GRAPH_ROW = f"box@example.com---pw-1---{OLD_RT}---access-1\n"
CHATAI_ROW = f"box@example.com----pw-1----client-1----{OLD_RT}\n"
GMAIL_OAUTH_ROW = f"gmail://box@gmail.com----cid-1----sec-1----{OLD_RT}\n"
REMAIL_ROW = "remail://other@example.com---tok-1---ord-1---pid-1\n"


class _Mailbox:
    """Only the attributes the write-back reads."""

    def __init__(self, email="box@example.com", refresh_token=OLD_RT, source=""):
        self.email = email
        self.refresh_token = refresh_token
        self.source = source
        self.access_token = ""


class _PoolCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.pool = Path(self._dir.name) / "mailbox_tokens.txt"

    def write_pool(self, text: str, *, encoding: str = "utf-8", newline: str = "\n") -> bytes:
        """Write the pool with explicit newline control; return the exact bytes.

        ``Path.write_text`` is not used: on Windows it would rewrite ``\\n`` as
        ``\\r\\n`` and every byte-level assertion below would be testing the
        wrong file.
        """
        data = text.replace("\n", newline).encode(encoding)
        with open(self.pool, "wb") as handle:
            handle.write(data)
        return data

    def read_pool(self) -> bytes:
        return self.pool.read_bytes()

    def mailbox(self, **kwargs):
        return _Mailbox(source=str(self.pool), **kwargs)


# --------------------------------------------------------------- happy paths


class RewriteTests(_PoolCase):
    def test_graph_row_token_is_replaced_and_every_other_byte_survives(self):
        before = self.write_pool(GRAPH_ROW)
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_UPDATED)
        after = self.read_pool()
        self.assertEqual(after, before.replace(OLD_RT.encode(), NEW_RT.encode()))

    def test_chatai_row_token_is_replaced(self):
        self.write_pool(CHATAI_ROW)
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_UPDATED)
        self.assertIn(NEW_RT, self.read_pool().decode())
        self.assertNotIn(OLD_RT, self.read_pool().decode())

    def test_gmail_oauth_row_token_is_replaced(self):
        self.write_pool(GMAIL_OAUTH_ROW)
        status = persist_rotated_refresh_token(
            self.mailbox(email="box@gmail.com"), previous=OLD_RT, current=NEW_RT
        )
        self.assertEqual(status, STATUS_UPDATED)
        self.assertEqual(self.read_pool().decode(), f"gmail://box@gmail.com----cid-1----sec-1----{NEW_RT}\n")

    def test_untouched_neighbours_comments_and_blank_lines_survive(self):
        text = (
            "# supplier batch 2026-09-20\n"
            "\n"
            + REMAIL_ROW
            + GRAPH_ROW
            + "# trailing note\n"
        )
        self.write_pool(text)
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        after = self.read_pool().decode()
        self.assertIn("# supplier batch 2026-09-20\n", after)
        self.assertIn("# trailing note\n", after)
        self.assertIn(REMAIL_ROW, after)
        self.assertIn(f"---{NEW_RT}---access-1\n", after)

    def test_only_the_target_row_changes_when_the_same_email_appears_once(self):
        other = "second@example.com---pw-2---other-rt-22222222222222222222222222\n"
        before = self.write_pool(GRAPH_ROW + other)
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(self.read_pool(), before.replace(OLD_RT.encode(), NEW_RT.encode()))


# ------------------------------------------------------- byte-level fidelity


class FidelityTests(_PoolCase):
    def test_crlf_line_endings_are_not_converted(self):
        before = self.write_pool(GRAPH_ROW + REMAIL_ROW, newline="\r\n")
        self.assertEqual(before.count(b"\r\n"), 2)
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_UPDATED)
        after = self.read_pool()
        self.assertEqual(after, before.replace(OLD_RT.encode(), NEW_RT.encode()))
        self.assertEqual(after.count(b"\r\n"), 2, "CRLF must survive a write-back")
        self.assertEqual(after.count(b"\n"), 2, "no bare LF was introduced")

    def test_lf_line_endings_are_not_converted_to_crlf(self):
        before = self.write_pool(GRAPH_ROW + REMAIL_ROW, newline="\n")
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        after = self.read_pool()
        self.assertNotIn(b"\r", after)
        self.assertEqual(after, before.replace(OLD_RT.encode(), NEW_RT.encode()))

    def test_utf8_bom_is_preserved(self):
        before = self.write_pool(GRAPH_ROW, encoding="utf-8-sig")
        self.assertTrue(before.startswith(b"\xef\xbb\xbf"))
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        after = self.read_pool()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"), "BOM must not be dropped")
        self.assertEqual(after, before.replace(OLD_RT.encode(), NEW_RT.encode()))

    def test_a_missing_trailing_newline_stays_missing(self):
        before = self.write_pool(GRAPH_ROW.rstrip("\n"))
        self.assertFalse(before.endswith(b"\n"))
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        after = self.read_pool()
        self.assertFalse(after.endswith(b"\n"), "the writer must not append a newline")
        self.assertEqual(after, before.replace(OLD_RT.encode(), NEW_RT.encode()))

    def test_no_temp_file_is_left_behind(self):
        self.write_pool(GRAPH_ROW)
        persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        leftovers = [p.name for p in Path(self._dir.name).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "the atomic-replace temp file must be gone")


# ------------------------------------------------------------ refusal paths


class RefusalTests(_PoolCase):
    def test_unchanged_when_the_token_did_not_rotate(self):
        before = self.write_pool(GRAPH_ROW)
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=OLD_RT)
        self.assertEqual(status, STATUS_UNCHANGED)
        self.assertEqual(self.read_pool(), before)

    def test_empty_new_token_is_ignored(self):
        before = self.write_pool(GRAPH_ROW)
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current="")
        self.assertEqual(status, STATUS_UNCHANGED)
        self.assertEqual(self.read_pool(), before)

    def test_missing_anchor_leaves_the_file_untouched(self):
        before = self.write_pool(GRAPH_ROW)
        status = persist_rotated_refresh_token(
            self.mailbox(), previous="a-token-that-is-not-in-this-file", current=NEW_RT
        )
        self.assertEqual(status, STATUS_NOT_FOUND)
        self.assertEqual(self.read_pool(), before)

    def test_an_ambiguous_anchor_is_refused_rather_than_guessed(self):
        """The same token on two rows: we cannot tell which mailbox we rotated."""
        before = self.write_pool(GRAPH_ROW + f"second@example.com---pw-2---{OLD_RT}\n")
        status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_AMBIGUOUS)
        self.assertEqual(self.read_pool(), before, "an ambiguous write must change nothing")

    def test_anchor_on_another_mailboxs_row_is_refused(self):
        before = self.write_pool(REMAIL_ROW + GRAPH_ROW)
        status = persist_rotated_refresh_token(
            self.mailbox(email="second@example.com"), previous=OLD_RT, current=NEW_RT
        )
        self.assertEqual(status, STATUS_NOT_FOUND)
        self.assertEqual(self.read_pool(), before)

    def test_a_mailbox_without_a_source_is_not_written_anywhere(self):
        status = persist_rotated_refresh_token(_Mailbox(source=""), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_NO_POOL)

    def test_a_source_that_is_not_a_file_is_not_written(self):
        status = persist_rotated_refresh_token(
            _Mailbox(source=str(Path(self._dir.name) / "nope.txt")), previous=OLD_RT, current=NEW_RT
        )
        self.assertEqual(status, STATUS_NO_POOL)

    def test_an_empty_email_is_refused(self):
        before = self.write_pool(GRAPH_ROW)
        status = persist_rotated_refresh_token(self.mailbox(email=""), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_NOT_FOUND)
        self.assertEqual(self.read_pool(), before)


class EscapeHatchTests(_PoolCase):
    def test_the_env_switch_disables_the_write_back(self):
        before = self.write_pool(GRAPH_ROW)
        with mock.patch.dict(os.environ, {"MAILBOX_RT_WRITEBACK": "0"}):
            self.assertFalse(writeback_enabled())
            status = persist_rotated_refresh_token(self.mailbox(), previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_DISABLED)
        self.assertEqual(self.read_pool(), before)

    def test_the_switch_defaults_to_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAILBOX_RT_WRITEBACK", None)
            self.assertTrue(writeback_enabled())


class ResolvePoolPathTests(_PoolCase):
    def test_an_explicit_path_wins_over_the_source(self):
        self.write_pool(GRAPH_ROW)
        other = Path(self._dir.name) / "other.txt"
        other.write_bytes(b"x\n")
        self.assertEqual(resolve_pool_path(self.mailbox(), other), other)

    def test_a_missing_explicit_path_resolves_to_nothing(self):
        self.assertIsNone(resolve_pool_path(self.mailbox(), Path(self._dir.name) / "nope.txt"))


class ApplyRotationTests(_PoolCase):
    def test_apply_rotation_updates_memory_and_disk_together(self):
        self.write_pool(GRAPH_ROW)
        box = self.mailbox()
        status = apply_rotation(box, previous=OLD_RT, current=NEW_RT)
        self.assertEqual(status, STATUS_UPDATED)
        self.assertEqual(box.refresh_token, NEW_RT)
        self.assertIn(NEW_RT, self.read_pool().decode())


# ------------------------------------------------- end-to-end through providers


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeRequests:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class ProviderIntegrationTests(_PoolCase):
    """The seam that actually matters: a real refresh must persist the rotation.

    These call the production refresh functions with only the HTTP client faked,
    so a future edit that drops the write-back from either provider fails here.
    """

    def test_graph_refresh_persists_the_rotated_token(self):
        self.write_pool(GRAPH_ROW)
        box = self.mailbox()
        fake = _FakeRequests(_Response(payload={"access_token": "at-new", "refresh_token": NEW_RT}))
        with mock.patch.object(mailbox_graph, "curl_requests", fake):
            token = mailbox_graph.ms_oauth_refresh(box, {})
        self.assertEqual(token, "at-new")
        self.assertEqual(box.refresh_token, NEW_RT, "in-memory record updated")
        self.assertIn(NEW_RT, self.read_pool().decode(), "pool file updated")

    def test_graph_refresh_without_rotation_leaves_the_pool_alone(self):
        before = self.write_pool(GRAPH_ROW)
        box = self.mailbox()
        fake = _FakeRequests(_Response(payload={"access_token": "at-new"}))
        with mock.patch.object(mailbox_graph, "curl_requests", fake):
            mailbox_graph.ms_oauth_refresh(box, {})
        self.assertEqual(self.read_pool(), before)
        self.assertEqual(box.refresh_token, OLD_RT)

    def test_gmail_refresh_persists_the_rotated_token(self):
        self.write_pool(GMAIL_OAUTH_ROW)
        box = _Mailbox(
            email="box@gmail.com",
            refresh_token=OLD_RT,
            source=str(self.pool),
        )
        box.token = "cid-1"
        box.client_secret = "sec-1"
        fake = _FakeRequests(_Response(payload={"access_token": "at-new", "refresh_token": NEW_RT}))
        with mock.patch.object(mailbox_gmail, "curl_requests", fake):
            token = mailbox_gmail.refresh_gmail_access_token(box, {})
        self.assertEqual(token, "at-new")
        self.assertEqual(box.refresh_token, NEW_RT)
        self.assertIn(NEW_RT, self.read_pool().decode())

    def test_a_pool_write_failure_does_not_break_the_refresh(self):
        """Persistence is best-effort: a locked/unwritable pool must not abort a
        refresh whose token is already valid in memory."""
        self.write_pool(GRAPH_ROW)
        box = self.mailbox()
        fake = _FakeRequests(_Response(payload={"access_token": "at-new", "refresh_token": NEW_RT}))
        with mock.patch.object(mailbox_graph, "curl_requests", fake):
            with mock.patch(
                "sms_tool.mailbox_pool_writer._replace_anchor", side_effect=OSError("disk full")
            ):
                token = mailbox_graph.ms_oauth_refresh(box, {})
        self.assertEqual(token, "at-new")
        self.assertEqual(box.refresh_token, NEW_RT)


if __name__ == "__main__":
    unittest.main()
