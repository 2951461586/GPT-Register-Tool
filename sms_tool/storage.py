import json
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from .account_models import AccountSessionModel
from .config import ConfigInput, current_config_data, resolve_runtime_config
from .paths import project_path, runtime_file


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
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KNOWN_EMAIL_DOMAINS = (
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "gmail.com",
)


# ── Schema & Connection ─────────────────────────────────────────────────────

def database_path(cfg: ConfigInput = None):
    cfg = resolve_runtime_config(cfg).data if cfg is not None else current_config_data()
    configured = ((cfg.get("storage") or {}).get("sqlite_path") or "").strip()
    if configured:
        path = project_path(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return runtime_file(cfg, "accounts.sqlite3")


def _connect(path=None, runtime_config: ConfigInput = None):
    db_path = Path(path) if path else database_path(runtime_config)
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


# ── Data Normalization Helpers ───────────────────────────────────────────────

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
    value = data.get(key, default) if isinstance(data, Mapping) else default
    return "" if value is None else value


def _nested(data, key):
    value = _get(data, key, {})
    return value if isinstance(value, Mapping) else {}


def _nested_field(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    return current


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


# ── Field Extraction ─────────────────────────────────────────────────────────

def _nested_token(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, Mapping):
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
    if paypal.get("ok") and str(_get(paypal, "pm_id")).startswith("pm_"):
        return "pm_created"
    if paypal.get("ok"):
        return "ready"
    return "missing"


def _payment_method(data, paypal):
    from .payment_link_manager import normalize_payment_method

    value = (
        str(_get(data, "payment_method")).strip()
        or str(_get(paypal, "payment_method")).strip()
        or str(_get(paypal, "method")).strip()
    ).lower()
    if value:
        return normalize_payment_method(value) or value
    pm_types = paypal.get("payment_method_types")
    if isinstance(pm_types, (list, tuple)):
        pm_type_values = {str(item or "").strip().lower() for item in pm_types}
    else:
        pm_type_values = {str(pm_types or "").strip().lower()} if pm_types else set()
    currency = str(_get(paypal, "currency")).strip().lower()
    if "upi" in pm_type_values or currency == "inr":
        return "upi"
    if "momo" in pm_type_values or currency == "vnd":
        return "momo"
    for method in ("ideal", "pix", "kakao", "blik", "twint"):
        if method in pm_type_values:
            return method
    if _get(paypal, "url"):
        return "paypal"
    return ""


def _oauth_refresh_token(data, auth_session):
    candidates = (
        str(_get(data, "oauth_refresh_token")).strip(),
        str(_get(auth_session, "refreshToken")).strip(),
        str(_get(auth_session, "refresh_token")).strip(),
        _nested_token(auth_session, "session", "refresh_token"),
        _nested_token(auth_session, "session", "refreshToken"),
    )
    for token in candidates:
        if _looks_codex_refresh_token(token):
            return token
    return ""


def _looks_codex_refresh_token(token):
    value = str(token or "").strip()
    if not value or value == "[REDACTED]":
        return False
    # OpenAI has issued both the legacy rt_* form and opaque, URL-safe OAuth
    # refresh tokens.  The latter are JWT-shaped; do not confuse mailbox
    # provider tokens (for example M.C_...) with an OAuth credential.
    if value.startswith("rt_"):
        return True
    if value.startswith(("M.C_", "M.R_")):
        return False
    return (
        len(value) >= 64
        and value.count(".") == 2
        and not any(char.isspace() for char in value)
        and all(char.isalnum() or char in "._~-" for char in value)
    )


def _normalize_account_type(value):
    text = str(value or "").strip().lower()
    if "team" in text or "business" in text or "enterprise" in text:
        return "team"
    if "plus" in text or "pro" in text:
        return "plus"
    if "k12" in text or "edu" in text:
        return "k12"
    if "free" in text:
        return "free"
    return ""


def _jwt_account_type(access_token):
    try:
        parts = str(access_token or "").split(".")
        if len(parts) >= 2:
            import base64
            payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
            value = auth.get("chatgpt_plan_type") or auth.get("plan_type") if isinstance(auth, dict) else ""
            return _normalize_account_type(value)
    except Exception:
        pass
    return ""


def _account_type(data, auth_session, workspace, access_token):
    # The refreshed OAuth access token is newer than the web auth_session.
    # One-click SMS can upgrade the token to Plus while auth_session still says
    # Free, so its claim is the authoritative subscription type.
    token_type = _jwt_account_type(access_token)
    if token_type:
        return token_type
    for value in (
        _get(data, "account_type"),
        _get(data, "plan_type"),
        _get(data, "planType"),
        _nested_field(data, "account", "plan_type"),
        _nested_field(data, "account", "planType"),
        _get(workspace, "account_type_after"),
        _nested_field(auth_session, "account", "plan_type"),
        _nested_field(auth_session, "account", "planType"),
    ):
        normalized = _normalize_account_type(value)
        if normalized:
            return normalized
    return ""


def _refresh_token_status(data, auth_session):
    explicit = str(_get(data, "refresh_token_status")).strip()
    if _oauth_refresh_token(data, auth_session):
        return "oauth_present"
    if explicit and explicit != "oauth_present":
        return explicit
    if _looks_codex_refresh_token(_get(data, "refresh_token")):
        return "legacy_present"
    return "no_rt"


def _status(data, paypal, access_token, has_refresh_token=False):
    explicit = str(_get(data, "status")).strip().lower()
    if explicit in {"account_deactivated", "account_deatived"}:
        return "account_deactivated"
    if explicit in {"at_invalid", "access_token_invalid", "token_invalidated"}:
        return "at_invalid"
    if _looks_account_deactivated(data, paypal):
        return "account_deactivated"
    failure_class = str(_get(data, "failure_class")).strip().lower()
    if failure_class == "network" and data.get("success") is False:
        return "network_failed"
    if failure_class == "mailbox" and data.get("success") is False:
        return "mailbox_failed"
    if failure_class == "auth_state" and data.get("success") is False:
        return "auth_state_failed"
    if failure_class == "rate_limit" and data.get("success") is False:
        return "rate_limited"
    if explicit in {"k12_joined", "k12_requested", "k12_left", "k12_verify_failed"}:
        return explicit
    if _looks_at_invalid(data, paypal):
        return "at_invalid"
    if data.get("success") is False and not has_refresh_token:
        return "failed" if data.get("error") else "pending"
    if not data.get("success") and data.get("error") and not has_refresh_token:
        return "failed"
    if access_token and paypal.get("ok") and str(_get(paypal, "pm_id")).startswith("pm_") and not _get(paypal, "url"):
        return "paypal_pm_created"
    if access_token and paypal.get("ok"):
        return "paypal_ready"
    if access_token and paypal.get("error"):
        return "paypal_failed"
    if access_token:
        return "registered"
    return "pending"


def _looks_at_invalid(data, paypal):
    text = " ".join(
        str(value or "")
        for value in (
            _get(data, "error"),
            _get(data, "paypal_regenerate_error"),
            _get(paypal, "error"),
            _get(paypal, "refresh_error"),
        )
    ).lower()
    markers = (
        "token_invalidated",
        "token_expired",
        "authentication token has been invalidated",
        "could not validate your token",
        "add_phone_required",
        "secondary_phone_verification_required",
        "oauth_refresh_http_401",
        "account_deactivated",
        "account_deatived",
        "deleted or deactivated",
        "account has been deleted",
        "account has been deactivated",
    )
    return any(marker in text for marker in markers)


def _looks_account_deactivated(data, paypal):
    text = " ".join(
        str(value or "")
        for value in (
            _get(data, "error"),
            _get(data, "status"),
            _get(data, "account_scan_status"),
            _get(paypal, "error"),
        )
    ).lower()
    return any(marker in text for marker in (
        "account_deactivated",
        "account_deatived",
        "deleted or deactivated",
        "account has been deleted",
        "account has been deactivated",
    ))


def _success_value(data, access_token):
    if isinstance(data, Mapping) and "success" in data:
        return bool(data.get("success"))
    return bool(access_token)


# ── Account Operations ───────────────────────────────────────────────────────

def upsert_account(
    data: AccountSessionModel | Mapping[str, object],
    json_path="",
    *,
    runtime_config: ConfigInput = None,
):
    model = AccountSessionModel.from_value(data)
    data = model.to_storage_mapping()
    init_database(runtime_config=runtime_config)
    paypal = _nested(data, "paypal")
    auth_session = _nested(data, "auth_session")
    quota = _nested(data, "quota")
    email = _normalize_account_email(model.email)
    if not email:
        return False

    now = int(time.time())
    created_at = _as_int(_get(data, "created_at")) or now
    access_token = model.credentials.access_token
    paypal_status = _paypal_status(data, paypal)
    payment_method = _payment_method(data, paypal)
    oauth_refresh_token = _oauth_refresh_token(data, auth_session)
    refresh_token_status = _refresh_token_status(data, auth_session)
    workspace = _nested(data, "workspace_scan")
    has_refresh_token = refresh_token_status in {"oauth_present", "legacy_present"}
    status = _status(data, paypal, access_token, has_refresh_token=has_refresh_token)
    safe_snapshot = model.safe_snapshot()
    if has_refresh_token and status not in {"at_invalid", "account_deactivated"}:
        safe_snapshot["error"] = ""
    raw_json = json.dumps(safe_snapshot, ensure_ascii=False, separators=(",", ":"))

    row = {
        "email": email,
        "source": model.source,
        "register_method": model.register_method,
        "session_type": model.session_type,
        "plan_type": model.plan_type,
        "password": model.password,
        "success": _as_bool(_success_value(data, access_token)),
        "status": status,
        "error": "" if has_refresh_token and status != "account_deactivated" else model.error,
        "session_token": model.credentials.session_token,
        "access_token": access_token,
        "refresh_token": oauth_refresh_token or model.credentials.refresh_token,
        "cookie_header": model.credentials.cookie_header,
        "device_id": model.device_id,
        "paypal_ok": _as_bool(model.payment.ok),
        "payment_method": payment_method,
        "paypal_url": model.payment.url,
        "paypal_status": paypal_status,
        "paypal_updated_at": _as_int(_get(data, "paypal_updated_at")) or now,
        "paypal_cs_id": model.payment.cs_id,
        "paypal_pm_id": model.payment.pm_id,
        "paypal_currency": model.payment.currency,
        "paypal_amount_due": model.payment.amount_due,
        "paypal_has_paypal": _as_bool(model.payment.has_paypal),
        "refresh_token_status": refresh_token_status,
        "refresh_token_updated_at": _as_int(_get(data, "refresh_token_updated_at")) or (now if oauth_refresh_token else 0),
        "oauth_refresh_token": oauth_refresh_token,
        "workspace_status": str(_get(data, "workspace_status") or _get(workspace, "status")),
        "workspace_id": "" if str(_get(data, "account_type") or _get(workspace, "account_type_after")).strip().lower() == "free" else str(_get(data, "workspace_id") or _get(workspace, "actual_workspace_id")),
        "workspace_name": "" if str(_get(data, "account_type") or _get(workspace, "account_type_after")).strip().lower() == "free" else str(_get(data, "workspace_name") or _get(workspace, "workspace_name") or _get(workspace, "actual_workspace_name")),
        "workspace_switch_result": str(_get(data, "workspace_switch_result") or _get(workspace, "switch_status") or _get(workspace, "switch_error")),
        "workspace_updated_at": _as_int(_get(data, "workspace_updated_at")) or _as_int(_get(workspace, "updated_at")),
        "account_type": _account_type(data, auth_session, workspace, access_token),
        "quota_status": str(_get(data, "quota_status") or quota.get("status", "")),
        "batch_id": str(_get(data, "batch_id")),
        "registration_state": str(_get(data, "registration_state") or ("active" if _get(data, "success") else "failed")),
        "registration_country": str(_get(data, "registration_country")),
        "totp_secret": model.credentials.totp_secret,
        "twofa_enrolled_at": _as_int(_get(data, "twofa_enrolled_at")) or (now if model.credentials.totp_secret else 0),
        "twofa_enroll_error": str(_get(data, "twofa_enroll_error")),
        "auth_session_logging_id": str(_get(data, "auth_session_logging_id")),
        "device_id_generated_at": _as_int(_get(data, "device_id_generated_at")) or (now if _get(data, "device_id") else 0),
        "mailbox_provider": model.mailbox.provider,
        "mailbox_source": model.mailbox.source,
        "mailbox_token": model.mailbox.token,
        "purchase_id": model.mailbox.purchase_id,
        "project_name": model.mailbox.project_name,
        "price": model.mailbox.price,
        "purchase_total_cost": model.mailbox.purchase_total_cost,
        "balance_after": model.mailbox.balance_after,
        "json_path": str(json_path or _get(data, "json_path")),
        "timing_total_seconds": _as_float(_get(model.timing, "total_seconds")),
        "pipeline_total_seconds": _as_float(_get(model.pipeline_timing, "total_seconds")),
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
    conn = _connect(runtime_config=runtime_config)
    try:
        row["email"] = _resolve_account_email(conn, email)
        conn.execute(sql, row)
        conn.commit()
    finally:
        conn.close()
    if status == "account_deactivated" and row["mailbox_provider"].strip().lower() == "remail":
        try:
            from .mailbox_remail import record_dead_remail_account

            record_dead_remail_account(data, reason="account_deactivated")
        except Exception as exc:
            print(f"[!] Failed to update ReMail dead-account history: {exc}")
    return True


def record_registration_audit(data, *, batch_id="", state="", runtime_config: ConfigInput = None):
    """Persist a token-free registration candidate/failure audit event."""
    if not isinstance(data, (AccountSessionModel, Mapping)):
        return False
    data = AccountSessionModel.from_value(data).to_storage_mapping()
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    probe = response.get("access_token_probe") if isinstance(response.get("access_token_probe"), dict) else {}
    telemetry = data.get("access_token_telemetry") if isinstance(data.get("access_token_telemetry"), dict) else {}
    registration_state = str(state or data.get("registration_state") or ("active" if data.get("success") else "failed"))
    email = _normalize_account_email(data.get("email") or "")
    error = str(data.get("error") or "")[:800]
    detail = {
        "registration_warning": str(data.get("registration_warning") or "")[:500],
        "probe_error": str(probe.get("error") or "")[:500],
        "probe_status": str(probe.get("status") or "")[:80],
        "registration_attempts": _as_int(data.get("registration_attempts")),
        "terminal": "account_deactivated" in error.lower(),
    }
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        conn.execute(
            """
            INSERT INTO registration_audit (
                batch_id,email,state,error,failure_class,at_status_code,token_hash,
                token_iat,token_exp,token_age_seconds,registration_country,
                fingerprint_profile,sentinel_version,created_at,detail_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(batch_id or data.get("batch_id") or "")[:100], email, registration_state,
                error, str(data.get("failure_class") or "")[:80], _as_int(probe.get("status_code")),
                str(telemetry.get("token_hash") or "")[:32], _as_int(telemetry.get("iat")),
                _as_int(telemetry.get("exp")), _as_int(telemetry.get("age_seconds")),
                str(data.get("registration_country") or "")[:8],
                str(data.get("auth_fingerprint_profile") or "")[:80],
                str(data.get("sentinel_version") or "")[:80], int(time.time()),
                json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_paypal_accounts(email="", *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    query = """
        SELECT email,access_token,payment_method,paypal_url,paypal_status,paypal_updated_at,refresh_token_status,json_path,updated_at
        FROM accounts
    """
    params = []
    if email:
        query += " WHERE lower(email)=lower(?)"
        params.append(email)
    query += " ORDER BY updated_at DESC"
    conn = _connect(runtime_config=runtime_config)
    try:
        if email:
            params[0] = _find_existing_account_email(conn, email) or _normalize_account_email(email)
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def get_paypal_url(email, *, runtime_config: ConfigInput = None):
    rows = list_paypal_accounts(email, runtime_config=runtime_config)
    for row in rows:
        url = str(row.get("paypal_url") or "").strip()
        if _is_http_url(url):
            return url
    return ""


def get_account_record(email, *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        lookup_email = _find_existing_account_email(conn, email) or _normalize_account_email(email)
        row = conn.execute(
            "SELECT * FROM accounts WHERE lower(email)=lower(?)",
            (lookup_email,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def save_registration_checkpoint(email, state, payload, *, runtime_config: ConfigInput = None):
    """Atomically persist resumable registration state before risky follow-up calls."""
    normalized = _normalize_account_email(email)
    if not normalized:
        return False
    init_database(runtime_config=runtime_config)
    safe_payload = dict(payload or {})
    safe_payload["email"] = normalized
    safe_payload["registration_state"] = str(state or "")
    encoded = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), default=str)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    try:
        conn.execute(
            """INSERT INTO registration_checkpoints(email,state,payload_json,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET state=excluded.state,
               payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (normalized, str(state or ""), encoded, now),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def get_registration_checkpoint(email, *, runtime_config: ConfigInput = None):
    normalized = _normalize_account_email(email)
    if not normalized:
        return {}
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        row = conn.execute("SELECT * FROM registration_checkpoints WHERE email=?", (normalized,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    result = dict(row)
    try:
        result["payload"] = json.loads(result.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["payload"] = {}
    return result


def clear_registration_checkpoint(email, *, runtime_config: ConfigInput = None):
    normalized = _normalize_account_email(email)
    if not normalized:
        return False
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        conn.execute("DELETE FROM registration_checkpoints WHERE email=?", (normalized,))
        conn.commit()
    finally:
        conn.close()
    return True


def get_account_record_by_id(account_id, *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    digits = str(account_id or "").strip()
    if not digits.isdigit():
        return {}
    conn = _connect(runtime_config=runtime_config)
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(digits),)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def list_account_records(*, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        rows = conn.execute("SELECT * FROM accounts ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_device_context(email, *, runtime_config: ConfigInput = None):
    """Return persisted {device_id, auth_session_logging_id} for an existing account.

    Used by registration to reuse the SAME device fingerprint across re-runs,
    preventing "same account, multiple unrelated devices" correlation signals.
    Returns {} if no stored record exists.
    """
    row = get_account_record(email, runtime_config=runtime_config)
    if not row:
        return {}
    device_id = str(row.get("device_id") or "").strip()
    logging_id = str(row.get("auth_session_logging_id") or "").strip()
    if not device_id and not logging_id:
        return {}
    return {
        "device_id": device_id,
        "auth_session_logging_id": logging_id,
    }


def list_terminal_remail_accounts(*, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    try:
        rows = conn.execute(
            """
            SELECT email,purchase_id,raw_json
            FROM accounts
            WHERE lower(mailbox_provider)='remail'
              AND (
                lower(status) IN ('account_deactivated','account_deatived')
                OR lower(error) LIKE '%account_deactivated%'
                OR lower(error) LIKE '%account_deatived%'
                OR lower(raw_json) LIKE '%account_deactivated%'
                OR lower(raw_json) LIKE '%account_deatived%'
              )
            """
        ).fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        item = {"email": row["email"], "purchase_id": row["purchase_id"]}
        try:
            raw = json.loads(row["raw_json"] or "{}")
            mailbox = raw.get("mailbox") if isinstance(raw, dict) and isinstance(raw.get("mailbox"), dict) else {}
            item["order_no"] = str(mailbox.get("order_no") or "")
        except Exception:
            item["order_no"] = ""
        results.append(item)
    return results


# ── Status Update Operations ─────────────────────────────────────────────────

def mark_paypal_status(email, status="completed", *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
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
            _mark_plan_type_plus(data)
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


def mark_quota_status(email, quota_status="", quota_result=None, *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    json_path = ""
    data = {}
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
        quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
        quota["status"] = str(quota_status or "")
        quota["updated_at"] = now
        if isinstance(quota_result, dict):
            quota["last_result"] = {
                key: value
                for key, value in quota_result.items()
                if key not in {"access_token", "authorization", "cookie", "cookie_header"}
            }
        data["quota"] = quota
        data["quota_status"] = str(quota_status or "")
        data["quota_updated_at"] = now
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            UPDATE accounts
            SET quota_status=?, updated_at=?, raw_json=?
            WHERE lower(email)=lower(?)
            """,
            (str(quota_status or ""), now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


def mark_promotion_status(email, promotion_status="", promotion_result=None, *, runtime_config: ConfigInput = None):
    """Persist the account plan/promotion (优惠) probe result into raw_json + session.

    Stored alongside the account without a dedicated DB column; ``desktop_read``
    surfaces ``promotion_status`` from raw_json for the 优惠状态 list column.
    """
    init_database(runtime_config=runtime_config)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    json_path = ""
    data = {}
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
        try:
            data = json.loads(row["raw_json"] or "{}")
        except Exception:
            data = {}
        json_path = str(row["json_path"] or "").strip()
        if json_path:
            try:
                file_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                if isinstance(file_data, dict):
                    data = {**file_data, **data}
            except Exception:
                pass
        promotion = data.get("promotion") if isinstance(data.get("promotion"), dict) else {}
        promotion["status"] = str(promotion_status or "")
        promotion["updated_at"] = now
        if isinstance(promotion_result, dict):
            promotion["last_result"] = {
                key: value
                for key, value in promotion_result.items()
                if key not in {"access_token", "authorization", "cookie", "cookie_header"}
            }
        data["promotion"] = promotion
        data["promotion_status"] = str(promotion_status or "")
        data["promotion_updated_at"] = now
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE accounts SET updated_at=?, raw_json=? WHERE lower(email)=lower(?)",
            (now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


def clear_stale_promotion_at_marker(email, *, runtime_config: ConfigInput = None):
    """Clear a stale ``AT失效`` promotion marker after a verified relogin.

    The promotion (优惠) probe label predates the replacement access token.
    Keep ``promotion.last_result`` for later inspection but stop surfacing the
    stale authentication failure in the desktop 优惠状态 column. Returns True
    when a stale marker was found and cleared.
    """
    init_database(runtime_config=runtime_config)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    json_path = ""
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
        try:
            data = json.loads(row["raw_json"] or "{}")
        except Exception:
            data = {}
        json_path = str(row["json_path"] or "").strip()
        if json_path:
            try:
                file_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                if isinstance(file_data, dict):
                    data = {**file_data, **data}
            except Exception:
                pass
        changed = False
        if str(data.get("promotion_status") or "").strip() == "AT失效":
            data["promotion_status"] = ""
            changed = True
        promotion = data.get("promotion") if isinstance(data.get("promotion"), dict) else None
        if isinstance(promotion, dict) and str(promotion.get("status") or "").strip() == "AT失效":
            promotion["status"] = ""
            data["promotion"] = promotion
            changed = True
        if not changed:
            return False
        data["promotion_updated_at"] = now
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE accounts SET updated_at=?, raw_json=? WHERE lower(email)=lower(?)",
            (now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


# ── Session File & Rebuild ───────────────────────────────────────────────────

def _mark_plan_type_plus(data):
    if not isinstance(data, dict):
        return
    data["planType"] = "plus"
    data["plan_type"] = "plus"
    account = data.get("account")
    if not isinstance(account, dict):
        account = {}
        data["account"] = account
    account["planType"] = "plus"

    auth_session = data.get("auth_session")
    if isinstance(auth_session, dict):
        auth_account = auth_session.get("account")
        if not isinstance(auth_account, dict):
            auth_account = {}
            auth_session["account"] = auth_account
        auth_account["planType"] = "plus"


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


def rebuild_from_session_dir(session_dir, *, runtime_config: ConfigInput = None):
    init_database(runtime_config=runtime_config)
    count = 0
    for path in sorted(Path(session_dir).glob("session_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] Skip bad session JSON: {path} {e}")
            continue
        if upsert_account(data, json_path=str(path), runtime_config=runtime_config):
            count += 1
    return count
