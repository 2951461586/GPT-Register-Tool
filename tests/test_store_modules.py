"""Contract + persistence tests for the ``sms_tool/store`` subpackage.

The store was mechanically split out of the former storage.py. These tests lock
the pure normalization rules (the ones that decide account identity, token
shape, plan type and PayPal status) and the SQLite persistence path
(checkpoints round-trip, account upsert, quota marker) so a future edit cannot
silently change account-identity matching or drop a persisted field.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.store import accounts, checkpoints, connection, constants, markers, normalize  # noqa: E402


class StoreConstantsTests(unittest.TestCase):
    def test_extra_columns_cover_core_fields(self):
        for column in ("payment_method", "paypal_status", "oauth_refresh_token", "batch_id"):
            self.assertIn(column, constants.EXTRA_COLUMNS)

    def test_known_email_domains(self):
        self.assertIn("gmail.com", constants.KNOWN_EMAIL_DOMAINS)
        self.assertIn("outlook.com", constants.KNOWN_EMAIL_DOMAINS)

    def test_email_re_rejects_obvious_non_emails(self):
        self.assertIsNone(constants.EMAIL_RE.match("not-an-email"))
        self.assertIsNotNone(constants.EMAIL_RE.match("user@example.com"))


class StoreNormalizeTests(unittest.TestCase):
    def test_normalize_account_email_lowercases_and_trims(self):
        self.assertEqual(normalize._normalize_account_email("  User@Example.COM "), "user@example.com")

    def test_normalize_account_email_repairs_plus_alias(self):
        # "user+alias@gmail.com" -> canonical "user+alias@gmail.com" stays valid
        self.assertEqual(
            normalize._normalize_account_email("user+alias@gmail.com"),
            "user+alias@gmail.com",
        )

    def test_normalize_account_email_repairs_bare_plus_suffix(self):
        # A "+" stuck before the domain is repaired to a proper alias form.
        self.assertEqual(
            normalize._normalize_account_email("user+@gmail.com"),
            "user+@gmail.com",
        )

    def test_looks_codex_refresh_token(self):
        self.assertTrue(normalize._looks_codex_refresh_token("rt_abc123"))
        # JWT-shaped OAuth refresh token: 3 base64url segments, exactly 2 dots.
        self.assertTrue(normalize._looks_codex_refresh_token("a" * 30 + "." + "b" * 30 + "." + "c" * 30))
        self.assertFalse(normalize._looks_codex_refresh_token(""))
        self.assertFalse(normalize._looks_codex_refresh_token("[REDACTED]"))
        self.assertFalse(normalize._looks_codex_refresh_token("M.C_something"))

    def test_normalize_account_type(self):
        self.assertEqual(normalize._normalize_account_type("ChatGPT Team"), "team")
        self.assertEqual(normalize._normalize_account_type("Plus plan"), "plus")
        self.assertEqual(normalize._normalize_account_type("K12 edu"), "k12")
        self.assertEqual(normalize._normalize_account_type("random"), "")

    def test_paypal_status_from_url(self):
        self.assertEqual(normalize._paypal_status({}, {"url": "https://paypal.example/x"}), "link_ready")

    def test_paypal_status_from_error(self):
        self.assertEqual(normalize._paypal_status({}, {"error": "boom"}), "failed")

    def test_is_http_url(self):
        self.assertTrue(normalize._is_http_url("https://example.com/x"))
        self.assertFalse(normalize._is_http_url("ftp://example.com"))
        self.assertFalse(normalize._is_http_url("not a url"))


class StoreConnectionTests(unittest.TestCase):
    def test_init_database_creates_tables(self):
        tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        with mock.patch("sms_tool.storage.database_path", return_value=tmp):
            connection.init_database()
            conn = connection._connect()
            try:
                tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
        self.assertIn("accounts", tables)
        self.assertIn("registration_checkpoints", tables)
        self.assertIn("registration_audit", tables)


class StoreCheckpointsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        patcher = mock.patch("sms_tool.storage.database_path", return_value=self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        connection.init_database()

    def test_save_get_clear_round_trip(self):
        self.assertTrue(checkpoints.save_registration_checkpoint("a@b.com", "active", {"step": "sms"}))
        loaded = checkpoints.get_registration_checkpoint("A@B.COM")
        self.assertEqual(loaded.get("state"), "active")
        self.assertEqual(loaded.get("payload", {}).get("step"), "sms")
        self.assertEqual(loaded.get("payload", {}).get("email"), "a@b.com")

        self.assertTrue(checkpoints.clear_registration_checkpoint("a@b.com"))
        self.assertEqual(checkpoints.get_registration_checkpoint("a@b.com"), {})

    def test_save_is_idempotent_on_same_email(self):
        checkpoints.save_registration_checkpoint("a@b.com", "active", {"n": 1})
        checkpoints.save_registration_checkpoint("a@b.com", "paid", {"n": 2})
        loaded = checkpoints.get_registration_checkpoint("a@b.com")
        self.assertEqual(loaded.get("state"), "paid")
        self.assertEqual(loaded.get("payload", {}).get("n"), 2)


class StoreAccountsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        patcher = mock.patch("sms_tool.storage.database_path", return_value=self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        connection.init_database()

    def test_upsert_account_persists_normalized_email(self):
        ok = accounts.upsert_account({"email": "A@B.com", "access_token": "at_x", "success": True})
        self.assertTrue(ok)
        conn = connection._connect()
        try:
            row = conn.execute(
                "SELECT email, status FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "a@b.com")

    def test_find_existing_account_email_is_case_insensitive(self):
        conn = connection._connect()
        try:
            conn.execute(
                "INSERT INTO accounts(email, success, created_at, updated_at) VALUES(?,1,?,?)",
                ("a@b.com", 1, 1),
            )
            conn.commit()
            self.assertEqual(normalize._find_existing_account_email(conn, "A@B.COM"), "a@b.com")
        finally:
            conn.close()


class StoreMarkersTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        patcher = mock.patch("sms_tool.storage.database_path", return_value=self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        connection.init_database()
        conn = connection._connect()
        conn.execute(
            "INSERT INTO accounts(email, success, created_at, updated_at, raw_json) VALUES(?,1,?,?,?)",
            ("a@b.com", 1, 1, "{}"),
        )
        conn.commit()
        conn.close()

    def test_mark_quota_status_updates_row(self):
        self.assertTrue(markers.mark_quota_status("a@b.com", "ok", {"checked": True}))
        conn = connection._connect()
        try:
            row = conn.execute(
                "SELECT quota_status, raw_json FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["quota_status"], "ok")
        self.assertIn("quota", row["raw_json"])

    def test_mark_quota_status_redacts_credentials_in_result(self):
        result = {"access_token": "secret", "plan": "team"}
        self.assertTrue(markers.mark_quota_status("a@b.com", "ok", result))
        conn = connection._connect()
        try:
            row = conn.execute(
                "SELECT raw_json FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertNotIn("secret", row["raw_json"])


if __name__ == "__main__":
    unittest.main()
