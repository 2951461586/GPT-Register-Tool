"""markers submodule of the former storage.py (mechanical split, bodies unchanged)."""

from collections.abc import Mapping
from pathlib import Path
import json
import time

from ..config import ConfigInput
from ..payment.promotion_states import PROMOTION_STATE_AUTH_INVALID

from .connection import _connect, init_database
from .normalize import (
    _find_existing_account_email,
    _looks_account_deactivated,
    _probe_is_token_invalid,
    _update_session_json,
)


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
            "SELECT raw_json,json_path,status,error FROM accounts WHERE email=?",
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
        _raw_quota: object = data.get("quota")
        quota: dict = _raw_quota if isinstance(_raw_quota, dict) else {}
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
        verified_active = False
        # No derived caches here. ``quota.last_result`` (written just above) is
        # this lane's probe record and is refreshed on *every* verdict; the old
        # writes of ``at_probe_status_code`` and a top-level ``token_probe`` were
        # a second copy of the same fact that only moved on a 200 -- measured
        # 2026-09-04, five rows kept ``token_probe.status_code=401`` /
        # ``at_probe_status_code='401'`` long after a scan had verified the same
        # AT with a 200, because a scan heals an account without going through
        # this function. The payload field the grid reads is projected from the
        # probe records at read time by
        # ``desktop.read._access_token_probe_status_code``, which ranks them by
        # timestamp, so nothing needs the stored copy.
        if isinstance(quota_result, dict):
            try:
                verified_active = (
                    bool(quota_result.get("ok")) and 200 <= int(quota_result.get("status_code") or 0) < 300
                )
            except (TypeError, ValueError):
                verified_active = False
        # A probe that just answered HTTP 200 answered for *this* account, so any
        # ``error`` the record still carries describes an attempt that is over.
        # Clearing it only when ``status='at_invalid'`` (the old rule) missed the
        # far more common shape: a transient scan/relogin failure writes
        # ``web_session_access_token_probe_failed:unknown`` and leaves
        # ``status='registered'`` (``normalize._status`` derives that from the AT
        # alone), so the error column was never cleared again and the WPF 状态
        # column rendered 失败 for a healthy account. Measured 2026-11-20: a
        # 2-account 账号测活 round printed 正常 2，掉号 0，其他失败 0 while both
        # rows showed 失败. A confirmed deactivation is the one verdict a probe
        # cannot retract, so it survives -- the row's own columns count as
        # evidence there, because that verdict is stored in them.
        stored = {
            "status": row["status"] or data.get("status"),
            "error": row["error"] or data.get("error"),
            "account_scan_status": data.get("account_scan_status"),
        }
        stale_error = verified_active and not _looks_account_deactivated(stored, {})
        if stale_error:
            data.pop("error", None)
        # The mirror verdict. Nothing in this path ever wrote ``status``, so a
        # 401 could only land in ``quota_status``/``quota.last_result`` -- keys
        # no grid column reads -- while the row kept ``status='registered'``
        # (``normalize._status`` derives it from the AT's mere presence) and the
        # WPF 状态 column rendered 已注册 for a dead token. Measured 2026-11-20:
        # avawhite155379@outlook.com held a 401 ``invalid_jwt`` probe with
        # ``status='registered'``, ``error=''``. The inverse transition
        # (at_invalid -> registered on a verified 200) already lived in this
        # SQL; this is the missing half that gives it something to undo. A
        # confirmed deactivation outranks both.
        invalid_at = (
            not verified_active and not _looks_account_deactivated(stored, {}) and _probe_is_token_invalid(quota_result)
        )
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            UPDATE accounts
            SET quota_status=?,
                status=CASE
                    WHEN ? THEN 'at_invalid'
                    WHEN status='at_invalid' AND ? THEN 'registered'
                    ELSE status END,
                error=CASE WHEN ? THEN '' ELSE error END,
                updated_at=?, raw_json=?
            WHERE email=?
            """,
            (
                str(quota_status or ""),
                int(invalid_at),
                int(verified_active),
                int(stale_error),
                now,
                raw_json,
                lookup_email,
            ),
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
            "SELECT raw_json, json_path FROM accounts WHERE email=?",
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
        from ..kernel.sanitizer import drop_sensitive_fields

        safe_result = drop_sensitive_fields(dict(health_result), max_string_length=1000)
        check = str(safe_result.get("check") or "unknown")
        _raw_health: object = data.get("account_health")
        health: dict = _raw_health if isinstance(_raw_health, dict) else {}
        _raw_checks: object = health.get("checks")
        checks: dict = _raw_checks if isinstance(_raw_checks, dict) else {}
        checks[check] = safe_result
        health.update(
            {
                "latest": safe_result,
                "checks": checks,
                "updated_at": now,
            }
        )
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
                terminal_state=CASE WHEN ? THEN 'account_deactivated' ELSE terminal_state END,
                updated_at=?,
                raw_json=?
            WHERE email=?
            """,
            (plan_type, plan_type, int(terminal), int(terminal), int(terminal), now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


def mark_promotion_status(
    email,
    promotion_status="",
    promotion_result=None,
    *,
    promotion_state: str = "",
    runtime_config: ConfigInput = None,
):
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
            "SELECT raw_json,json_path FROM accounts WHERE email=?",
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
        _raw_promotion: object = data.get("promotion")
        promotion: dict = _raw_promotion if isinstance(_raw_promotion, dict) else {}
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
        # Machine state next to the display label (sms_tool/payment/promotion_states.py):
        # the desktop filter/sort keys off this, not off the Chinese copy.
        promotion_state = str(promotion_state or (promotion_result or {}).get("promotion_state") or "").strip()
        promotion["state"] = promotion_state
        data["promotion_state"] = promotion_state
        if isinstance(promotion_result, dict):
            if "plus_trial_eligible" in promotion_result:
                promotion["plus_trial_eligible"] = bool(promotion_result.get("plus_trial_eligible"))
            if "team_trial_eligible" in promotion_result:
                team_eligible = bool(promotion_result.get("team_trial_eligible"))
                promotion["team_trial_eligible"] = team_eligible
                data["team_trial_eligible"] = team_eligible
            team_state = str(promotion_result.get("team_trial_state") or "").strip()
            if team_state:
                promotion["team_trial_state"] = team_state
        # Promotion and liveness are separate contracts. A promotion 401 is
        # retained under promotion.last_result and must not downgrade the
        # shared account/AT status.
        promotion_code = ""
        if isinstance(promotion_result, dict):
            promotion_code = str(promotion_result.get("status_code") or "").strip()
            if not promotion_code and str(promotion_result.get("error") or "").strip().lower() in {
                "token_invalid",
                "access_token_invalid",
                "token_invalidated",
            }:
                promotion_code = "401"
        promotion["status_code"] = promotion_code
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE accounts SET updated_at=?, raw_json=? WHERE email=?",
            (now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


#: States that answered the operator's question. A transient transport failure
#: must not erase the last one.
_CONCLUSIVE_QUALIFICATION_STATES = frozenset({"eligible", "ineligible"})


def _update_last_conclusive(qualification: dict, label: str, state: str, now: int) -> None:
    """Preserve the last conclusive verdict before the current fields are overwritten."""
    previous_state = str(qualification.get("state") or "")
    previous_status = str(qualification.get("status") or "")
    if state in _CONCLUSIVE_QUALIFICATION_STATES:
        qualification["last_conclusive"] = {
            "status": str(label or ""),
            "state": state,
            "updated_at": now,
        }
    elif previous_state in _CONCLUSIVE_QUALIFICATION_STATES and not isinstance(
        qualification.get("last_conclusive"), dict
    ):
        qualification["last_conclusive"] = {
            "status": previous_status,
            "state": previous_state,
            "updated_at": qualification.get("updated_at") or now,
        }


def mark_payment_qualification_status(
    email,
    label="",
    *,
    state: str = "",
    result=None,
    runtime_config: ConfigInput = None,
):
    """Persist one account's 支付资格 probe result into ``raw_json`` + session.

    Stored under ``payment_qualification`` (a dict: status/state/last_result)
    without a dedicated DB column, exactly like ``promotion``. The desktop grid
    reads the label and state from ``desktop_read``, which derives them from
    this block — adding a column must not need a schema migration.

    ``last_result`` is scrubbed of token-bearing keys before it is written: the
    probe result carries checkout diagnostics, and a support bundle must never
    contain an access token because a qualification run ran.
    """
    init_database(runtime_config=runtime_config)
    now = int(time.time())
    conn = _connect(runtime_config=runtime_config)
    json_path = ""
    data: dict = {}
    try:
        lookup_email = _find_existing_account_email(conn, email)
        if not lookup_email:
            return False
        row = conn.execute(
            "SELECT raw_json,json_path FROM accounts WHERE email=?",
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
        _raw: object = data.get("payment_qualification")
        qualification: dict = _raw if isinstance(_raw, dict) else {}
        _update_last_conclusive(qualification, str(label or ""), str(state or ""), now)
        qualification["status"] = str(label or "")
        qualification["state"] = str(state or "")
        qualification["updated_at"] = now
        if isinstance(result, dict):
            qualification["last_result"] = {
                key: value
                for key, value in result.items()
                if key
                not in {
                    "access_token",
                    "authorization",
                    "cookie",
                    "cookie_header",
                    "auth_context",
                    "probe",
                }
            }
        data["payment_qualification"] = qualification
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE accounts SET updated_at=?, raw_json=? WHERE email=?",
            (now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True


def clear_stale_promotion_at_marker(email, *, verified_at: int | None = None, runtime_config: ConfigInput = None):
    """Clear an older ``AT失效`` promotion marker after a verified liveness probe.

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
            "SELECT raw_json,json_path FROM accounts WHERE email=?",
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
        cutoff = int(verified_at or now)
        try:
            promotion_updated_at = int(data.get("promotion_updated_at") or 0)
        except (TypeError, ValueError):
            promotion_updated_at = 0
        if promotion_updated_at > cutoff:
            return False
        changed = False
        if str(data.get("promotion_status") or "").strip() == "AT失效":
            data["promotion_status"] = ""
            changed = True
        if str(data.get("promotion_state") or "").strip() == PROMOTION_STATE_AUTH_INVALID:
            data["promotion_state"] = ""
            changed = True
        promotion = data.get("promotion") if isinstance(data.get("promotion"), dict) else None
        if isinstance(promotion, dict):
            if str(promotion.get("status") or "").strip() == "AT失效":
                promotion["status"] = ""
                changed = True
            if str(promotion.get("state") or "").strip() == PROMOTION_STATE_AUTH_INVALID:
                promotion["state"] = ""
                changed = True
            if changed:
                data["promotion"] = promotion
        if not changed:
            return False
        data["promotion_updated_at"] = now
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE accounts SET updated_at=?, raw_json=? WHERE email=?",
            (now, raw_json, lookup_email),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        _update_session_json(json_path, data)
    return True
