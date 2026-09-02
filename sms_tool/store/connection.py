"""connection submodule of the former storage.py (mechanical split, bodies unchanged)."""

from pathlib import Path
import os
import sqlite3
import threading

from ..config import ConfigInput
from ..config import current_config_data
from ..config import resolve_runtime_config
from ..paths import project_path
from ..paths import runtime_file
from .constants import EXTRA_COLUMNS


# Schema initialization is idempotent but NOT cheap: it replays the whole DDL
# (`CREATE TABLE IF NOT EXISTS` x3 + CREATE INDEX x4) and then runs three
# unconditional full-table UPDATEs in `_ensure_extra_columns`. Measured on the
# production database (42 MB, 795 rows):
#     init_database()                        ~100 ms per call
#     get_account_record() end-to-end        ~105 ms, i.e. ~99 % of it is this
#     795 accounts scanned                   ~83 s of pure schema replay
# It used to run from 17 call sites on hot paths (every upsert / lookup /
# checkpoint write), making account lookups O(N^2) in a batch run.
#
# The memo below is keyed by resolved database path, NOT by `runtime_config`,
# because `runtime_config` is usually a dict (unhashable) and therefore cannot
# be used with functools.lru_cache. Two different configs pointing at the same
# file correctly share one entry.
#
# Scope of the guarantee: once per (process, database path). Schema migrations
# are shipped with new code, and new code means a new process, so a stale memo
# cannot outlive the migration that would invalidate it. Passing `force=True`
# (or calling `reset_database_init_cache()`) bypasses it for scripts that
# deliberately rebuild or repair a database in-process.
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


# DELIBERATE REVERSE DEPENDENCY - do not "clean this up".
#
# `store` reaching back into `sms_tool.storage` (the back-compat shell) forms an
# import cycle, and that is intentional. The shell is the test suite's patch
# injection point: 7 test files do `patch.object(storage, "database_path", ...)`.
# Resolving through the shell re-reads the attribute on every call, so the patch
# still redirects internal callers. Binding the local `connection.database_path`
# instead would make every internal caller silently use the real database.


def _init_cache_key(path=None, runtime_config: ConfigInput = None) -> str:
    """Resolved, normalized path used to memoize schema initialization.

    Resolved through `sms_tool.storage.database_path` for the same reason
    `_connect()` does: the test suite patches that symbol, so binding the local
    `connection.database_path` here would silently key on the real database.
    """
    import sms_tool.storage as _storage

    raw = Path(path) if path else _storage.database_path(runtime_config)
    try:
        return os.path.normcase(str(Path(raw).resolve()))
    except OSError:
        return os.path.normcase(str(Path(raw).absolute()))


def reset_database_init_cache() -> None:
    """Forget every memoized schema initialization. Test/debug helper."""
    with _INIT_LOCK:
        _INITIALIZED.clear()


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
    # No PRAGMA busy_timeout here ON PURPOSE — verified 2026-09-01:
    #   sqlite3.connect(p)                  -> PRAGMA busy_timeout = 5000
    #   sqlite3.connect(p, timeout=30.0)    -> PRAGMA busy_timeout = 30000
    # i.e. CPython already sets busy_timeout from the `timeout` argument, whose
    # default is 5.0 s. A "busy_timeout is never set anywhere" grep hit is a
    # FALSE NEGATIVE - the value is applied in C, not in our source. Adding
    # `PRAGMA busy_timeout=5000` here would be a literal no-op.
    # Raising it was considered and rejected: there is no measurement showing
    # 5 s is insufficient, and a larger value only converts a loud failure into a
    # longer silent hang. Revisit only with evidence of real lock timeouts.
    #
    # WAL is likewise deliberately NOT enabled — measured on this 42 MB /
    # 795-row database with a writer looping init_database()'s three full-table
    # UPDATEs (each ~32 MB of changes, i.e. far past the 1000-page
    # autocheckpoint threshold, so every commit rewrites the database):
    #     delete (current)          : 41 write tx / 6 s,  reader p95   1.68 ms
    #     WAL                       : 26 write tx / 6 s,  reader p95   4.43 ms
    #     WAL + wal_autocheckpoint=0: 17 write tx / 6 s,  reader p95 121.36 ms
    # WAL is ~1.8x SLOWER with today's giant write transactions. It flips to a
    # clear win once those transactions are small — the same harness with
    # single-row UPDATEs gave WAL 57 reads vs delete 7, and the delete-mode
    # reader hit `database is locked` while the WAL reader never did.
    # => Re-enable `PRAGMA journal_mode=WAL` TOGETHER WITH the init_database()
    #    fix (audit-2026-09-01-round4 §8 item 6), not before it.
    return conn



def init_database(path=None, runtime_config: ConfigInput = None, force: bool = False):
    """Create/upgrade the schema. Runs at most once per (process, database path).

    Set `force=True` to re-run it anyway (rebuild/repair scripts, tests that
    delete and recreate the file at a path the process has already seen).
    """
    key = _init_cache_key(path, runtime_config)
    if not force and key in _INITIALIZED:
        return
    with _INIT_LOCK:
        # Re-check under the lock: another thread may have finished the very
        # same initialization while we were waiting for it.
        if not force and key in _INITIALIZED:
            return
        _init_database_uncached(path, runtime_config)
        # Only memoize on success - a failed (e.g. lock-contended) run must be
        # retried by the next caller instead of leaving the schema half-built.
        _INITIALIZED.add(key)


def _init_database_uncached(path=None, runtime_config: ConfigInput = None):
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_terminal_state ON accounts(terminal_state)")
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
        # Expression index for the `lower(email)=lower(?)` lookups used across
        # store/accounts.py, store/markers.py and account_lifecycle.py. Without
        # it the UNIQUE index on `email` is unusable for those queries (wrapping
        # the column in a function defeats it) and every lookup is a full table
        # scan - measured at 795 rows x ~41 KB raw_json (~31 MiB scanned per
        # lookup). Verified with EXPLAIN QUERY PLAN: SCAN accounts -> SEARCH
        # accounts USING INDEX idx_accounts_email_lower.
        # Requires SQLite >= 3.9; the bundled version is 3.43.1.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email_lower ON accounts(lower(email))")
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
    # Item #15: backfill terminal_state from the three deactivated signals
    # (status / error / raw_json) so list_terminal_remail_accounts can use an
    # indexed column instead of a lower(raw_json) LIKE full-table scan.
    # Idempotent: only rows still carrying the empty default are re-evaluated,
    # so after the first migration this touches 0 rows (no recurring scan).
    conn.execute("""
        UPDATE accounts SET terminal_state = CASE
          WHEN lower(status) IN ('account_deactivated','account_deatived')
            OR lower(error) LIKE '%account_deactivated%'
            OR lower(error) LIKE '%account_deatived%'
            OR lower(raw_json) LIKE '%account_deactivated%'
            OR lower(raw_json) LIKE '%account_deatived%'
          THEN 'account_deactivated' ELSE 'active' END
        WHERE terminal_state='' OR terminal_state IS NULL
    """)

