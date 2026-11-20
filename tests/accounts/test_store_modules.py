"""Contract + persistence tests for the ``sms_tool/store`` subpackage.

The store was mechanically split out of the former storage.py. These tests lock
the pure normalization rules (the ones that decide account identity, token
shape, plan type and PayPal status) and the SQLite persistence path
(checkpoints round-trip, account upsert, quota marker) so a future edit cannot
silently change account-identity matching or drop a persisted field.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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

    def test_probe_is_token_invalid_only_accepts_definitive_verdicts(self):
        """``unknown`` is a transport failure, not a dead AT.

        A single owner for the rule: an over-eager ``True`` here would persist
        AT失效 for an account whose probe merely could not reach the exit.
        """
        self.assertTrue(normalize._probe_is_token_invalid({"status_code": 401}))
        self.assertTrue(normalize._probe_is_token_invalid({"status": "token_invalid"}))
        self.assertTrue(normalize._probe_is_token_invalid({"status": "Token_Invalid", "status_code": 0}))
        self.assertFalse(normalize._probe_is_token_invalid({"status": "unknown", "status_code": 0}))
        self.assertFalse(normalize._probe_is_token_invalid({"ok": True, "status": "active", "status_code": 200}))
        self.assertFalse(normalize._probe_is_token_invalid({"status_code": "401abc"}))
        self.assertFalse(normalize._probe_is_token_invalid(None))
        self.assertFalse(normalize._probe_is_token_invalid("token_invalid"))


class StoreConnectionTests(unittest.TestCase):
    def test_init_database_creates_tables(self):
        tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        with connection.database_path_override(tmp):
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
        connection.set_database_path_provider(lambda *args, _p=self._tmp, **kwargs: _p)
        self.addCleanup(connection.set_database_path_provider, None)
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
        connection.set_database_path_provider(lambda *args, _p=self._tmp, **kwargs: _p)
        self.addCleanup(connection.set_database_path_provider, None)
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

    def test_account_payload_merges_identity_context_from_raw_json(self):
        """``identity_context`` has no column, so only the payload form sees it.

        A probe built from ``get_account_record`` alone loses the fingerprint key,
        device id, proxy affinity and browser identity -- which made the single-row
        quota probe present the AT under a different identity than registration.
        """
        identity = {
            "fingerprint_key": "chrome136-win10",
            "device_id": "dev-123",
            "proxy_affinity": {"host": "proxy.example", "port": 8080, "country": "US"},
            "browser_identity": {"driver": "camoufox"},
        }
        accounts.upsert_account(
            {
                "email": "payload@example.com",
                "access_token": "at_payload",
                "identity_context": identity,
                "success": True,
            }
        )

        row = accounts.get_account_record("payload@example.com")
        self.assertNotIn("identity_context", row, "identity must not become a column")

        payload = accounts.get_account_payload("payload@example.com")
        self.assertEqual(payload["access_token"], "at_payload")
        self.assertEqual(payload["identity_context"], identity)
        self.assertEqual(accounts.get_account_payload("missing@example.com"), {})


class StoreMarkersTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "accounts.sqlite3"
        connection.set_database_path_provider(lambda *args, _p=self._tmp, **kwargs: _p)
        self.addCleanup(connection.set_database_path_provider, None)
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
            row = conn.execute("SELECT raw_json FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)).fetchone()
        finally:
            conn.close()
        self.assertNotIn("secret", row["raw_json"])

    def _set_stored_state(self, status, error):
        conn = connection._connect()
        try:
            conn.execute(
                "UPDATE accounts SET status=?, error=? WHERE lower(email)=lower(?)",
                (status, error, "a@b.com"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT status, error, raw_json FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        return row

    def test_verified_probe_clears_stale_error_on_registered_row(self):
        """A 200 probe retires an earlier failure, whatever its wording.

        Field report (2026-11-20): 账号测活 printed 正常 2，掉号 0，其他失败 0 while
        both rows still showed 失败 in the 状态 column. The rows carried
        ``status='registered'`` (``normalize._status`` derives that from the AT
        alone) plus a stale ``web_session_access_token_probe_failed:unknown``
        left by an earlier transient scan/relogin failure. The old SQL cleared
        ``error`` only when ``status='at_invalid'``, so nothing ever retired it.
        """
        row = self._set_stored_state("registered", "web_session_access_token_probe_failed:unknown")
        self.assertEqual(row["error"], "web_session_access_token_probe_failed:unknown")

        self.assertTrue(
            markers.mark_quota_status("a@b.com", "可用", {"ok": True, "status": "active", "status_code": 200})
        )

        conn = connection._connect()
        try:
            saved = conn.execute(
                "SELECT status, error, raw_json FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["status"], "registered")
        self.assertEqual(saved["error"], "")
        self.assertNotIn("error", json.loads(saved["raw_json"]), "raw_json must not keep the stale error")

    def test_failed_probe_keeps_recorded_error(self):
        self._set_stored_state("registered", "web_session_access_token_probe_failed:unknown")
        self.assertTrue(
            markers.mark_quota_status(
                "a@b.com", "401失效", {"ok": False, "status": "token_invalid", "status_code": 401}
            )
        )
        conn = connection._connect()
        try:
            saved = conn.execute("SELECT error FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["error"], "web_session_access_token_probe_failed:unknown")

    def test_verified_probe_keeps_confirmed_deactivation(self):
        """A deactivation verdict outranks a later probe and must stay visible."""
        self._set_stored_state("account_deactivated", "account_deactivated")
        markers.mark_quota_status("a@b.com", "可用", {"ok": True, "status": "active", "status_code": 200})
        conn = connection._connect()
        try:
            saved = conn.execute("SELECT error FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["error"], "account_deactivated")

    def test_definitive_401_probe_demotes_the_status_column(self):
        """The mirror of the stale-error bug: 401 must reach the 状态 column.

        Field report (2026-11-20): avawhite155379@outlook.com held a 401
        ``invalid_jwt`` probe while the row stayed ``status='registered'``,
        ``error=''`` -- so the grid rendered 已注册 for a dead AT. The 401 only
        ever reached ``quota_status``/``quota.last_result``, which no grid column
        reads, and nothing in this path wrote ``status`` at all.
        """
        self._set_stored_state("registered", "")
        self.assertTrue(
            markers.mark_quota_status(
                "a@b.com",
                "relogin_failed",
                {"ok": False, "status": "token_invalid", "status_code": 401, "error": "invalid_jwt"},
            )
        )
        conn = connection._connect()
        try:
            saved = conn.execute("SELECT status FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["status"], "at_invalid")

    def test_transport_unknown_probe_leaves_status_alone(self):
        """A probe that never got an answer must not mark the AT invalid."""
        self._set_stored_state("registered", "")
        markers.mark_quota_status(
            "a@b.com",
            "检测失败",
            {"ok": False, "status": "unknown", "status_code": 0, "error": "curl: (7) CONNECT tunnel failed"},
        )
        conn = connection._connect()
        try:
            saved = conn.execute("SELECT status FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["status"], "registered")

    def test_401_probe_does_not_downgrade_a_confirmed_deactivation(self):
        self._set_stored_state("account_deactivated", "account_deactivated")
        markers.mark_quota_status("a@b.com", "401失效", {"ok": False, "status": "token_invalid", "status_code": 401})
        conn = connection._connect()
        try:
            saved = conn.execute(
                "SELECT status, error FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["status"], "account_deactivated")
        self.assertEqual(saved["error"], "account_deactivated")

    def test_verified_probe_after_401_restores_registered(self):
        """The at_invalid -> registered half of the same rule (a relogin healed)."""
        self._set_stored_state("at_invalid", "invalid_jwt")
        markers.mark_quota_status("a@b.com", "可用", {"ok": True, "status": "active", "status_code": 200})
        conn = connection._connect()
        try:
            saved = conn.execute(
                "SELECT status, error FROM accounts WHERE lower(email)=lower(?)", ("a@b.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(saved["status"], "registered")
        self.assertEqual(saved["error"], "")


if __name__ == "__main__":
    unittest.main()
