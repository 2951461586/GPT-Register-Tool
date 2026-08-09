"""Auth-session diagnostics for protocol registration flows.

This module keeps the diagnostic seam small: callers ask for a redacted
``client_auth_session_dump`` summary and do not need to know which fields are
sensitive or how the nested auth-state payload is shaped.
"""

import json

from .auth_headers import auth_impersonate
from .http_client import request_with_retry
from .http_utils import _json_or_raw


def _redact_auth_dump_value(value):
    if isinstance(value, str):
        text = value.strip()
        return f"[REDACTED](len={len(text)})" if text else ""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return f"<list:{len(value)}>"
    if isinstance(value, dict):
        return f"<dict:{len(value)}>"
    return f"<{type(value).__name__}>"


def _find_auth_dump_keys(data, wanted):
    found = {}
    wanted_lc = {str(key).lower() for key in wanted}

    def walk(node, path="", depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_s = str(key)
                key_lc = key_s.lower()
                next_path = f"{path}.{key_s}" if path else key_s
                if key_lc in wanted_lc or any(part in key_lc for part in ("verifier", "session", "challenge")):
                    found.setdefault(next_path, _redact_auth_dump_value(value))
                if isinstance(value, (dict, list)):
                    walk(value, next_path, depth + 1)
        elif isinstance(node, list):
            for idx, item in enumerate(node[:10]):
                walk(item, f"{path}[{idx}]", depth + 1)

    walk(data)
    return found


def auth_dump_summary(data):
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    client_auth_session = data.get("client_auth_session") or data.get("clientAuthSession") or {}
    return {
        "top_keys": sorted(str(k) for k in list(data.keys())[:20]),
        "client_auth_session_keys": sorted(str(k) for k in list(client_auth_session.keys())[:20]) if isinstance(client_auth_session, dict) else [],
        "signals": _find_auth_dump_keys(
            data,
            {
                "state",
                "session_id",
                "sessionId",
                "flow",
                "page_type",
                "pageType",
                "continue_url",
                "continueUrl",
                "login_verifier",
                "verifier",
                "email_verification_mode",
            },
        ),
    }


def fetch_client_auth_session_dump(session, auth_base, base_headers, stage=""):
    try:
        response = request_with_retry(
            session,
            "get",
            f"{auth_base}/api/accounts/client_auth_session_dump",
            label=f"client_auth_session_dump {stage or 'default'}",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{auth_base}/email-verification"},
            impersonate=auth_impersonate(),
        )
    except Exception as exc:
        print(f"  client_auth_session_dump[{stage or 'default'}] warning: {exc}")
        return {}
    body = _json_or_raw(response, limit=1200)
    if getattr(response, "status_code", 0) != 200:
        print(f"  client_auth_session_dump[{stage or 'default'}]: {response.status_code}")
        return {"status": getattr(response, "status_code", 0), "body": body}
    summary = auth_dump_summary(body)
    print(f"  client_auth_session_dump[{stage or 'default'}]: {json.dumps(summary, ensure_ascii=False)[:800]}")
    return summary
