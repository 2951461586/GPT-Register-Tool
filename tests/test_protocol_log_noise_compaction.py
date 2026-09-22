"""The protocol lane's two noisiest log lines, and why they were noisy.

The lane reports through ``print()`` (stdout is the WPF IPC channel), so there is
no severity to filter on -- a diagnostic and a failure look identical.  Measured
2026-09-14 against a live ``backend_stdout.log``:

* ``client_auth_session_dump[...]`` -- 10.85% of the log.  Its summary changed in
  only **16 of 81** dumps, and ``top_keys`` had exactly **one** value in all 81,
  so the same 17 names were reprinted once per account for nothing new.  The
  changes that *are* real cost 795 characters each -- one 13-key account among
  17-key ones flips the cache twice -- so a change now prints as a diff of the
  keys that moved.
* ``Response: {"continue_url": ...}`` -- every line cut at exactly 600 chars (so
  never JSON-parseable), where the body prints the *same* callback URL twice and
  the second copy dies mid-query at character 182.

Both lines are worth having -- the ``signals`` answer "which auth mode did the
server pick", and the 600-char budget exists so ``userAlreadyExistsRecovery``
survives the cut (see the comment at ``registration_handlers.py``).  So the fix
is compaction, not deletion, and the tests below pin **both** halves: the line
got shorter *and* nothing was lost.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from sms_tool import auth_state, registration_handlers

_CALLBACK = "https://chatgpt.com/api/auth/callback/openai"
#: A single-use OAuth code, verbatim shape from the 15:13 run.
_CODE = "ac_qW1CQSpGsHDLlM9oQ56ZqgeNB0oCGqufE-i6b9xNX9I.HiXCT9zcq65gTfUn98ejw7PHRnek-6ISeRrxUBZAipQ"
_QUERY = f"code={_CODE}&scope=openid+email+profile+offline_access+model.request+model.read"

#: The 200 body from the live run, which is what made the line unusable: the
#: callback URL appears once at the top level and again under ``page.payload``.
_SUCCESS_200 = {
    "continue_url": f"{_CALLBACK}?{_QUERY}",
    "method": "GET",
    "page": {
        "type": "external_url",
        "backstack_behavior": "default",
        "payload": {"url": f"{_CALLBACK}?{_QUERY}"},
    },
}

#: The 17 keys the live signup flow returns, verbatim.
_SIGNUP_KEYS = [
    "app_name_enum",
    "auth_session_logging_id",
    "country_code_hint",
    "destination_app_name",
    "email",
    "email_verification_mode",
    "openai_client_id",
    "original_screen_hint",
    "passwordless_disabled",
    "passwordless_otp_from_password_redirect",
    "passwordless_signup_from_default_redirect",
    "promo",
    "requested_oauth_scopes",
    "session_id",
    "signup_mode",
    "signup_source",
    "username",
]

#: The 13 keys the server returns once this session already has a magic link
#: sent, verbatim from the 18:43 batch.  This is the shape that alternates with
#: ``_SIGNUP_KEYS`` and flips the dump cache twice for a single account.
_LOGIN_KEYS = [
    "app_name_enum",
    "auth_session_logging_id",
    "country_code_hint",
    "destination_app_name",
    "email",
    "openai_client_id",
    "original_screen_hint",
    "passwordless_login_magic_link_sent",
    "promo",
    "requested_oauth_scopes",
    "session_id",
    "signup_mode",
    "signup_source",
]


def _user_already_exists_400():
    """A 400 whose recovery object sits past the 300-char mark.

    That is the whole reason the budget is 600: at 300 the only place the server
    names the recovery action is cut off.
    """
    return {
        "error": {"code": "user_already_exists", "message": "x" * 320},
        "userAlreadyExistsRecovery": {"action": "continue_to_login"},
    }


def _sanitize(value):
    return str(value)


def _summary(keys=None, signals=None):
    return {
        "top_keys": ["checksum", "client_auth_session", "session_id"],
        "client_auth_session_keys": list(keys or _SIGNUP_KEYS),
        "signals": signals
        or {
            "client_auth_session": "<dict:17>",
            "client_auth_session.auth_session_logging_id": "[REDACTED](len=32)",
            "client_auth_session.email_verification_mode": "[REDACTED](len=19)",
            "client_auth_session.session_id": "[REDACTED](len=33)",
            "session_id": "[REDACTED](len=33)",
        },
    }


class CompactAuthDumpTextTests(unittest.TestCase):
    """``compact_auth_dump_text`` -- a repeat is a marker, a change is a diff."""

    def setUp(self):
        auth_state._LAST_DUMP_TEXT.clear()
        auth_state._SEEN_DUMP_SHAPES.clear()

    def test_the_first_summary_for_a_stage_prints_in_full(self):
        text = auth_state.compact_auth_dump_text("after_otp_send", _summary())

        self.assertIn("signup_mode", text)
        self.assertIn("email_verification_mode", text)

    def test_an_identical_summary_collapses_to_a_marker(self):
        summary = _summary()
        auth_state.compact_auth_dump_text("after_otp_send", summary)

        text = auth_state.compact_auth_dump_text("after_otp_send", summary)

        self.assertEqual(text, "17 keys unchanged")

    def test_a_reordered_key_list_is_not_a_change(self):
        """Same names, different order is not a shape change.

        It has to collapse like any other repeat: ``changed: {}`` reads as a
        change that carries nothing.
        """
        auth_state.compact_auth_dump_text("s", _summary(keys=["signup_mode", "username"]))

        text = auth_state.compact_auth_dump_text("s", _summary(keys=["username", "signup_mode"]))

        self.assertEqual(text, "2 keys unchanged")

    def test_a_changed_summary_prints_only_what_moved(self):
        """The change *is* the signal: passwordless magic link vs email mode.

        It used to reprint the whole summary, which is what made one alternating
        account cost 795 characters per flip.
        """
        auth_state.compact_auth_dump_text("after_otp_send", _summary())
        magic_link = _summary(
            keys=["passwordless_login_magic_link_sent", "signup_mode", "username"]
        )

        text = auth_state.compact_auth_dump_text("after_otp_send", magic_link)

        self.assertTrue(text.startswith("changed: "), text)
        self.assertIn("passwordless_login_magic_link_sent", text)
        # ``top_keys`` did not move, so it must not be reprinted.
        self.assertNotIn("top_keys", text)

    def test_an_alternating_shape_still_names_the_keys_it_moved(self):
        """A single 13-key account among 17-key ones flips the cache *twice*.

        Measured 2026-09-14 on the 18:43 batch: 10 of the log's 20 changes were
        the server walking back to a shape it had already shown.  The first
        sighting of each shape explains itself; the round trip after that used to
        cost a bare count.

        🔴 2026-09-16（P2-1）：那个裸计数**就是缺陷**——它把「回到已见过的形状」
        变成日志里不可回溯的一行，任何离线判据都会在那里漏数据（实测：本扫描
        第一版关联脚本把 19 个 dump 信号只识别出 3 个）。现在 ``seen before``
        仍然带 ``added`` / ``removed`` 键名，所以从首行全文起逐行走 diff 就能
        完整重建每个 run 的键集合。

        降噪没有撤销：``unchanged`` 分支（实测 41 行 dump 里 20 行）照旧塌成
        一行计数，省下的大头不动。
        """
        signup = _summary(keys=_SIGNUP_KEYS)
        login = _summary(keys=_LOGIN_KEYS)

        auth_state.compact_auth_dump_text("after_signup_state", signup)
        out = auth_state.compact_auth_dump_text("after_signup_state", login)
        back = auth_state.compact_auth_dump_text("after_signup_state", signup)
        out_again = auth_state.compact_auth_dump_text("after_signup_state", login)

        # First sighting of the 13-key shape: the diff carries the whole signal.
        self.assertTrue(out.startswith("changed: {"), out)
        self.assertLess(len(out), 400, out)
        self.assertIn("passwordless_login_magic_link_sent", out)

        # Round trips keep the marker *and* the names, so the log stays walkable.
        for text, key in ((back, "passwordless_login_magic_link_sent"),
                          (out_again, "passwordless_login_magic_link_sent")):
            self.assertIn("(seen before)", text)
            self.assertIn(key, text)
        self.assertIn('"added"', back)
        self.assertIn('"removed"', out_again)

    def test_a_seen_before_line_stays_bounded(self):
        """P2-1 的代价必须可量化：一行最多与首个 diff 同量级，不是全文重印。"""
        auth_state.compact_auth_dump_text("s", _summary(keys=_SIGNUP_KEYS))
        auth_state.compact_auth_dump_text("s", _summary(keys=_LOGIN_KEYS))

        back = auth_state.compact_auth_dump_text("s", _summary(keys=_SIGNUP_KEYS))

        self.assertIn("(seen before)", back)
        # 同时钉住「带了 diff」——只断言长度的话，裸计数那一版也会通过。
        self.assertIn('"added"', back)
        self.assertIn("passwordless_login_magic_link_sent", back)
        self.assertLess(len(back), 400, back)
        # The full summary it replaced is an order of magnitude longer.
        self.assertGreater(
            len(json.dumps(_summary(keys=_SIGNUP_KEYS), ensure_ascii=False)), 700
        )

    def test_a_shape_never_seen_before_still_prints_its_diff(self):
        """The seen-before shortcut must not swallow a genuinely new shape."""
        auth_state.compact_auth_dump_text("s", _summary(keys=_SIGNUP_KEYS))

        text = auth_state.compact_auth_dump_text("s", _summary(keys=_LOGIN_KEYS))

        self.assertTrue(text.startswith("changed: {"), text)
        self.assertIn("passwordless_login_magic_link_sent", text)

    def test_a_changed_signal_length_counts_as_a_change(self):
        """Values are redacted to a length, so the length *is* the observation."""
        auth_state.compact_auth_dump_text("s", _summary())
        shifted = _summary(
            signals={"client_auth_session.session_id": "[REDACTED](len=34)"}
        )

        text = auth_state.compact_auth_dump_text("s", shifted)

        self.assertIn("len=34", text)

    def test_each_stage_tracks_its_own_summary(self):
        """Otherwise the two stages would alternate and never collapse."""
        signup = _summary()
        otp = _summary(keys=_SIGNUP_KEYS[:-1])
        auth_state.compact_auth_dump_text("after_signup_state", signup)
        auth_state.compact_auth_dump_text("after_otp_send", otp)

        again_signup = auth_state.compact_auth_dump_text("after_signup_state", signup)
        again_otp = auth_state.compact_auth_dump_text("after_otp_send", otp)

        self.assertEqual(again_signup, "17 keys unchanged")
        self.assertEqual(again_otp, "16 keys unchanged")

    def test_a_non_dict_passes_through(self):
        self.assertEqual(auth_state.compact_auth_dump_text("s", "raw"), "raw")


class DumpKeyDiffTests(unittest.TestCase):
    """``_dump_key_diff`` -- the diff must be complete, not merely short.

    A diff that drops "this key disappeared" is worse than the full reprint it
    replaced: the operator reads the line to learn that the shape moved, and a
    silently omitted key reads as "still there".
    """

    def test_only_the_moved_keys_are_reported(self):
        diff = auth_state._dump_key_diff({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 9, "c": 3})

        self.assertEqual({"b": 9}, diff)

    def test_a_list_reports_added_and_removed_names(self):
        diff = auth_state._dump_key_diff(
            {"client_auth_session_keys": ["email_verification_mode", "session_id"]},
            {"client_auth_session_keys": ["passwordless_login_magic_link_sent", "session_id"]},
        )

        self.assertEqual(
            {
                "client_auth_session_keys": {
                    "added": ["passwordless_login_magic_link_sent"],
                    "removed": ["email_verification_mode"],
                }
            },
            diff,
        )

    def test_a_vanished_key_reports_none_not_absence(self):
        diff = auth_state._dump_key_diff(
            {"signals": {"client_auth_session.email_verification_mode": "[REDACTED](len=19)"}},
            {"signals": {}},
        )

        self.assertEqual(
            {"signals": {"client_auth_session.email_verification_mode": None}}, diff
        )

    def test_nested_dicts_diff_in_place(self):
        diff = auth_state._dump_key_diff(
            {"signals": {"x": 1, "y": 2}}, {"signals": {"x": 1, "y": 3}}
        )

        self.assertEqual({"signals": {"y": 3}}, diff)

    def test_an_unchanged_summary_diffs_to_nothing(self):
        self.assertEqual({}, auth_state._dump_key_diff({"a": 1}, {"a": 1}))

    def test_a_reordered_list_diffs_to_nothing(self):
        """Order is not shape, so an empty added/removed pair is not a diff."""
        self.assertEqual(
            {},
            auth_state._dump_key_diff(
                {"client_auth_session_keys": ["b", "a"]},
                {"client_auth_session_keys": ["a", "b"]},
            ),
        )


class DumpPrintWiringTests(unittest.TestCase):
    """The *printed* line is compact; the *returned* summary is not."""

    def setUp(self):
        auth_state._LAST_DUMP_TEXT.clear()
        auth_state._SEEN_DUMP_SHAPES.clear()

    def _dump(self, body, stage="after_otp_send"):
        response = Mock()
        response.status_code = 200
        response.json.return_value = body
        with patch.object(auth_state, "request_with_retry", return_value=response):
            with redirect_stdout(io.StringIO()) as captured:
                summary = auth_state.fetch_client_auth_session_dump(
                    Mock(), "https://auth.openai.com", {}, stage
                )
        return summary, captured.getvalue()

    def _body(self, extra_signals=0):
        session = {key: f"v{index}" for index, key in enumerate(_SIGNUP_KEYS)}
        # Names containing "session" are what ``_find_auth_dump_keys`` collects,
        # so these pad the ``signals`` block to a chosen length.
        for index in range(extra_signals):
            session[f"session_marker_{index:02d}"] = "abcdef"
        return {"checksum": "c" * 40, "session_id": "s" * 33, "client_auth_session": session}

    def test_a_repeated_summary_is_not_printed_twice(self):
        body = self._body()

        _, first = self._dump(body)
        _, second = self._dump(body)

        self.assertIn("signup_mode", first)
        self.assertIn("unchanged", second)
        self.assertNotIn("signup_mode", second)

    def test_the_printed_line_is_never_silently_truncated(self):
        """The observed maximum is 795 characters, so the old 800 cap was 5 bytes away.

        Asserting "not truncated" only means something if the summary is long
        enough to have been cut at 800 -- so that is asserted first.  Without it
        this test passes for the wrong reason (a 744-character summary fits under
        either cap, which is exactly how the first version of it missed).
        """
        body = self._body(extra_signals=4)
        full = json.dumps(auth_state.auth_dump_summary(body), ensure_ascii=False)
        self.assertGreater(len(full), 800, "probe is outside the discriminating region")
        self.assertLess(len(full), 1200, "probe would be cut by the new cap too")

        _, printed = self._dump(body)

        self.assertNotIn("unchanged", printed)
        self.assertTrue(printed.rstrip().endswith("}"), printed[-90:])

    def test_the_caller_still_gets_the_uncompacted_summary(self):
        """Compaction is a printing concern; the seam keeps its contract."""
        summary, _ = self._dump(self._body())

        self.assertIn("signup_mode", summary["client_auth_session_keys"])
        self.assertIn("client_auth_session.session_id", summary["signals"])


class DumpFailureDiagnosticsTests(unittest.TestCase):
    """A non-200 dump used to print the status code and nothing else.

    Measured 2026-09-14: a dead-end account's relogin printed
    ``client_auth_session_dump[existing_login_after_otp_validate_failed]: 404``
    on both attempts, so an empty body and a Cloudflare block page were
    indistinguishable -- exactly the case the dump exists to explain.
    """

    def setUp(self):
        auth_state._LAST_DUMP_TEXT.clear()
        auth_state._SEEN_DUMP_SHAPES.clear()

    def _dump_response(self, status_code, *, body=None, text=""):
        response = Mock()
        response.status_code = status_code
        response.text = text
        if body is None:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = body
        with patch.object(auth_state, "request_with_retry", return_value=response):
            with redirect_stdout(io.StringIO()) as captured:
                result = auth_state.fetch_client_auth_session_dump(
                    Mock(), "https://auth.openai.com", {}, "existing_login_after_otp_validate_failed"
                )
        return result, captured.getvalue()

    def test_a_non_json_failure_keeps_the_body_text(self):
        """For a Cloudflare block page, *what* came back is the diagnosis."""
        _, printed = self._dump_response(404, text="<html>Just a moment...</html>")

        self.assertIn("404", printed)
        self.assertIn("Just a moment", printed)

    def test_a_json_failure_prints_key_names_only(self):
        """The values are not needed to act on it; the keys are."""
        _, printed = self._dump_response(404, body={"detail": "Not Found", "request_id": "r-1"})

        self.assertIn("body_keys=", printed)
        self.assertIn("detail", printed)
        self.assertNotIn("Not Found", printed)

    def test_an_empty_failure_body_says_so(self):
        """``<empty>`` must be distinguishable from a page that had content."""
        _, printed = self._dump_response(404, text="")

        self.assertIn("body=<empty>", printed)

    def test_the_failure_body_does_not_leak_into_the_200_path(self):
        """Negative control: the success path keeps its exact contract."""
        _, printed = self._dump_response(
            200, body={"checksum": "c", "session_id": "s", "client_auth_session": {}}
        )

        self.assertNotIn("body=", printed)
        self.assertNotIn("body_keys=", printed)


class CreateAccountResponseLineTests(unittest.TestCase):
    """``_create_account_response_line`` -- same budget, spent on the decision."""

    def _line(self, status, data):
        return registration_handlers._create_account_response_line(status, data, _sanitize)

    def test_a_success_names_the_landing_instead_of_dumping_it_twice(self):
        line = self._line(200, _SUCCESS_200)

        self.assertIn("page.type=external_url", line)
        self.assertIn(f"continue_url={_CALLBACK}", line)
        self.assertNotIn("page.payload", line)
        self.assertLess(len(line), 200)

    def test_a_success_does_not_log_the_single_use_oauth_code(self):
        """The query string is a one-shot code, not a signal -- and it is huge."""
        line = self._line(200, _SUCCESS_200)

        self.assertNotIn(_CODE, line)
        self.assertNotIn("scope=openid", line)

    def test_a_failure_still_prints_the_raw_body(self):
        """The recovery object lives past the 300-char mark; that is why 600 exists."""
        line = self._line(400, _user_already_exists_400())

        self.assertIn("continue_to_login", line)
        self.assertIn("user_already_exists", line)
        self.assertLessEqual(len(line), 600)

    def test_an_unrecognised_success_falls_back_to_the_raw_body(self):
        """Better a long line than one that says nothing about the landing."""
        data = {"ok": True, "something_new": "value"}

        line = self._line(200, data)

        self.assertEqual(line, json.dumps(data, ensure_ascii=False)[:600])

    def test_a_success_that_also_carries_an_error_reports_it(self):
        data = {"page": {"type": "external_url"}, "error": {"code": "boom"}}

        line = self._line(200, data)

        self.assertIn("boom", line)

    def test_a_non_dict_body_is_dumped_raw(self):
        line = self._line(200, ["not", "a", "dict"])

        self.assertEqual(line, json.dumps(["not", "a", "dict"], ensure_ascii=False)[:600])

    def test_the_sanitizer_is_applied_on_every_path(self):
        """Both branches are operator-visible, so both must be redacted."""
        calls = []

        def spy(value):
            calls.append(value)
            return str(value)

        registration_handlers._create_account_response_line(200, _SUCCESS_200, spy)
        registration_handlers._create_account_response_line(400, _user_already_exists_400(), spy)

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
