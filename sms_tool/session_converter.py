"""Local ChatGPT session JSON converter.

Python port of the useful, non-UI parts from chatgpt-workspace-tools'
``converter-core.js``.  It accepts ChatGPT Web session JSON, Codex auth-like
JSON, 9router/AxonHub/Codex-Manager-ish objects, and nested arrays/documents,
then exports import-ready CPA/sub2api/Cockpit/9router/Codex/AxonHub formats.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMATS = {"cpa", "sub2api", "cockpit", "9router", "codex", "axonhub", "codexmanager"}
AXONHUB_PLACEHOLDER_REFRESH_TOKEN = "__missing_refresh_token__"


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


def nested(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current


def strip_unavailable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_unavailable(item)
            for key, item in value.items()
            if item is not None and item != "" and item != []
        }
    if isinstance(value, list):
        return [strip_unavailable(item) for item in value if item is not None]
    return value


def parse_jwt_payload(token: str | None) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def base64url_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def openai_auth(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
    return value if isinstance(value, dict) else {}


def openai_profile(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("https://api.openai.com/profile") if isinstance(payload, dict) else {}
    return value if isinstance(value, dict) else {}


def normalize_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return text


def timestamp_from_unix_seconds(value: Any) -> str:
    try:
        seconds = float(value)
        if not seconds:
            return ""
        return normalize_timestamp(seconds)
    except Exception:
        return ""


def to_unix_seconds(value: Any) -> int:
    """Convert an ISO timestamp string, datetime, or numeric Unix timestamp to
    integer Unix seconds.  Returns 0 if the value is empty or unparseable.

    Used for sub2api's ``expires_at`` field which the Go server expects as
    ``*int64`` (Unix seconds), not an ISO string.
    """
    if isinstance(value, (int, float)):
        raw = float(value)
        return int(raw / 1000 if raw > 1e11 else raw)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        if text.isdigit():
            raw = int(text)
            return int(raw / 1000 if raw > 1e11 else raw)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def to_email_key(email: str) -> str:
    return str(email or "").strip().lower()


def build_synthetic_codex_id_token(email: str, account_id: str, plan_type: str = "", user_id: str = "", expires_at: str = "") -> str:
    if not account_id:
        return ""
    now = int(time.time())
    exp = now + 90 * 24 * 60 * 60
    if expires_at:
        try:
            exp = int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    auth = {"chatgpt_account_id": account_id}
    if plan_type:
        auth["chatgpt_plan_type"] = plan_type
    if user_id:
        auth["chatgpt_user_id"] = user_id
    payload = {
        "iat": now,
        "exp": exp,
        "https://api.openai.com/auth": auth,
    }
    if email:
        payload["email"] = email
        payload["https://api.openai.com/profile"] = {"email": email, **({"user_id": user_id} if user_id else {})}
    return f"{base64url_json({'alg': 'none', 'typ': 'JWT', 'synthetic': True})}.{base64url_json(payload)}."


def collect_session_like_objects(value: Any, source_name: str = "pasted-json") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            marker = id(item)
            if marker in seen:
                return
            seen.add(marker)
            token = first_non_empty(
                item.get("accessToken"),
                item.get("access_token"),
                nested(item, "tokens", "accessToken"),
                nested(item, "tokens", "access_token"),
                nested(item, "token", "accessToken"),
                nested(item, "token", "access_token"),
                nested(item, "credentials", "accessToken"),
                nested(item, "credentials", "access_token"),
                nested(item, "auth_session", "accessToken"),
                nested(item, "auth_session", "access_token"),
                nested(item, "auth_session", "session", "accessToken"),
                nested(item, "auth_session", "session", "access_token"),
            )
            has_identity = isinstance(item.get("user"), dict) or first_non_empty(
                item.get("email"),
                item.get("name"),
                item.get("label"),
                nested(item, "meta", "label"),
                nested(item, "tokens", "accountId"),
                nested(item, "tokens", "account_id"),
                nested(item, "tokens", "chatgptAccountId"),
                nested(item, "tokens", "chatgpt_account_id"),
                nested(item, "credentials", "accountId"),
                nested(item, "credentials", "account_id"),
                nested(item, "credentials", "chatgptAccountId"),
                nested(item, "credentials", "chatgpt_account_id"),
                nested(item, "credentials", "email"),
                nested(item, "providerSpecificData", "chatgptAccountId"),
                nested(item, "providerSpecificData", "chatgpt_account_id"),
                item.get("account_id"),
                item.get("chatgpt_account_id"),
                nested(item, "account", "id"),
                item.get("id"),
            )
            if token and has_identity:
                found.append({"value": item, "sourceName": source_name, "path": path})
                return
            for key, child in item.items():
                if key in {"accessToken", "access_token", "sessionToken", "session_token"}:
                    continue
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, "$")
    return found


def convert_session(record: dict[str, Any], now: Any | None = None, source_name: str = "pasted-json", source_path: str = "") -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("session is not a JSON object")

    access_token = first_non_empty(
        record.get("accessToken"), record.get("access_token"),
        nested(record, "tokens", "accessToken"), nested(record, "tokens", "access_token"),
        nested(record, "token", "accessToken"), nested(record, "token", "access_token"),
        nested(record, "credentials", "accessToken"), nested(record, "credentials", "access_token"),
        nested(record, "auth_session", "accessToken"), nested(record, "auth_session", "access_token"),
        nested(record, "auth_session", "session", "accessToken"), nested(record, "auth_session", "session", "access_token"),
    )
    if not access_token:
        raise ValueError("missing accessToken")
    session_token = first_non_empty(record.get("sessionToken"), record.get("session_token"), nested(record, "tokens", "sessionToken"), nested(record, "tokens", "session_token"))
    refresh_token = first_non_empty(record.get("refreshToken"), record.get("refresh_token"), nested(record, "tokens", "refreshToken"), nested(record, "tokens", "refresh_token"), nested(record, "token", "refreshToken"), nested(record, "token", "refresh_token"), nested(record, "credentials", "refresh_token"))
    input_id_token = first_non_empty(record.get("idToken"), record.get("id_token"), nested(record, "tokens", "idToken"), nested(record, "tokens", "id_token"), nested(record, "token", "idToken"), nested(record, "token", "id_token"), nested(record, "credentials", "id_token"))

    payload = parse_jwt_payload(access_token)
    id_payload = parse_jwt_payload(input_id_token)
    auth = openai_auth(payload)
    id_auth = openai_auth(id_payload)
    profile = openai_profile(payload)
    expires_at = "" if refresh_token else first_non_empty(
        timestamp_from_unix_seconds(payload.get("exp")),
        normalize_timestamp(record.get("expires")),
        normalize_timestamp(record.get("expiresAt")),
        normalize_timestamp(record.get("expired")),
        normalize_timestamp(record.get("expires_at")),
    )
    access_token_expires_at = "" if refresh_token else timestamp_from_unix_seconds(payload.get("exp"))
    email = first_non_empty(nested(record, "user", "email"), record.get("email"), nested(record, "meta", "label"), record.get("label"), nested(record, "credentials", "email"), nested(record, "providerSpecificData", "email"), profile.get("email"), id_payload.get("email"), payload.get("email"))
    account_id = first_non_empty(
        nested(record, "account", "id"), record.get("account_id"), record.get("accountId"),
        record.get("chatgptAccountId"), record.get("chatgpt_account_id"),
        nested(record, "meta", "chatgptAccountId"), nested(record, "meta", "chatgpt_account_id"),
        nested(record, "tokens", "accountId"), nested(record, "tokens", "account_id"),
        nested(record, "tokens", "chatgptAccountId"), nested(record, "tokens", "chatgpt_account_id"),
        nested(record, "providerSpecificData", "chatgptAccountId"), nested(record, "providerSpecificData", "chatgpt_account_id"),
        nested(record, "credentials", "chatgpt_account_id"),
        auth.get("chatgpt_account_id"), id_auth.get("chatgpt_account_id"),
        record.get("id") if record.get("provider") == "codex" else "",
    )
    chatgpt_account_id = first_non_empty(record.get("chatgptAccountId"), record.get("chatgpt_account_id"), account_id)
    workspace_id = first_non_empty(nested(record, "account", "workspaceId"), nested(record, "account", "workspace_id"), record.get("workspaceId"), record.get("workspace_id"), nested(record, "meta", "workspaceId"), nested(record, "meta", "workspace_id"), nested(record, "providerSpecificData", "workspaceId"), nested(record, "providerSpecificData", "workspace_id"), payload.get("workspace_id"), id_payload.get("workspace_id"))
    user_id = first_non_empty(nested(record, "user", "id"), record.get("user_id"), record.get("userId"), record.get("chatgptUserId"), record.get("chatgpt_user_id"), nested(record, "providerSpecificData", "chatgptUserId"), nested(record, "providerSpecificData", "chatgpt_user_id"), auth.get("chatgpt_user_id"), auth.get("user_id"), id_auth.get("chatgpt_user_id"), id_auth.get("user_id"), profile.get("user_id"), payload.get("sub"))
    plan_type = first_non_empty(nested(record, "account", "planType"), nested(record, "account", "plan_type"), record.get("planType"), record.get("plan_type"), nested(record, "providerSpecificData", "chatgptPlanType"), nested(record, "providerSpecificData", "chatgpt_plan_type"), nested(record, "credentials", "plan_type"), auth.get("chatgpt_plan_type"), id_auth.get("chatgpt_plan_type"))
    exported_at = normalize_timestamp(now or datetime.now(timezone.utc))
    name = first_non_empty(email, source_name, "ChatGPT Account")
    synthetic_id_token = "" if input_id_token else build_synthetic_codex_id_token(email, account_id, plan_type, user_id, expires_at)
    id_token = first_non_empty(input_id_token, synthetic_id_token)

    cpa = strip_unavailable({
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "id_token_synthetic": bool(synthetic_id_token) or None,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "session_token": session_token,
        "last_refresh": exported_at,
        "expired": expires_at,
        "disabled": bool(record.get("disabled")) or None,
    })
    cockpit = {
        "type": "codex",
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "account_id": account_id,
        "last_refresh": exported_at,
        "email": email,
        "expired": expires_at,
        "account_note": first_non_empty(record.get("account_note"), record.get("accountInfo"), record.get("account_info"), record.get("note"), record.get("notes"), record.get("remark")),
    }
    sub2api_account = strip_unavailable({
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "expires_at": to_unix_seconds(access_token_expires_at) or None,
        "auto_pause_on_expired": True if access_token_expires_at else None,
        "concurrency": 10,
        "priority": 1,
        "credentials": {
            "access_token": access_token,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "email": email,
            "expires_at": expires_at,
            "plan_type": plan_type,
        },
        "extra": {"email": email, "email_key": to_email_key(email), "name": name, "source": "chatgpt_web_session", "last_refresh": exported_at},
    })
    nine_router = strip_unavailable({
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "testStatus": first_non_empty(record.get("testStatus"), record.get("test_status"), "active"),
        "providerSpecificData": {"chatgptAccountId": account_id, "chatgptPlanType": plan_type},
        "id": account_id,
        "provider": "codex",
        "authType": "oauth",
        "name": name,
        "email": email,
        "priority": int(record.get("priority") or 9),
        "isActive": not bool(record.get("disabled")),
        "createdAt": normalize_timestamp(record.get("createdAt")) or exported_at,
        "updatedAt": normalize_timestamp(record.get("updatedAt")) or exported_at,
    })
    codex_auth_json = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"id_token": id_token, "access_token": access_token, "refresh_token": refresh_token or "", "account_id": account_id}, "last_refresh": exported_at}
    axonhub_refresh = refresh_token or AXONHUB_PLACEHOLDER_REFRESH_TOKEN
    axonhub = strip_unavailable({"auth_mode": "chatgpt", "last_refresh": expires_at or exported_at, "tokens": {"access_token": access_token, "refresh_token": axonhub_refresh, "id_token": id_token}, "axonhub_refresh_token_placeholder": None if refresh_token else True, "axonhub_note": None if refresh_token else "refresh_token is a placeholder; access_token works only until it expires."})
    codex_manager = {"tokens": strip_unavailable({"access_token": access_token, "refresh_token": refresh_token or "", "id_token": input_id_token or "", "account_id": account_id, "chatgpt_account_id": chatgpt_account_id}), "meta": strip_unavailable({"label": name, "workspace_id": workspace_id, "chatgpt_account_id": chatgpt_account_id, "note": "Imported from ChatGPT session"})}
    return {
        "sourceName": source_name,
        "sourcePath": source_path,
        "email": email,
        "name": name,
        "expiresAt": expires_at,
        "accessTokenExpiresAt": access_token_expires_at,
        "cpa": cpa,
        "cockpit": cockpit,
        "nineRouter": nine_router,
        "codexAuthJson": codex_auth_json,
        "axonHub": axonhub,
        "codexManager": codex_manager,
        "sub2apiAccount": sub2api_account,
    }


def build_output_document(fmt: str, converted: list[dict[str, Any]], now: Any | None = None) -> Any:
    fmt = str(fmt or "sub2api").lower()
    if fmt == "sub2api":
        return {"exported_at": normalize_timestamp(now or datetime.now(timezone.utc)), "proxies": [], "accounts": [item["sub2apiAccount"] for item in converted]}
    mapping = {
        "cpa": "cpa",
        "cockpit": "cockpit",
        "9router": "nineRouter",
        "codex": "codexAuthJson",
        "axonhub": "axonHub",
        "codexmanager": "codexManager",
    }
    key = mapping.get(fmt, "cpa")
    docs = [item[key] for item in converted]
    return docs[0] if len(docs) == 1 else docs


def convert_sources(sources: list[dict[str, Any]], fmt: str = "sub2api", now: Any | None = None) -> dict[str, Any]:
    converted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for index, item in enumerate(sources):
        try:
            converted.append(convert_session(item["value"], now=now, source_name=item.get("sourceName") or "pasted-json", source_path=item.get("path") or f"$[{index}]"))
        except Exception as exc:
            skipped.append({"sourceName": item.get("sourceName") or "pasted-json", "path": item.get("path") or "", "reason": str(exc)})
    output = build_output_document(fmt, converted, now=now) if converted else None
    return {"sessions": sources, "converted": converted, "skipped": skipped, "output": output, "outputText": json.dumps(output, ensure_ascii=False, indent=2) if output is not None else ""}


def convert_json_value(value: Any, fmt: str = "sub2api", source_name: str = "json") -> dict[str, Any]:
    return convert_sources(collect_session_like_objects(value, source_name=source_name), fmt=fmt)


def convert_json_file(path: str | Path, fmt: str = "sub2api") -> dict[str, Any]:
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    return convert_json_value(data, fmt=fmt, source_name=str(target))
