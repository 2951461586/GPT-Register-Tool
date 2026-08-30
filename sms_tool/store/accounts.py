"""accounts submodule of the former storage.py (mechanical split, bodies unchanged)."""

from collections.abc import Mapping
from pathlib import Path
import json
import time

from ..account_models import AccountSessionModel
from ..config import ConfigInput

from .connection import _connect, init_database
from .normalize import _account_type, _as_bool, _as_float, _as_int, _find_existing_account_email, _get, _is_http_url, _nested, _normalize_account_email, _oauth_refresh_token, _payment_method, _paypal_status, _refresh_token_status, _resolve_account_email, _status, _success_value


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
            from ..mailbox_remail import record_dead_remail_account

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



def migrate_account_email(old_email, new_email, verified_data, *, runtime_config: ConfigInput = None):
    """Atomically move one account row to a new email after verified relogin.

    The destination is never overwritten.  The database update and session-file
    replacement are prepared before the transaction; a failed destination
    conflict or malformed account leaves the original row untouched.
    """
    old = _normalize_account_email(old_email)
    new = _normalize_account_email(new_email)
    if not old or not new or old == new or not isinstance(verified_data, Mapping):
        return False
    if not str(verified_data.get("access_token") or verified_data.get("cookie_header") or "").strip():
        return False
    init_database(runtime_config=runtime_config)
    conn = _connect(runtime_config=runtime_config)
    session_path = ""
    session_payload = None
    session_target = None
    session_backup = None
    session_existed = False
    session_replaced = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT * FROM accounts WHERE lower(email)=lower(?)", (old,)).fetchone()
        if source is None:
            conn.rollback()
            return False
        destination = conn.execute("SELECT 1 FROM accounts WHERE lower(email)=lower(?)", (new,)).fetchone()
        if destination is not None:
            conn.rollback()
            return False
        row = dict(source)
        session_path = str(row.get("json_path") or verified_data.get("json_path") or "").strip()
        session_payload = dict(verified_data)
        session_payload["email"] = new
        if session_path:
            target = Path(session_path)
            if target.exists():
                try:
                    existing_session = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(existing_session, dict):
                        merged = dict(existing_session)
                        merged.update(session_payload)
                        session_payload = merged
                except Exception:
                    pass
        model = AccountSessionModel.from_value(session_payload)
        mailbox = model.mailbox
        safe = model.safe_snapshot()
        safe["email"] = new
        raw_existing = {}
        try:
            raw_existing = json.loads(row.get("raw_json") or "{}")
        except Exception:
            raw_existing = {}
        if isinstance(raw_existing, dict):
            raw_existing.update(safe)
            raw_json = json.dumps(raw_existing, ensure_ascii=False, separators=(",", ":"))
        else:
            raw_json = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        now = int(time.time())
        updates = {
            "email": new,
            "password": model.password or row.get("password", ""),
            "success": _as_bool(_success_value(session_payload, model.credentials.access_token)),
            "status": _status(session_payload, _nested(session_payload, "paypal"), model.credentials.access_token, has_refresh_token=bool(model.credentials.refresh_token or model.credentials.oauth_refresh_token)),
            "error": model.error,
            "session_token": model.credentials.session_token or row.get("session_token", ""),
            "access_token": model.credentials.access_token or row.get("access_token", ""),
            "refresh_token": model.credentials.refresh_token or row.get("refresh_token", ""),
            "cookie_header": model.credentials.cookie_header or row.get("cookie_header", ""),
            "device_id": model.device_id or row.get("device_id", ""),
            "mailbox_provider": mailbox.provider,
            "mailbox_source": mailbox.source,
            "mailbox_token": mailbox.token,
            "purchase_id": mailbox.purchase_id,
            "project_name": mailbox.project_name,
            "price": mailbox.price,
            "purchase_total_cost": mailbox.purchase_total_cost,
            "balance_after": mailbox.balance_after,
            "json_path": session_path,
            "updated_at": now,
            "raw_json": raw_json,
        }
        if session_path:
            session_target = Path(session_path)
            session_target.parent.mkdir(parents=True, exist_ok=True)
            session_existed = session_target.exists()
            session_backup = session_target.read_bytes() if session_existed else None
            temp = session_target.with_name(session_target.name + ".email-change.tmp")
            temp.write_text(json.dumps(session_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(session_target)
            session_replaced = True
        cursor = conn.execute("UPDATE accounts SET " + ", ".join(f"{key}=:{key}" for key in updates) + " WHERE lower(email)=lower(:old)", {**updates, "old": old})
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        if session_replaced and session_target is not None:
            try:
                if session_existed and session_backup is not None:
                    session_target.write_bytes(session_backup)
                elif session_target.exists():
                    session_target.unlink()
            except Exception:
                pass
        return False
    finally:
        conn.close()
    return True



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

