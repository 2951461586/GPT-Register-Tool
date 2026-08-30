"""connection submodule of the former storage.py (mechanical split, bodies unchanged)."""

from pathlib import Path
import sqlite3

from ..config import ConfigInput
from ..config import current_config_data
from ..config import resolve_runtime_config
from ..paths import project_path
from ..paths import runtime_file
from .constants import EXTRA_COLUMNS


def database_path(cfg: ConfigInput = None):
    cfg = resolve_runtime_config(cfg).data if cfg is not None else current_config_data()
    configured = ((cfg.get("storage") or {}).get("sqlite_path") or "").strip()
    if configured:
        path = project_path(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return runtime_file(cfg, "accounts.sqlite3")



def _connect(path=None, runtime_config: ConfigInput = None):
    # Resolve database_path through the public `sms_tool.storage` module so that
    # `patch.object(storage, "database_path", ...)` (used widely across the test
    # suite) still redirects internal callers after the module was split into the
    # `store` subpackage. Without this, _connect would bind the local
    # `connection.database_path` and ignore the monkeypatch.
    import sms_tool.storage as _storage

    db_path = Path(path) if path else _storage.database_path(runtime_config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn



def init_database(path=None, runtime_config: ConfigInput = None):
    conn = _connect(path, runtime_config)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password TEXT DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                status TEXT DEFAULT '',
                error TEXT DEFAULT '',
                session_token TEXT DEFAULT '',
                access_token TEXT DEFAULT '',
                refresh_token TEXT DEFAULT '',
                cookie_header TEXT DEFAULT '',
                device_id TEXT DEFAULT '',
                paypal_ok INTEGER NOT NULL DEFAULT 0,
                paypal_url TEXT DEFAULT '',
                paypal_cs_id TEXT DEFAULT '',
                paypal_pm_id TEXT DEFAULT '',
                paypal_currency TEXT DEFAULT '',
                paypal_amount_due INTEGER DEFAULT 0,
                paypal_has_paypal INTEGER NOT NULL DEFAULT 0,
                mailbox_provider TEXT DEFAULT '',
                mailbox_source TEXT DEFAULT '',
                mailbox_token TEXT DEFAULT '',
                purchase_id TEXT DEFAULT '',
                project_name TEXT DEFAULT '',
                price TEXT DEFAULT '',
                purchase_total_cost TEXT DEFAULT '',
                balance_after TEXT DEFAULT '',
                json_path TEXT DEFAULT '',
                timing_total_seconds REAL DEFAULT 0,
                pipeline_total_seconds REAL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                raw_json TEXT DEFAULT ''
            )
        """)
        _ensure_extra_columns(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registration_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT DEFAULT '',
                email TEXT DEFAULT '',
                state TEXT NOT NULL,
                error TEXT DEFAULT '',
                failure_class TEXT DEFAULT '',
                at_status_code INTEGER DEFAULT 0,
                token_hash TEXT DEFAULT '',
                token_iat INTEGER DEFAULT 0,
                token_exp INTEGER DEFAULT 0,
                token_age_seconds INTEGER DEFAULT 0,
                registration_country TEXT DEFAULT '',
                fingerprint_profile TEXT DEFAULT '',
                sentinel_version TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                detail_json TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_updated_at ON accounts(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_success ON accounts(success)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_audit_batch ON registration_audit(batch_id, state)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registration_checkpoints (
                email TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_checkpoints_state ON registration_checkpoints(state)")
        conn.commit()
    finally:
        conn.close()



def _ensure_extra_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    for name, definition in EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
    conn.execute("""
        UPDATE accounts
        SET paypal_status='link_ready'
        WHERE (paypal_status IS NULL OR paypal_status='')
          AND paypal_url IS NOT NULL
          AND paypal_url <> ''
    """)
    conn.execute("""
        UPDATE accounts
        SET refresh_token_status='no_rt'
        WHERE refresh_token_status IS NULL OR refresh_token_status=''
    """)
    conn.execute("""
        UPDATE accounts
        SET plan_type=lower(account_type)
        WHERE (plan_type IS NULL OR plan_type='' OR plan_type='unknown')
          AND account_type IS NOT NULL AND account_type <> ''
    """)

