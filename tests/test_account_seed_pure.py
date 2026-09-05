"""Behaviour tests for ``sms_tool/account_seed.py`` (2026-09-03, round 7).

72 lines, **zero test files import it** (AST audit) -- yet it is on the money
path: ``payment_auth.py`` (2 call sites), ``payment_batch.py`` (2) and
``paypal/orchestrator.py`` (imported as ``_load_seed`` / ``_extract_access_token``)
all go through it. It is the seam between payment adapters and persisted account
state: whatever comes out of ``load_account_seed`` is what gets charged.

Why it matters: every failure mode here is silent. A corrupt session file, a
malformed ``raw_json`` column, a record with no ``json_path`` -- all of them come
back as "no data" rather than an error, and the payment flow proceeds with an
empty seed instead of stopping.

Patch seam: ``get_account_record`` is imported at module level
(``from .storage import get_account_record``), so
``patch.object(account_seed, "get_account_record")`` is effective. ``read_json``
is exercised against **real temp files** -- no patching, so BOM handling and the
"file is a JSON array" case are real rather than simulated.

Quirks pinned, not fixed:

* **Two different token extractors exist and disagree.** This module's
  ``extract_access_token`` recognises 3 sources and *requires nested values to be
  ``str``*; ``k12_identity._extract_access_token`` recognises 12 and stringifies
  whatever it finds. ``paypal/orchestrator`` uses this one, ``k12_client`` and
  ``workspace_scan`` use the other. Same account, potentially different answers.
* **The top-level token is stripped, nested ones are not.**
* **The on-disk JSON file outranks the ``raw_json`` column** (``{**raw_data, **data}``).
* **``setdefault`` keys off presence, not truthiness** -- a seed file holding
  ``email: ""`` keeps the empty string and never gets the DB value.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sms_tool import account_seed


def _write(path: Path, payload, encoding: str = "utf-8") -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding=encoding)
    return path


#: The seven keys ``load_account_seed`` backfills from the SQLite record. Whenever
#: a record is returned at all, **all seven land in the result** (empty string if
#: the column was absent) -- so "seed contains nothing" never means ``{}``.
DEFAULT_KEYS = ("email", "access_token", "cookie_header", "oauth_refresh_token",
                "refresh_token", "registration_country", "batch_id")
EMPTY_DEFAULTS = {key: "" for key in DEFAULT_KEYS}


class _TmpDirCase(unittest.TestCase):
    """Real files on disk -- ``read_json`` is not stubbed anywhere."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)


class LoadAccountSeedTests(_TmpDirCase):
    """``load_account_seed`` -- which source wins, and what stays silent."""

    def _seed(self, record=None, *, session_file: str = "", email: str = ""):
        """Run it with ``get_account_record`` replaced by a scripted recorder.

        ⚠️ ``record`` only takes effect if an ``email`` is passed too: production
        does ``get_account_record(email) if email else {}``, so a record supplied
        with a blank email is never consulted. Supplying a record implies one.
        """
        if record is not None and not email:
            email = "seed@example.test"
        recorder = mock.Mock(side_effect=[record] if record is not None else None)
        with mock.patch.object(account_seed, "get_account_record", recorder):
            data, path = account_seed.load_account_seed(email=email, session_file=session_file)
        return data, path, recorder

    # --- the explicit-file shortcut ----------------------------------------

    def test_explicit_session_file_wins_and_short_circuits_the_database(self):
        """When a file is named, the SQLite index is never consulted at all."""
        path = _write(self.tmp / "s.json", {"email": "a@b.c"})
        data, returned, recorder = self._seed(session_file=str(path), email="ignored@b.c")
        self.assertEqual(data, {"email": "a@b.c"})
        self.assertEqual(returned, str(path))
        recorder.assert_not_called()

    def test_explicit_session_file_skips_the_record_defaults_too(self):
        """The ``setdefault`` block only runs on the database path, so an explicit
        file is handed back verbatim -- no synthetic email/token fields."""
        path = _write(self.tmp / "s.json", {})
        data, _, _ = self._seed(session_file=str(path))
        self.assertEqual(data, {})

    def test_explicit_session_file_that_is_unreadable_gives_an_empty_seed(self):
        missing = self.tmp / "missing.json"
        data, returned, _ = self._seed(session_file=str(missing))
        self.assertEqual(data, {})
        self.assertEqual(returned, str(missing))

    # --- the database path -------------------------------------------------

    def test_no_email_and_no_file_gives_an_empty_seed_and_an_empty_path(self):
        data, path, recorder = self._seed()
        self.assertEqual(data, {})
        self.assertEqual(path, "")
        recorder.assert_not_called()

    def test_record_with_no_usable_file_still_yields_the_defaults(self):
        data, path, _ = self._seed({"email": "a@b.c", "access_token": "tok"})
        self.assertEqual(path, "")
        self.assertEqual(data["email"], "a@b.c")
        self.assertEqual(data["access_token"], "tok")

    def test_json_path_from_the_record_is_read_and_returned(self):
        path = _write(self.tmp / "acct.json", {"user_id": "u1"})
        data, returned, _ = self._seed({"json_path": str(path)})
        self.assertEqual(data, {**EMPTY_DEFAULTS, "user_id": "u1"})
        self.assertEqual(returned, str(path))

    def test_json_path_that_does_not_exist_is_silent(self):
        gone = self.tmp / "gone.json"
        data, returned, _ = self._seed({"json_path": str(gone)})
        self.assertEqual(data, EMPTY_DEFAULTS)
        self.assertEqual(returned, str(gone),
                         "the path is echoed back even though nothing was read")

    def test_a_record_always_yields_all_seven_default_keys(self):
        """⚠️ Pinned: the ``setdefault`` block runs whenever ``record`` is truthy,
        so an empty record is not a no-op -- it still injects seven empty keys."""
        data, _, _ = self._seed({"unused": "x"})
        self.assertEqual(sorted(data), sorted(DEFAULT_KEYS))
        self.assertEqual(data, EMPTY_DEFAULTS)

    # --- merge precedence ---------------------------------------------------

    def test_file_contents_outrank_the_raw_json_column(self):
        """``data = {**raw_data, **data}`` -- the file is applied last, so it wins."""
        path = _write(self.tmp / "acct.json", {"email": "from-file"})
        data, _, _ = self._seed({"json_path": str(path),
                                 "raw_json": json.dumps({"email": "from-db"})})
        self.assertEqual(data["email"], "from-file")

    def test_raw_json_fills_keys_the_file_is_missing(self):
        path = _write(self.tmp / "acct.json", {"a": 1})
        data, _, _ = self._seed({"json_path": str(path), "raw_json": json.dumps({"b": 2})})
        self.assertEqual(data, {**EMPTY_DEFAULTS, "a": 1, "b": 2})

    def test_raw_json_is_used_alone_when_there_is_no_file(self):
        data, _, _ = self._seed({"raw_json": json.dumps({"email": "from-db"})})
        self.assertEqual(data["email"], "from-db")

    def test_malformed_raw_json_is_swallowed(self):
        data, _, _ = self._seed({"raw_json": "{not json"})
        self.assertEqual(data, EMPTY_DEFAULTS)

    def test_raw_json_that_is_not_an_object_is_ignored(self):
        """A JSON array / scalar in the column is discarded, not merged."""
        for raw in ("[1, 2, 3]", '"just a string"', "42", "null"):
            with self.subTest(raw=raw):
                data, _, _ = self._seed({"raw_json": raw})
                self.assertEqual(data, EMPTY_DEFAULTS)

    # --- setdefault semantics ----------------------------------------------

    def test_record_defaults_never_overwrite_what_the_seed_already_has(self):
        path = _write(self.tmp / "acct.json", {"email": "from-file"})
        data, _, _ = self._seed({"json_path": str(path), "email": "from-db"})
        self.assertEqual(data["email"], "from-file")

    def test_existing_empty_string_is_not_backfilled(self):
        """⚠️ Pinned: ``setdefault`` keys off **presence**, not truthiness, so an
        empty ``email`` in the seed file stays empty and the DB value is dropped."""
        path = _write(self.tmp / "acct.json", {"email": ""})
        data, _, _ = self._seed({"json_path": str(path), "email": "from-db"})
        self.assertEqual(data["email"], "")

    def test_every_documented_default_key_is_backfilled(self):
        keys = ("email", "access_token", "cookie_header", "oauth_refresh_token",
                "refresh_token", "registration_country", "batch_id")
        data, _, _ = self._seed({key: f"value-{key}" for key in keys})
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(data[key], f"value-{key}")

    def test_missing_record_fields_backfill_as_empty_strings(self):
        """``record.get(k, "")`` -- an absent column becomes '', not None."""
        data, _, _ = self._seed({"email": "a@b.c"})
        self.assertEqual(data["cookie_header"], "")
        self.assertEqual(data["batch_id"], "")


class ReadJsonTests(_TmpDirCase):
    def test_reads_a_json_object(self):
        self.assertEqual(account_seed.read_json(_write(self.tmp / "a.json", {"a": 1})), {"a": 1})

    def test_handles_a_utf8_bom(self):
        path = _write(self.tmp / "a.json", {"a": 1}, encoding="utf-8-sig")
        self.assertEqual(account_seed.read_json(path), {"a": 1})

    def test_missing_file_gives_an_empty_dict(self):
        self.assertEqual(account_seed.read_json(self.tmp / "gone.json"), {})

    def test_malformed_json_gives_an_empty_dict(self):
        path = self.tmp / "a.json"
        path.write_text("{nope", encoding="utf-8")
        self.assertEqual(account_seed.read_json(path), {})

    def test_non_object_json_is_returned_as_is(self):
        """⚠️ Pinned: the return annotation says ``dict`` but a top-level array or
        scalar comes back unchanged. Callers that index the result will break."""
        for payload in ([1, 2, 3], "just a string", 42, None, True):
            with self.subTest(payload=payload):
                self.assertEqual(account_seed.read_json(_write(self.tmp / "a.json", payload)),
                                 payload)

    def test_a_directory_gives_an_empty_dict(self):
        self.assertEqual(account_seed.read_json(self.tmp), {})


class ExtractAccessTokenTests(unittest.TestCase):
    """Only 3 sources, and strict about types -- unlike ``k12_identity``'s 12."""

    def test_top_level_access_token_wins(self):
        self.assertEqual(account_seed.extract_access_token({"access_token": "T"}), "T")

    def test_top_level_value_is_stringified_and_stripped(self):
        """⚠️ The top level runs through ``str(...)``, so a *number* is accepted."""
        self.assertEqual(account_seed.extract_access_token({"access_token": 12345}), "12345")
        self.assertEqual(account_seed.extract_access_token({"access_token": "  T  "}), "T")

    def test_falsy_top_level_values_are_treated_as_absent(self):
        for value in ("", None, 0, False):
            with self.subTest(value=value):
                self.assertEqual(
                    account_seed.extract_access_token({"access_token": value,
                                                       "auth_session": {"accessToken": "T"}}),
                    "T",
                )

    def test_auth_session_camel_case_comes_first(self):
        data = {"auth_session": {"accessToken": "camel", "access_token": "snake"}}
        self.assertEqual(account_seed.extract_access_token(data), "camel")

    def test_auth_session_snake_case_is_the_second_choice(self):
        self.assertEqual(
            account_seed.extract_access_token({"auth_session": {"access_token": "snake"}}),
            "snake",
        )

    def test_nested_session_level_is_the_last_resort(self):
        self.assertEqual(
            account_seed.extract_access_token({"auth_session": {"session": {"accessToken": "dc"}}}),
            "dc",
        )
        self.assertEqual(
            account_seed.extract_access_token({"auth_session": {"session": {"access_token": "ds"}}}),
            "ds",
        )

    def test_shallower_source_beats_a_deeper_one(self):
        data = {"auth_session": {"accessToken": "shallow", "session": {"accessToken": "deep"}}}
        self.assertEqual(account_seed.extract_access_token(data), "shallow")

    def test_nested_values_must_be_strings(self):
        """⚠️ Pinned asymmetry: the top level is stringified, nested levels are
        **type-checked**. A numeric nested token is skipped entirely."""
        data = {"auth_session": {"accessToken": 12345, "access_token": "snake"}}
        self.assertEqual(account_seed.extract_access_token(data), "snake")

    def test_nested_values_are_not_stripped(self):
        """⚠️ Pinned: only the top-level token goes through ``.strip()``."""
        self.assertEqual(
            account_seed.extract_access_token({"auth_session": {"accessToken": "  padded  "}}),
            "  padded  ",
        )

    def test_the_two_nested_levels_behave_identically(self):
        """⚠️ Every rule above must hold at *both* nesting levels.

        The two levels are written as two near-identical loops, so a fix applied
        to one and not the other is invisible until each level is asserted on its
        own. Mutations A22 (camel/snake order) and A24 (stray ``.strip()``) both
        survived a first pass that only exercised the outer level.
        """
        outer = {"auth_session": {"accessToken": "camel", "access_token": "snake"}}
        inner = {"auth_session": {"session": {"accessToken": "camel", "access_token": "snake"}}}
        self.assertEqual(account_seed.extract_access_token(outer), "camel")
        self.assertEqual(account_seed.extract_access_token(inner), "camel")

        outer = {"auth_session": {"accessToken": "  padded  "}}
        inner = {"auth_session": {"session": {"accessToken": "  padded  "}}}
        self.assertEqual(account_seed.extract_access_token(outer), "  padded  ")
        self.assertEqual(account_seed.extract_access_token(inner), "  padded  ")

        outer = {"auth_session": {"accessToken": 12345, "access_token": "snake"}}
        inner = {"auth_session": {"session": {"accessToken": 12345, "access_token": "snake"}}}
        self.assertEqual(account_seed.extract_access_token(outer), "snake")
        self.assertEqual(account_seed.extract_access_token(inner), "snake")

    def test_empty_nested_string_falls_through(self):
        data = {"auth_session": {"accessToken": "", "access_token": "snake"}}
        self.assertEqual(account_seed.extract_access_token(data), "snake")

    def test_non_dict_auth_session_is_ignored(self):
        for value in ("nope", 42, [1], None):
            with self.subTest(value=value):
                self.assertEqual(account_seed.extract_access_token({"auth_session": value}), "")

    def test_non_dict_nested_session_is_ignored(self):
        self.assertEqual(account_seed.extract_access_token({"auth_session": {"session": "n"}}), "")

    def test_nothing_recognised_gives_an_empty_string(self):
        self.assertEqual(account_seed.extract_access_token({}), "")
        self.assertEqual(account_seed.extract_access_token({"auth_session": {}}), "")

    def test_missing_top_level_key_is_not_an_error(self):
        """``data.get("access_token")`` -- absent is the same as empty."""
        self.assertEqual(account_seed.extract_access_token({"other": 1}), "")


if __name__ == "__main__":
    unittest.main()
