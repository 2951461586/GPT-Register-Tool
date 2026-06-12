import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import storage


class StorageDedupTests(unittest.TestCase):
    def test_upsert_reuses_existing_email_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({"email": "User@Example.com", "success": False, "error": "first"}))
                self.assertTrue(storage.upsert_account({"email": "user@example.com", "success": True, "access_token": "tok"}))

                conn = storage._connect()
                try:
                    rows = conn.execute("SELECT email,success,access_token,error FROM accounts").fetchall()
                finally:
                    conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "user@example.com")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["access_token"], "tok")
        self.assertEqual(rows[0]["error"], "")

    def test_upsert_clears_failed_error_when_refresh_token_is_acquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({
                    "email": "rt@example.com",
                    "success": False,
                    "error": "passwordless_email_otp_poll_timeout",
                }))
                self.assertTrue(storage.upsert_account({
                    "email": "rt@example.com",
                    "success": True,
                    "access_token": "at",
                    "oauth_refresh_token": "rt_TEST",
                    "refresh_token_status": "oauth_present",
                    "error": "passwordless_email_otp_poll_timeout",
                    "paypal": {"ok": True, "url": "https://example.com/pay"},
                }))

                conn = storage._connect()
                try:
                    row = conn.execute(
                        "SELECT status,error,refresh_token_status,oauth_refresh_token,paypal_status,raw_json FROM accounts WHERE email=?",
                        ("rt@example.com",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["status"], "paypal_ready")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["refresh_token_status"], "oauth_present")
        self.assertEqual(row["oauth_refresh_token"], "rt_TEST")
        self.assertEqual(row["paypal_status"], "link_ready")
        self.assertNotIn("passwordless_email_otp_poll_timeout", row["raw_json"])

    def test_upsert_does_not_treat_mailbox_refresh_token_as_codex_rt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({
                    "email": "mailbox-rt@example.com",
                    "success": True,
                    "access_token": "at",
                    "oauth_refresh_token": "M.C_FAKE_MAILBOX_TOKEN",
                    "refresh_token": "M.C_FAKE_MAILBOX_TOKEN",
                    "refresh_token_status": "oauth_present",
                }))

                conn = storage._connect()
                try:
                    row = conn.execute(
                        "SELECT refresh_token_status,oauth_refresh_token,refresh_token FROM accounts WHERE email=?",
                        ("mailbox-rt@example.com",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["refresh_token_status"], "no_rt")
        self.assertEqual(row["oauth_refresh_token"], "")
        self.assertEqual(row["refresh_token"], "M.C_FAKE_MAILBOX_TOKEN")

    def test_upsert_repairs_misplaced_alias_plus(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({"email": "CierraRiste7566@+oai01hotmail.com", "success": False}))

                conn = storage._connect()
                try:
                    row = conn.execute("SELECT email FROM accounts").fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["email"], "cierrariste7566+oai01@hotmail.com")

    def test_upsert_reuses_preexisting_misplaced_alias_plus_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                storage.init_database()
                conn = storage._connect()
                try:
                    now = 1779115200
                    conn.execute(
                        """
                        INSERT INTO accounts (email, success, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        ("cierrariste7566@+oai01hotmail.com", 0, now, now),
                    )
                    conn.commit()
                finally:
                    conn.close()

                self.assertTrue(storage.upsert_account({
                    "email": "cierrariste7566+oai01@hotmail.com",
                    "success": True,
                    "access_token": "tok",
                }))

                conn = storage._connect()
                try:
                    rows = conn.execute("SELECT email,success,access_token FROM accounts").fetchall()
                finally:
                    conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "cierrariste7566+oai01@hotmail.com")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["access_token"], "tok")

    def test_upsert_marks_pm_created_state_without_paypal_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({
                    "email": "pm@example.com",
                    "success": True,
                    "access_token": "at",
                    "paypal": {
                        "ok": True,
                        "link_type": "pm_created",
                        "pm_id": "pm_TESTPAYPAL",
                        "url": "",
                    },
                }))

                conn = storage._connect()
                try:
                    row = conn.execute(
                        "SELECT status,paypal_status,paypal_ok,paypal_url,paypal_pm_id FROM accounts WHERE email=?",
                        ("pm@example.com",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["status"], "paypal_pm_created")
        self.assertEqual(row["paypal_status"], "pm_created")
        self.assertEqual(row["paypal_ok"], 1)
        self.assertEqual(row["paypal_url"], "")
        self.assertEqual(row["paypal_pm_id"], "pm_TESTPAYPAL")

    def test_upsert_detects_upi_payment_method_from_currency_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.sqlite3"
            with patch.object(storage, "database_path", return_value=db_path):
                self.assertTrue(storage.upsert_account({
                    "email": "upi@example.com",
                    "success": True,
                    "access_token": "at",
                    "paypal": {
                        "ok": True,
                        "url": "https://pay.openai.com/c/pay/cs_live_UPI",
                        "currency": "inr",
                        "payment_method_types": ["card", "upi"],
                    },
                }))

                conn = storage._connect()
                try:
                    row = conn.execute(
                        "SELECT payment_method,paypal_status,paypal_url FROM accounts WHERE email=?",
                        ("upi@example.com",),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["payment_method"], "upi")
        self.assertEqual(row["paypal_status"], "link_ready")
        self.assertIn("cs_live_UPI", row["paypal_url"])


if __name__ == "__main__":
    unittest.main()
