"""normalize submodule of the former storage.py (mechanical split, bodies unchanged)."""

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse
import json
import sqlite3

from .constants import EMAIL_RE
from .constants import KNOWN_EMAIL_DOMAINS


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
    from ..payment_link_manager import normalize_payment_method

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

