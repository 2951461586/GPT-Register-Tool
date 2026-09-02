"""Module-level constants extracted from the former storage.py (unchanged)."""

import re

# Stamped into SQLite's `PRAGMA user_version` by init_database() (2026-09-02,
# round 6). Before this existed there was no way to tell which schema shape a
# given accounts.sqlite3 was on: schema setup was three CREATE TABLE IF NOT
# EXISTS statements plus an ALTER TABLE loop, applied blindly on every start.
# The stamp does not migrate anything by itself -- it makes "which version is
# this file?" answerable, which is the prerequisite for writing a real
# migration. Bump it whenever EXTRA_COLUMNS or the CREATE TABLE statements
# change shape.
SCHEMA_VERSION = 1


EXTRA_COLUMNS = {
    "source": "TEXT DEFAULT ''",
    "register_method": "TEXT DEFAULT 'unknown'",
    "session_type": "TEXT DEFAULT 'unknown'",
    "plan_type": "TEXT DEFAULT 'unknown'",
    "batch_id": "TEXT DEFAULT ''",
    "registration_state": "TEXT DEFAULT ''",
    "registration_country": "TEXT DEFAULT ''",
    "totp_secret": "TEXT DEFAULT ''",
    "twofa_enrolled_at": "INTEGER DEFAULT 0",
    "twofa_enroll_error": "TEXT DEFAULT ''",
    "auth_session_logging_id": "TEXT DEFAULT ''",
    "device_id_generated_at": "INTEGER DEFAULT 0",
    "payment_method": "TEXT DEFAULT 'paypal'",
    "paypal_status": "TEXT DEFAULT ''",
    "paypal_updated_at": "INTEGER DEFAULT 0",
    "paypal_completed_at": "INTEGER DEFAULT 0",
    "refresh_token_status": "TEXT DEFAULT ''",
    "refresh_token_updated_at": "INTEGER DEFAULT 0",
    "oauth_refresh_token": "TEXT DEFAULT ''",
    "workspace_status": "TEXT DEFAULT ''",
    "workspace_id": "TEXT DEFAULT ''",
    "workspace_name": "TEXT DEFAULT ''",
    "workspace_switch_result": "TEXT DEFAULT ''",
    "workspace_updated_at": "INTEGER DEFAULT 0",
    "account_type": "TEXT DEFAULT ''",
    "quota_status": "TEXT DEFAULT ''",
    "terminal_state": "TEXT DEFAULT ''",
}


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


KNOWN_EMAIL_DOMAINS = (
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "gmail.com",
)

