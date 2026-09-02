"""Mailbox line-format parsing: one detector, no second copy.

Why this exists
---------------
Adding a mailbox provider used to mean touching five places, because the
six-branch provider chain was copied verbatim into a second function. The two
copies had already drifted apart: ``_parse_mailbox_token_file`` had no ``----``
(chatai) branch, so a chatai line inside a token file fell through to the ``---``
graph splitter and produced a silently corrupt account - ``email=a``,
``password='-b'``, ``refresh_token='-c'``, all looking valid enough to try to
use. Nothing raised; the account simply never worked.

Both copies now delegate to :func:`parse_mailbox_pool_line`. These tests pin
that structurally rather than case by case: for every supported line shape, all
three entry points must agree. A future second copy that drifts on any shape
fails here, without anyone having to remember the list.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from sms_tool.mailbox_parsers import (
    _parse_chatai_mailbox_file,
    _parse_mailbox_lines,
    _parse_mailbox_token_file,
    parse_mailbox_pool_line,
)

# One representative line per supported mailbox format. These are shapes, not
# credentials - the values are deliberately obvious placeholders.
SAMPLE_LINES = [
    "remail://user@example.com---tok-abc---ord-1---pid-9",
    "smailr://user@example.com---mbx-1234",
    "gmail://user@gmail.com----cid-1----sec-1----refresh-1",
    "cfworker://user@example.com",
    "user@liziai.cloud",
    "user@example.com--------pw-1----cid-1----refresh-1",   # chongzhi (8 hyphens)
    "user@example.com----pw-1----cid-1----refresh-1",       # chatai (4 hyphens)
    "user@example.com---pw-1---refresh-1---access-1",       # graph (3 hyphens)
    "user@icloud.com----https://mail.example/messages/secret/user%40icloud.com",
]

COMPARED_FIELDS = (
    "email", "password", "refresh_token", "access_token", "token",
    "provider", "auth_mode", "order_no", "purchase_id",
)


def _signature(account) -> tuple:
    return tuple(getattr(account, field, None) for field in COMPARED_FIELDS)


def _write(lines: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "mailbox.txt"
    path.write_text(lines, encoding="utf-8")
    return path


class MailboxLineFormatTests(unittest.TestCase):
    def test_every_sample_line_parses_to_the_provider_it_belongs_to(self):
        """Meta-check: a sample that no longer parses means the shape moved."""
        for line in SAMPLE_LINES:
            with self.subTest(line=line):
                account = parse_mailbox_pool_line(line, "sample", 1)
                self.assertIsNotNone(account, f"{line!r} no longer parses at all")
                self.assertTrue(account.email, f"{line!r} parsed without an email")
                self.assertTrue(account.provider)

    def test_all_three_entry_points_agree_on_every_line_shape(self):
        """The drift guard: one detector, three callers, identical results."""
        path = _write("\n".join(SAMPLE_LINES) + "\n")
        for loader in (_parse_mailbox_token_file, _parse_chatai_mailbox_file):
            with self.subTest(loader=loader.__name__):
                from_file = [_signature(a) for a in loader(path)]
                from_line = [
                    _signature(parse_mailbox_pool_line(line, str(path), index))
                    for index, line in enumerate(SAMPLE_LINES, start=1)
                ]
                self.assertEqual(from_file, from_line)

    def test_chatai_line_in_a_token_file_is_not_split_into_a_corrupt_record(self):
        """Regression for the bug the duplicated chain was hiding.

        Without the shared detector, this line reached the ``---`` graph
        splitter, which cut ``email----pw----cid----refresh`` into
        ``['email', '-pw', '-cid', '-refresh']`` - a plausible-looking account
        whose password and refresh token both start with a stray hyphen.
        """
        path = _write("user@example.com----pw-1----cid-1----refresh-1\n")
        (account,) = _parse_mailbox_token_file(path)
        self.assertEqual(account.provider, "chatai")
        self.assertEqual(account.password, "pw-1")
        self.assertEqual(account.refresh_token, "refresh-1")
        self.assertEqual(account.token, "cid-1")
        for field in ("password", "refresh_token", "token"):
            self.assertNotIn(
                "-", (getattr(account, field) or "")[:1],
                f"{field} was built from a mis-split chatai line",
            )

    def test_chatai_and_chongzhi_shapes_stay_distinguishable(self):
        """8 hyphens is chongzhi, 4 is chatai - they must not collide."""
        chongzhi = parse_mailbox_pool_line(
            "user@example.com--------pw-1----cid-1----refresh-1", "s", 1)
        chatai = parse_mailbox_pool_line(
            "user@example.com----pw-1----cid-1----refresh-1", "s", 1)
        self.assertEqual(chongzhi.provider, "chongzhi")
        self.assertEqual(chatai.provider, "chatai")

    def test_blank_and_comment_lines_are_skipped_by_every_loader(self):
        path = _write("\n# a comment\n   \nuser@example.com---pw-1---refresh-1\n")
        for loader in (_parse_mailbox_token_file, _parse_chatai_mailbox_file,
                       _parse_mailbox_lines):
            with self.subTest(loader=loader.__name__):
                records = loader(path)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].provider, "graph")

    def test_missing_file_yields_no_records(self):
        for loader in (_parse_mailbox_token_file, _parse_chatai_mailbox_file,
                       _parse_mailbox_lines):
            with self.subTest(loader=loader.__name__):
                self.assertEqual(loader(Path(tempfile.gettempdir()) / "does-not-exist.txt"), [])


if __name__ == "__main__":
    unittest.main()
