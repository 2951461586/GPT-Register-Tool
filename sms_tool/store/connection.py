"""connection submodule of the former storage.py (mechanical split, bodies unchanged)."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import threading
from typing import Iterator, Protocol

from ..config import ConfigInput
from ..config import current_config_data
from ..config import resolve_runtime_config
from ..kernel.paths import project_path
from ..kernel.paths import runtime_file
from .constants import EXTRA_COLUMNS, SCHEMA_VERSION


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


# --------------------------------------------------------------- path seam
#
# `store` must not import `sms_tool.storage` (the back-compat shell). That edge
# existed only so the test suite could rebind `storage.database_path` and have
# internal callers observe it; it made a test convenience part of the production
# import graph. The seam is explicit now: every resolver asks the registered
# `DatabasePathProvider`, and tests redirect it with
# `set_database_path_provider()` or `database_path_override()`.
#
# The provider is a process-global rather than a constructor argument on purpose:
# 26 `_connect()` call sites and their callers would otherwise each grow a
# parameter, and this seam exists for tests and repair scripts -- production has
# exactly one resolver.


class DatabasePathProvider(Protocol):
    """Callable that resolves the SQLite file for one runtime config."""

    def __call__(self, runtime_config: ConfigInput = None) -> Path: ...


_PATH_PROVIDER: "DatabasePathProvider | None" = None


def default_database_path(cfg: ConfigInput = None) -> Path:
    """Configured `storage.sqlite_path`, else `<runtime>/accounts.sqlite3`."""
    cfg = resolve_runtime_config(cfg).data if cfg is not None else current_config_data()
    configured = ((cfg.get("storage") or {}).get("sqlite_path") or "").strip()
    if configured:
        path = project_path(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return runtime_file(cfg, "accounts.sqlite3")


def database_path(cfg: ConfigInput = None) -> Path:
    """Resolve the database file through the registered provider."""
    provider = _PATH_PROVIDER or default_database_path
    return Path(provider(cfg))


def database_path_provider() -> "DatabasePathProvider | None":
    """The installed provider, or ``None`` when the default resolver is used."""
    return _PATH_PROVIDER


def set_database_path_provider(provider: "DatabasePathProvider | None") -> None:
    """Install (or clear, with ``None``) the process-wide path provider."""
    global _PATH_PROVIDER
    _PATH_PROVIDER = provider


@contextmanager
def database_path_override(path) -> Iterator[Path]:
    """Point every store call at ``path`` for the duration of the block.

    Public on purpose: it replaces ``patch.object(storage, "database_path")``
    in tests and is the documented way for repair/rebuild scripts to work on a
    copy instead of the live database.
    """
    previous = _PATH_PROVIDER
    target = Path(path)
    set_database_path_provider(lambda runtime_config=None, _target=target: _target)
    try:
        yield target
    finally:
        set_database_path_provider(previous)


def _init_cache_key(path=None, runtime_config: ConfigInput = None) -> str:
    """Resolved, normalized path used to memoize schema initialization."""
    raw = Path(path) if path else database_path(runtime_config)
    try:
        return os.path.normcase(str(Path(raw).resolve()))
    except OSError:
        return os.path.normcase(str(Path(raw).absolute()))


def reset_database_init_cache() -> None:
    """Forget every memoized schema initialization. Test/debug helper."""
    with _INIT_LOCK:
        _INITIALIZED.clear()


def _connect(path=None, runtime_config: ConfigInput = None):
    db_path = Path(path) if path else database_path(runtime_config)
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
    # WAL is likewise deliberately NOT enabled. 2026-09-01 measured it as ~1.8x
    # SLOWER under init_database()'s three full-table UPDATEs and deferred it
    # until that replay left the hot path - which it since did (the memo above).
    # The deferred re-test ran 2026-10-20 on a synthetic 795-row / 38.5 MB
    # database using the production pattern (one connection per operation,
    # 3 repeats, medians):
    #     single-row commit     delete 62.6 tx/s    WAL 42.2 tx/s
    #     upsert-shaped write   delete 56.0 tx/s    WAL 44.4 tx/s   (rewrites raw_json)
    #     init_database(force)  ~165-176 ms in delete, WAL and WAL+NORMAL
    #     2 writers + 4 readers ~56-58 tx/s, zero `database is locked` in every mode
    #     reader p95 under write  delete 42.9 ms    WAL 29.2 ms
    # The 09-01 prediction ("small transactions flip WAL to a clear win") did NOT
    # reproduce: at one connection per operation WAL is ~1.3-1.5x slower and
    # nothing contends. It only wins once connections are reused - the same
    # harness at one connection per thread measured delete 105-117 tx/s vs WAL
    # 500-563 tx/s - so connection reuse is the lever here, not the journal mode.
    # Production volume is far below that crossover too: a batch of ~800 accounts
    # issues a few thousand transactions spread over hours (well under 1 tx/s),
    # so neither change is user-visible today. Revisit only with a measured
    # write-rate or lock-wait problem, and land connection reuse first if it ever
    # comes to that - enabling WAL alone would make today's pattern slower.
    # History: the 09-01 run (writer looping init_database's UPDATEs) measured
    # delete 41 tx / 6 s at reader p95 1.68 ms, WAL 26 tx / 6 s at 4.43 ms, and
    # WAL+wal_autocheckpoint=0 17 tx / 6 s at 121.36 ms.
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
        # Time-boxed egress/fingerprint leases (store/environment_ledger.py).
        #
        # The two UNIQUE indexes are PARTIAL, filtered to live leases.  That
        # predicate is the whole point: this install's pool is ten entries, so a
        # batch of fifty has to reuse exits -- a permanent-uniqueness index (the
        # shape the reference project uses) would refuse to allocate the
        # eleventh task.  Filtering on `state` turns the constraint into a
        # concurrency arbiter instead of a capacity wall; `expires_at` is checked
        # by sweep_expired_environment_leases() before every acquire, so any row
        # still marked 'leased' at insert time is genuinely live.
        #
        # The `<> ''` term keeps unkeyed rows out of the index entirely, so a
        # lease with no exit key cannot collide with another keyless one.
        #
        # `exit_ip` and `observed_fingerprint_key` are deliberately NOT unique:
        # they are only knowable after the registration ran (one measured, one
        # chosen by the driver mid-attempt), so they cannot gate anything --
        # record_environment_observations() reports a collision instead of
        # refusing a write that is already a fact.  Measured: making the observed
        # profile unique turns a collision report into an IntegrityError.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS environment_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exit_key TEXT NOT NULL DEFAULT '',
                exit_ip TEXT NOT NULL DEFAULT '',
                fingerprint_key TEXT NOT NULL DEFAULT '',
                observed_fingerprint_key TEXT NOT NULL DEFAULT '',
                account_ref TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'leased',
                reason TEXT NOT NULL DEFAULT '',
                leased_at INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL DEFAULT 0,
                released_at INTEGER NOT NULL DEFAULT 0,
                active_holders INTEGER NOT NULL DEFAULT 1,
                reuse_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_environment_ledger_live_exit
            ON environment_ledger(exit_key)
            WHERE state='leased' AND exit_key <> ''
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_environment_ledger_live_fingerprint
            ON environment_ledger(fingerprint_key)
            WHERE state='leased' AND fingerprint_key <> ''
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_environment_ledger_expires ON environment_ledger(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_environment_ledger_exit_ip ON environment_ledger(exit_ip)")
        # Stamp the schema shape. PRAGMA cannot be parameterised, and the value
        # is a module constant (not user data), so this interpolation is safe.
        # Written before commit() so a failed init never leaves a version stamp
        # on a half-built database -- matching the memoize-on-success rule above.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
