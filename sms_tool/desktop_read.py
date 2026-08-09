"""Read-only desktop data contract.

The WPF client consumes this boundary instead of opening SQLite. Secret-bearing
mailbox material is written to a local temporary file and only its path crosses
IPC; normal account reads contain the token-free session snapshot.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .config import ConfigInput, RuntimeConfig, resolve_runtime_config
from .sanitizer import sanitize
from .storage import get_account_record_by_id, list_account_records


_PUBLIC_COLUMNS = (
    "id", "email", "success", "status", "error", "device_id", "paypal_ok",
    "payment_method", "paypal_status", "paypal_updated_at", "refresh_token_status",
    "refresh_token_updated_at", "workspace_status", "workspace_id", "workspace_name",
    "workspace_switch_result", "workspace_updated_at", "account_type", "quota_status",
    "batch_id", "registration_state", "registration_country", "twofa_enrolled_at",
    "twofa_enroll_error", "auth_session_logging_id", "device_id_generated_at",
    "mailbox_provider", "mailbox_source", "purchase_id", "project_name", "price",
    "purchase_total_cost", "balance_after", "json_path", "timing_total_seconds",
    "pipeline_total_seconds", "created_at", "updated_at",
)


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    result = {key: record.get(key) for key in _PUBLIC_COLUMNS}
    result.update({
        "has_access_token": bool(str(record.get("access_token") or "").strip()),
        "has_refresh_token": bool(str(record.get("refresh_token") or record.get("oauth_refresh_token") or "").strip()),
        "has_payment_url": bool(str(record.get("paypal_url") or "").strip()),
        "has_totp": bool(str(record.get("totp_secret") or "").strip()),
    })
    raw_json = record.get("raw_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            result["session"] = sanitize(json.loads(raw_json))
        except (TypeError, ValueError):
            result["session"] = {}
    else:
        result["session"] = {}
    return result


def read_accounts(runtime_config: ConfigInput = None) -> list[dict[str, Any]]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    return [_record_payload(row) for row in list_account_records(runtime_config=config)]


def read_account(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    row = get_account_record_by_id(account_id, runtime_config=config) if str(account_id or "").strip() else {}
    if not row and email:
        from .storage import get_account_record
        row = get_account_record(email, runtime_config=config)
    return _record_payload(row) if row else {}


def create_mailbox_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    data = _full_account_payload(row)
    mailbox = data.get("mailbox") if isinstance(data, dict) else {}
    if not isinstance(mailbox, dict):
        mailbox = {}
    line = _mailbox_line(mailbox)
    if not line:
        return {"ok": False, "error": "mailbox_credentials_missing"}
    target = _write_temp_text("smsworkbench_mailbox_", line + "\n", suffix=".txt")
    return {"ok": True, "path": str(target), "provider": str(mailbox.get("provider") or "")}


def create_account_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    target = _write_temp_text(
        "smsworkbench_account_",
        json.dumps(_full_account_payload(row), ensure_ascii=False, separators=(",", ":")),
        suffix=".json",
    )
    return {"ok": True, "path": str(target)}


def create_payment_url_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    url = str(row.get("paypal_url") or "").strip()
    if not url:
        return {"ok": False, "error": "payment_url_missing"}
    target = _write_temp_text("smsworkbench_payment_url_", url + "\n", suffix=".txt")
    return {"ok": True, "path": str(target)}


def _find_record(account_id: str, email: str, runtime_config: ConfigInput) -> dict[str, Any]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    row = get_account_record_by_id(account_id, runtime_config=config) if str(account_id or "").strip() else {}
    if not row and email:
        from .storage import get_account_record
        row = get_account_record(email, runtime_config=config)
    return row


def _full_account_payload(row: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw_json = str(row.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data.update(parsed)
        except (TypeError, ValueError):
            pass

    json_path = Path(str(row.get("json_path") or "").strip())
    if json_path.is_file():
        try:
            parsed = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                data.update(parsed)
        except (OSError, TypeError, ValueError):
            pass

    for key in (
        "email", "password", "success", "status", "error", "session_token", "access_token",
        "refresh_token", "oauth_refresh_token", "cookie_header", "device_id", "totp_secret",
        "auth_session_logging_id", "registration_country", "refresh_token_status", "payment_method",
    ):
        value = row.get(key)
        if value not in (None, ""):
            data[key] = value

    mailbox = data.get("mailbox") if isinstance(data.get("mailbox"), dict) else {}
    mailbox = dict(mailbox)
    mailbox_defaults = {
        "email": row.get("email"),
        "provider": row.get("mailbox_provider"),
        "source": row.get("mailbox_source"),
        "token": row.get("mailbox_token"),
        "purchase_id": row.get("purchase_id"),
        "project_name": row.get("project_name"),
        "price": row.get("price"),
        "purchase_total_cost": row.get("purchase_total_cost"),
        "balance_after": row.get("balance_after"),
    }
    for key, value in mailbox_defaults.items():
        if value not in (None, "") and not mailbox.get(key):
            mailbox[key] = value
    data["mailbox"] = mailbox

    payment = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    payment = dict(payment)
    for key, column in (
        ("ok", "paypal_ok"), ("url", "paypal_url"), ("status", "paypal_status"),
        ("cs_id", "paypal_cs_id"), ("pm_id", "paypal_pm_id"),
        ("currency", "paypal_currency"), ("amount_due", "paypal_amount_due"),
        ("has_paypal", "paypal_has_paypal"),
    ):
        value = row.get(column)
        if value not in (None, "") and not payment.get(key):
            payment[key] = value
    data["paypal"] = payment
    return data


def _write_temp_text(prefix: str, content: str, *, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=prefix, suffix=suffix, delete=False
    )
    with handle:
        handle.write(content)
    return Path(handle.name)


def _mailbox_line(mailbox: dict[str, Any]) -> str:
    email = str(mailbox.get("email") or "").strip()
    provider = str(mailbox.get("provider") or "").strip().lower()
    if not email:
        return ""
    if provider == "cfworker":
        return f"cfworker://{email}"
    if provider == "smailr":
        return f"smailr://{email}"
    if provider == "remail":
        token = str(mailbox.get("token") or "").strip()
        order = str(mailbox.get("order_no") or "").strip()
        purchase = str(mailbox.get("purchase_id") or "").strip()
        return "remail://" + "|".join(filter(None, (email, token, order, purchase)))
    if provider == "gmail":
        client = str(mailbox.get("client_id") or mailbox.get("token") or "").strip()
        secret = str(mailbox.get("client_secret") or "").strip()
        refresh = str(mailbox.get("refresh_token") or "").strip()
        if client and secret and refresh:
            return f"gmail://{email}----{client}----{secret}----{refresh}"
        password = str(mailbox.get("login_password") or mailbox.get("password") or "").strip()
        return f"gmail://{email}---{password}" if password else ""
    password = str(mailbox.get("password") or "").strip()
    refresh = str(mailbox.get("refresh_token") or "").strip()
    access = str(mailbox.get("access_token") or "").strip()
    client = str(mailbox.get("client_id") or mailbox.get("clientId") or mailbox.get("token") or "").strip()
    if client and refresh:
        return f"{email}----{password}----{client}----{refresh}"
    if refresh:
        return f"{email}---{password}---{refresh}---{access}---0"
    token = str(mailbox.get("token") or "").strip()
    return f"{email}----{token}" if token else ""
