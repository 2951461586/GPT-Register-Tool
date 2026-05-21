import json
import time
from pathlib import Path

from .gen_pp_link import generate_pp_link
from .storage import get_account_record, upsert_account


def regenerate_paypal_link(email="", session_file="", proxy=None):
    data, json_path = _load_seed(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    if target_email:
        data["email"] = target_email

    access_token = _access_token(data)
    if not access_token:
        return {"ok": False, "email": target_email, "error": "missing_access_token"}

    old_paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    old_paypal_status = str(data.get("paypal_status") or "").strip()
    paypal = generate_pp_link(access_token, proxy=proxy)
    now = int(time.time())
    if paypal.get("ok") and paypal.get("url"):
        data["paypal"] = paypal
        data["paypal_status"] = "link_ready"
    else:
        if old_paypal.get("url"):
            data["paypal"] = old_paypal
            data["paypal_status"] = old_paypal_status or "link_ready"
        else:
            data["paypal"] = paypal
            data["paypal_status"] = "failed"
        data["paypal_regenerate_error"] = paypal.get("error", "")
        if isinstance(paypal.get("stripe_error"), dict):
            data["paypal_regenerate_error_details"] = paypal["stripe_error"]
        if paypal.get("error_code"):
            data["paypal_regenerate_error_code"] = paypal["error_code"]
    data["paypal_updated_at"] = now
    data["access_token"] = access_token
    data["success"] = bool(data.get("success", True))

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)

    return {
        "ok": bool(paypal.get("ok") and paypal.get("url")),
        "email": data.get("email", ""),
        "paypal_status": data["paypal_status"],
        "paypal_url": data.get("paypal", {}).get("url", "") if isinstance(data.get("paypal"), dict) else "",
        "json_path": json_path,
        "error": paypal.get("error", ""),
    }


def _load_seed(email="", session_file=""):
    if session_file:
        path = Path(session_file)
        data = _read_json(path)
        return data, str(path)

    record = get_account_record(email) if email else {}
    json_path = str(record.get("json_path") or "").strip()
    data = {}
    if json_path:
        data = _read_json(Path(json_path))
    raw_json = str(record.get("raw_json") or "").strip()
    if raw_json:
        try:
            raw_data = json.loads(raw_json)
            if isinstance(raw_data, dict):
                data = {**raw_data, **data}
        except Exception:
            pass
    if record:
        data.setdefault("email", record.get("email", ""))
        data.setdefault("access_token", record.get("access_token", ""))
        data.setdefault("cookie_header", record.get("cookie_header", ""))
    return data, json_path


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _access_token(data):
    token = str(data.get("access_token") or "").strip()
    if token:
        return token
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = auth_session.get(key)
        if isinstance(value, str) and value:
            return value
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = session.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
