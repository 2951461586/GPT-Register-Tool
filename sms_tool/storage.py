import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import CFG
from .paths import project_path, runtime_file


EXTRA_COLUMNS = {
    "payment_method": "TEXT DEFAULT 'paypal'",
    "paypal_status": "TEXT DEFAULT ''",
    "paypal_updated_at": "INTEGER DEFAULT 0",
    "paypal_completed_at": "INTEGER DEFAULT 0",
    "refresh_token_status": "TEXT DEFAULT ''",
    "refresh_token_updated_at": "INTEGER DEFAULT 0",
    "oauth_refresh_token": "TEXT DEFAULT ''",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KNOWN_EMAIL_DOMAINS = (
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "gmail.com",
)


def database_path(cfg=None):
    cfg = cfg or CFG
    configured = ((cfg.get("storage") or {}).get("sqlite_path") or "").strip()
    if configured:
        path = project_path(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return runtime_file(cfg, "accounts.sqlite3")


def _connect(path=None):
    db_path = Path(path) if path else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_database(path=None):
    conn = _connect(path)
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_updated_at ON accounts(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_success ON accounts(success)")
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


def _as_bool(value):
    return 1 if bool(value) else 0


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return 0


def _as_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _get(data, key, default=""):
    value = data.get(key, default) if isinstance(data, dict) else default
    return "" if value is None else value


def _nested(data, key):
    value = _get(data, key, {})
    return value if isinstance(value, dict) else {}


def _normalize_account_email(email):
    value = str(email or "").strip().lstrip("\ufeff")
    if "@+" in value:
        local, suffix = value.split("@+", 1)
        suffix_lower = suffix.lower()
        for domain in KNOWN_EMAIL_DOMAINS:
            if suffix_lower.endswith(domain) and len(suffix) > len(domain):
                alias = suffix[: -len(domain)]
                repaired = f"{local}+{alias}@{domain}"
                if EMAIL_RE.match(repaired):
                    return repaired.lower()
    if EMAIL_RE.match(value):
        domain = value.rsplit("@", 1)[1]
        if not domain.startswith("+"):
            return value.lower()
    return value.lower()


def _find_existing_account_email(conn, email):
    canonical = _normalize_account_email(email)
    if not canonical:
        return ""
    row = conn.execute(
        "SELECT email FROM accounts WHERE lower(email)=lower(?) LIMIT 1",
        (canonical,),
    ).fetchone()
    if row is not None:
        return row["email"]
    for row in conn.execute("SELECT email FROM accounts"):
        existing = str(row["email"] or "")
        if _normalize_account_email(existing) == canonical:
            return existing
    return ""


def _resolve_account_email(conn, email):
    canonical = _normalize_account_email(email)
    existing = _find_existing_account_email(conn, canonical)
    if not existing:
        return canonical
    if existing == canonical:
        return canonical
    try:
        conn.execute("UPDATE accounts SET email=? WHERE email=?", (canonical, existing))
        return canonical
    except sqlite3.IntegrityError:
        matched = _find_existing_account_email(conn, canonical)
        return matched or existing


def _nested_token(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _paypal_status(data, paypal):
    explicit = str(_get(data, "paypal_status")).strip()
    if explicit:
        return explicit
    explicit = str(_get(paypal, "status")).strip()
    if explicit:
        return explicit
    if _get(paypal, "error"):
        return "failed"
    if _get(paypal, "url"):
        return "link_ready"
    if paypal.get("ok"):
        return "ready"
    return "missing"


def _payment_method(data, paypal):
    value = (
        str(_get(data, "payment_method")).strip()
        or str(_get(paypal, "payment_method")).strip()
        or str(_get(paypal, "method")).strip()
    ).lower()
    if value in {"gopay", "go-pay", "go_pay"}:
        return "gopay"
    if value:
        return "paypal"
    if _get(paypal, "url"):
        return "paypal"
    return ""


def _oauth_refresh_token(data, auth_session):
    return (
        str(_get(data, "oauth_refresh_token")).strip()
        or str(_get(auth_session, "refreshToken")).strip()
        or str(_get(auth_session, "refresh_token")).strip()
        or _nested_token(auth_session, "session", "refresh_token")
        or _nested_token(auth_session, "session", "refreshToken")
    )


def _refresh_token_status(data, auth_session):
    explicit = str(_get(data, "refresh_token_status")).strip()
    if explicit:
        return explicit
    if _oauth_refresh_token(data, auth_session):
        return "oauth_present"
    if str(_get(data, "refresh_token")).strip():
        return "legacy_present"
    return "no_rt"


def _status(data, paypal, access_token, has_refresh_token=False):
    if data.get("success") is False and not has_refresh_token:
        return "failed" if data.get("error") else "pending"
    if not data.get("success") and data.get("error") and not has_refresh_token:
        return "failed"
    if access_token and paypal.get("ok"):
        return "paypal_ready"
    if access_token and paypal.get("error"):
        return "paypal_failed"
    if access_token:
        return "registered"
    return "pending"


def _success_value(data, access_token):
    if isinstance(data, dict) and "success" in data:
        return bool(data.get("success"))
    return bool(access_token)


def upsert_account(data, json_path=""):
    init_database()
    mailbox = _nested(data, "mailbox")
    purchase = _nested(data, "purchase")
    paypal = _nested(data, "paypal")
    auth_session = _nested(data, "auth_session")
    timing = _nested(data, "timing")
    pipeline_timing = _nested(data, "pipeline_timing")
    email = _normalize_account_email(_get(data, "email") or _get(mailbox, "email"))
    if not email:
        return False

    now = int(time.time())
    created_at = _as_int(_get(data, "created_at")) or now
    access_token = str(_get(data, "access_token"))
    paypal_status = _paypal_status(data, paypal)
    payment_method = _payment_method(data, paypal)
    oauth_refresh_token = _oauth_refresh_token(data, auth_session)
    refresh_token_status = _refresh_token_status(data, auth_session)
    has_refresh_token = refresh_token_status in {"oauth_present", "legacy_present"}
    status = _status(data, paypal, access_token, has_refresh_token=has_refresh_token)
    if has_refresh_token and _get(data, "error"):
        data = dict(data)
        data.pop("error", None)
    raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    row = {
        "email": email,
        "password": str(_get(data, "password")),
        "success": _as_bool(_success_value(data, access_token)),
        "status": status,
        "error": "" if has_refresh_token else str(_get(data, "error")),
        "session_token": str(_get(data, "session_token")),
        "access_token": access_token,
        "refresh_token": str(_get(data, "refresh_token")),
        "cookie_header": str(_get(data, "cookie_header")),
        "device_id": str(_get(data, "device_id")),
        "paypal_ok": _as_bool(paypal.get("ok")),
        "payment_method": payment_method,
        "paypal_url": str(_get(paypal, "url")),
        "paypal_status": paypal_status,
        "paypal_updated_at": _as_int(_get(data, "paypal_updated_at")) or now,
        "paypal_cs_id": str(_get(paypal, "cs_id")),
        "paypal_pm_id": str(_get(paypal, "pm_id")),
        "paypal_currency": str(_get(paypal, "currency")),
        "paypal_amount_due": _as_int(_get(paypal, "amount_due") or _get(paypal, "due")),
        "paypal_has_paypal": _as_bool(paypal.get("has_paypal")),
        "refresh_token_status": refresh_token_status,
        "refresh_token_updated_at": _as_int(_get(data, "refresh_token_updated_at")) or (now if oauth_refresh_token else 0),
        "oauth_refresh_token": oauth_refresh_token,
        "mailbox_provider": str(_get(mailbox, "provider") or _get(purchase, "provider")),
        "mailbox_source": str(_get(mailbox, "source") or _get(purchase, "source")),
        "mailbox_token": str(_get(mailbox, "token")),
        "purchase_id": str(_get(mailbox, "purchase_id") or _get(purchase, "purchase_id")),
        "project_name": str(_get(mailbox, "project_name") or _get(purchase, "project_name")),
        "price": str(_get(mailbox, "price") or _get(purchase, "price")),
        "purchase_total_cost": str(_get(mailbox, "purchase_total_cost") or _get(purchase, "total_cost")),
        "balance_after": str(_get(mailbox, "balance_after") or _get(purchase, "balance_after")),
        "json_path": str(json_path or _get(data, "json_path")),
        "timing_total_seconds": _as_float(_get(timing, "total_seconds")),
        "pipeline_total_seconds": _as_float(_get(pipeline_timing, "total_seconds")),
        "created_at": created_at,
        "updated_at": now,
        "raw_json": raw_json,
    }

    columns = list(row)
    placeholders = ", ".join(":" + column for column in columns)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in columns
        if column not in {"email", "created_at"}
    )
    sql = f"""
        INSERT INTO accounts ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(email) DO UPDATE SET {updates}
    """
    conn = _connect()
    try:
        row["email"] = _resolve_account_email(conn, email)
        conn.execute(sql, row)
        conn.commit()
    finally:
        conn.close()
    return True


def list_paypal_accounts(email=""):
    init_database()
    query = """
        SELECT email,access_token,payment_method,paypal_url,paypal_status,paypal_updated_at,refresh_token_status,json_path,updated_at
        FROM accounts
    """
    params = []
    if email:
        query += " WHERE lower(email)=lower(?)"
        params.append(email)
    query += " ORDER BY updated_at DESC"
    conn = _connect()
    try:
        if email:
            params[0] = _find_existing_account_email(conn, email) or _normalize_account_email(email)
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def get_paypal_url(email):
    rows = list_paypal_accounts(email)
    for row in rows:
        url = str(row.get("paypal_url") or "").strip()
        if _is_http_url(url):
            return url
    return ""


def get_account_record(email):
    init_database()
    conn = _connect()
    try:
        lookup_email = _find_existing_account_email(conn, email) or _normalize_account_email(email)
        row = conn.execute(
            "SELECT * FROM accounts WHERE lower(email)=lower(?)",
            (lookup_email,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def mark_paypal_status(email, status="completed"):
    init_database()
    now = int(time.time())
    conn = _connect()
    try:
        lookup_email = _find_existing_account_email(conn, email)
        if not lookup_email:
            return False
        row = conn.execute(
            "SELECT raw_json,json_path FROM accounts WHERE lower(email)=lower(?)",
            (lookup_email,),
        ).fetchone()
        if row is None:
            return False
        raw_json = row["raw_json"] or "{}"
        json_path = str(row["json_path"] or "").strip()
        try:
            data = json.loads(raw_json)
        except Exception:
            data = {}
        if json_path:
            try:
                file_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                if isinstance(file_data, dict):
                    data = {**file_data, **data}
            except Exception:
                pass
        paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
        paypal["status"] = status
        data["paypal"] = paypal
        data["paypal_status"] = status
        data["paypal_updated_at"] = now
        if status == "completed":
            data["paypal_completed_at"] = now
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            UPDATE accounts
            SET paypal_status=?, paypal_updated_at=?, updated_at=?, raw_json=?
            WHERE lower(email)=lower(?)
            """,
            (status, now, now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


def _update_session_json(path, data):
    try:
        target = Path(path)
        if target.exists():
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] Failed to update session JSON {path}: {e}")


def _is_http_url(value):
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def rebuild_from_session_dir(session_dir):
    init_database()
    count = 0
    for path in sorted(Path(session_dir).glob("session_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] Skip bad session JSON: {path} {e}")
            continue
        if upsert_account(data, json_path=str(path)):
            count += 1
    return count
