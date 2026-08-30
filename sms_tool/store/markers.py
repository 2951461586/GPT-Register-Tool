"""markers submodule of the former storage.py (mechanical split, bodies unchanged)."""

from collections.abc import Mapping
from pathlib import Path
import json
import time

from ..config import ConfigInput

from .connection import _connect, init_database
from .normalize import _find_existing_account_email, _update_session_json


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



def mark_account_health_result(
    email,
    health_result,
    *,
    runtime_config: ConfigInput = None,
):
    """Persist the unified account-health contract without storing credentials."""
    if not isinstance(health_result, Mapping):
        return False
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
            "SELECT raw_json, json_path FROM accounts WHERE lower(email)=lower(?)",
            (lookup_email,),
        ).fetchone()
        if row is None:
            return False
        json_path = str(row["json_path"] or "")
        try:
            data = json.loads(row["raw_json"] or "{}")
        except Exception:
            data = {}
        if json_path:
            try:
                file_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                if isinstance(file_data, dict):
                    data = {**file_data, **data}
            except Exception:
                pass
        from ..account_health import sanitize_health_details

        safe_result = sanitize_health_details(dict(health_result))
        check = str(safe_result.get("check") or "unknown")
        health = data.get("account_health") if isinstance(data.get("account_health"), dict) else {}
        checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
        checks[check] = safe_result
        health.update({
            "latest": safe_result,
            "checks": checks,
            "updated_at": now,
        })
        data["account_health"] = health
        plan_type = str(safe_result.get("plan_type") or "").strip().lower()
        terminal = bool(safe_result.get("terminal"))
        if plan_type:
            data["plan_type"] = plan_type
        if terminal:
            data["status"] = "account_deactivated"
            data["error"] = "account_deactivated"
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            UPDATE accounts
            SET plan_type=CASE WHEN ? <> '' THEN ? ELSE plan_type END,
                status=CASE WHEN ? THEN 'account_deactivated' ELSE status END,
                error=CASE WHEN ? THEN 'account_deactivated' ELSE error END,
                updated_at=?,
                raw_json=?
            WHERE lower(email)=lower(?)
            """,
            (plan_type, plan_type, int(terminal), int(terminal), now, raw_json, lookup_email),
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

