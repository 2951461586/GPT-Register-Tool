import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import CFG
from .paths import output_dir
from .k12_identity import (
    _base64url_json,
    _extract_access_token,
    _extract_id_token,
    _extract_refresh_token,
    _jwt_claims,
    _token_account_id,
)

def _epoch_from_iso(value):
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def _iso_local_from_epoch(epoch_seconds, tz_hours=8):
    try:
        tz = timezone(timedelta(hours=tz_hours))
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).astimezone(tz).replace(microsecond=0).isoformat()
    except Exception:
        return ""


def _now_local_iso(tz_hours=8):
    tz = timezone(timedelta(hours=tz_hours))
    return datetime.now(tz=tz).replace(microsecond=0).isoformat()


def _build_synthetic_k12_id_token(email, workspace_id, expires_at=""):
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return ""
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": _epoch_from_iso(expires_at) or now + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."

def build_k12_cpa_json(account, workspace_id, now_iso=""):
    """Build the compact CPA JSON used after a K12 workspace join.

    Reference shape:
    access_token/account_id/disabled/email/expired/id_token/last_refresh/refresh_token/type.
    The account_id is intentionally the K12 workspace id.  If the stored id_token
    belongs to another account, generate a synthetic CPA-compatible id_token so
    the exported account id stays consistent.
    """
    raw = account.get("raw") if isinstance(account.get("raw"), dict) else {}
    workspace_id = str(workspace_id or "").strip()
    email = str(raw.get("email") or account.get("email") or "").strip()
    access_token = str(account.get("access_token") or _extract_access_token(raw)).strip()
    refresh_token = _extract_refresh_token(raw)
    access_claims = _jwt_claims(access_token)
    expired = _iso_local_from_epoch(access_claims.get("exp")) if access_claims.get("exp") else ""
    if not expired:
        expired = str(raw.get("expired") or raw.get("expires") or raw.get("expires_at") or "").strip()
    id_token = _extract_id_token(raw)
    id_account_id = _token_account_id(id_token)
    if not id_token or id_account_id != workspace_id:
        id_token = _build_synthetic_k12_id_token(email=email, workspace_id=workspace_id, expires_at=expired)
    return {
        "access_token": access_token,
        "account_id": workspace_id,
        "disabled": bool(raw.get("disabled", False)),
        "email": email,
        "expired": expired,
        "id_token": id_token,
        "last_refresh": str(now_iso or _now_local_iso()),
        "refresh_token": refresh_token,
        "type": "codex",
    }


def _k12_cpa_export_path(email, export_dir=""):
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "codex_exports"
    safe_email = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", (email or "unknown").strip())
    return directory / f"codex-{safe_email}-k12.json"


def export_k12_cpa_json(account, workspace_id, export_dir=""):
    cpa_json = build_k12_cpa_json(account, workspace_id)
    if not cpa_json.get("access_token"):
        return {"ok": False, "error": "missing_access_token"}
    if not cpa_json.get("account_id"):
        return {"ok": False, "error": "missing_workspace_id"}
    path = _k12_cpa_export_path(cpa_json.get("email") or account.get("email") or "", export_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cpa_json, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "email": cpa_json.get("email", ""),
        "account_id": cpa_json.get("account_id", ""),
        "expired": cpa_json.get("expired", ""),
        "refresh_token_status": "oauth_present" if cpa_json.get("refresh_token") else "no_rt",
    }

