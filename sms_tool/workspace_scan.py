"""Workspace health and protocol switching helpers for one-click scan."""

import json
import time

from .codex_oauth import select_workspace_by_cookie
from .http_client import is_transient_transport_error
from .k12_client import _fetch_auth_session_from_cookie
from .k12_identity import _extract_account_id_from_data, _extract_access_token, _token_account_id


WORKSPACE_DISABLED_MARKERS = (
    "workspace_deactivated",
    "workspace_disabled",
    "workspace suspended",
    "workspace has been disabled",
    "workspace has been deactivated",
    "account_deactivated",
    "deleted or deactivated",
    "account has been deleted",
    "account has been deactivated",
    "organization_disabled",
    "organization deactivated",
    "organization suspended",
    "not a member of this workspace",
    "workspace_not_found",
)


def parse_workspace_fallback_ids(value=""):
    output = []
    seen = set()
    for chunk in str(value or "").replace("\n", ",").replace(";", ",").split(","):
        item = chunk.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def inspect_workspace(
    data,
    proxy=None,
    timeout=30,
    enabled=True,
    target_workspace_id="",
    fallback_workspace_ids=None,
    auto_switch=False,
    fetch_auth_session_func=None,
    select_workspace_func=None,
):
    """Inspect current workspace and optionally switch to a fallback workspace.

    The check is intentionally cookie/session based: exported ATs can be stale
    or personal-scoped, so /api/auth/session is treated as the source of truth
    when a saved ChatGPT browser session cookie is available.
    """
    if not enabled:
        return {"ok": True, "status": "workspace_check_disabled"}
    account = data if isinstance(data, dict) else {}
    now = int(time.time())
    token_workspace_id = _token_account_id(_extract_access_token(account))
    account_type_before = _account_type(account)
    result = {
        "ok": False,
        "status": "workspace_unknown",
        "account_type_before": account_type_before,
        "account_type_after": account_type_before,
        "token_workspace_id": token_workspace_id,
        "actual_workspace_id": "",
        "actual_workspace_name": "",
        "workspace_name": "",
        "target_workspace_id": str(target_workspace_id or "").strip(),
        "fallback_workspace_ids": parse_workspace_fallback_ids(",".join(fallback_workspace_ids or [])),
        "updated_at": now,
    }

    fetch_result = _fetch_workspace_session(
        account,
        proxy=proxy,
        timeout=timeout,
        fetch_auth_session_func=fetch_auth_session_func,
    )
    result["fetch"] = fetch_result
    actual = str(fetch_result.get("actual_workspace_id") or "").strip()
    result["actual_workspace_id"] = actual
    result["actual_workspace_name"] = str(fetch_result.get("actual_workspace_name") or "")
    result["workspace_name"] = str(fetch_result.get("actual_workspace_name") or "")
    available_workspaces = fetch_result.get("available_workspaces") if isinstance(fetch_result.get("available_workspaces"), list) else []
    if available_workspaces:
        result["available_workspaces"] = available_workspaces
    if fetch_result.get("ok"):
        result["status"] = "workspace_ok"
        result["ok"] = True
    elif _looks_workspace_deactivated(fetch_result):
        result["status"] = "workspace_deactivated"
        result["error"] = "workspace_deactivated"
    elif fetch_result.get("inconclusive") or fetch_result.get("error") == "workspace_check_transport_error":
        result["status"] = "workspace_check_inconclusive"
        result["error"] = str(fetch_result.get("transport_error") or fetch_result.get("error") or "workspace_check_inconclusive")
        result["inconclusive"] = True
    elif fetch_result.get("error") == "missing_session_cookie":
        # AT claim is still useful for inventory, but not enough to prove live
        # workspace state or perform a protocol switch.
        result["status"] = "workspace_cookie_missing"
        result["error"] = "missing_session_cookie"
    else:
        result["status"] = "workspace_check_failed"
        result["error"] = str(fetch_result.get("error") or "workspace_check_failed")

    target = str(target_workspace_id or "").strip()
    if target and actual and actual != target and result["status"] == "workspace_ok":
        result["status"] = "workspace_not_target"
        result["ok"] = False
        result["error"] = "workspace_not_target"

    fallback_candidates = list(fallback_workspace_ids or [])
    if account_type_before == "k12" and result["status"] in {"workspace_deactivated", "workspace_check_failed", "workspace_not_target"}:
        fallback_candidates.extend(_available_workspace_ids(available_workspaces, exclude={actual}))
    switch_ids = _switch_candidates(target, fallback_candidates, actual)
    should_switch = (
        bool(auto_switch or target or fallback_workspace_ids or (account_type_before == "k12" and result["status"] == "workspace_deactivated"))
        and bool(switch_ids)
        and (target or result["status"] != "workspace_ok")
    )
    if should_switch:
        switch = switch_workspace(
            account,
            switch_ids,
            proxy=proxy,
            timeout=timeout,
            fetch_auth_session_func=fetch_auth_session_func,
            select_workspace_func=select_workspace_func,
        )
        result["switch"] = switch
        result["switch_status"] = str(switch.get("status") or "")
        result["switch_error"] = str(switch.get("error") or "")
        if switch.get("ok"):
            result["ok"] = True
            result["status"] = "workspace_switched"
            result["actual_workspace_id"] = str(switch.get("actual_workspace_id") or switch.get("workspace_id") or "")
            result["actual_workspace_name"] = str(switch.get("actual_workspace_name") or "")
            result["workspace_name"] = str(switch.get("actual_workspace_name") or "")
            result["switched_workspace_id"] = str(switch.get("workspace_id") or "")
            result["account_type_after"] = _workspace_account_type(_workspace_by_id(available_workspaces, result["actual_workspace_id"])) or account_type_before
        elif result["status"] in {"workspace_deactivated", "workspace_not_target", "workspace_check_failed"}:
            result["status"] = "workspace_switch_failed"
            result["ok"] = False
            result["error"] = str(switch.get("error") or result.get("error") or "workspace_switch_failed")

    if account_type_before == "k12" and result["status"] in {"workspace_deactivated", "workspace_switch_failed"}:
        personal = _personal_workspace(available_workspaces)
        if personal:
            switch = switch_workspace(
                account,
                [personal.get("id")],
                proxy=proxy,
                timeout=timeout,
                fetch_auth_session_func=fetch_auth_session_func,
                select_workspace_func=select_workspace_func,
            )
            result["free_fallback"] = switch
            if switch.get("ok"):
                result["ok"] = True
                result["status"] = "workspace_fallback_free"
                result["actual_workspace_id"] = str(switch.get("actual_workspace_id") or switch.get("workspace_id") or "")
                result["actual_workspace_name"] = str(switch.get("actual_workspace_name") or personal.get("name") or personal.get("title") or "")
                result["workspace_name"] = result["actual_workspace_name"]
                result["account_type_after"] = "free"
                result.pop("error", None)
        elif not switch_ids:
            result["ok"] = True
            result["status"] = "workspace_fallback_free"
            result["account_type_after"] = "free"
            result["error"] = "no_available_workspace_fallback_to_free"

    return _public_workspace_result(result)


def switch_workspace(
    account,
    workspace_ids,
    proxy=None,
    timeout=30,
    fetch_auth_session_func=None,
    select_workspace_func=None,
):
    cookie = str((account or {}).get("cookie_header") or "").strip()
    if not cookie:
        return {"ok": False, "status": "workspace_switch_failed", "error": "missing_session_cookie"}
    attempts = []
    selector = select_workspace_func or select_workspace_by_cookie
    fetcher = fetch_auth_session_func or _fetch_auth_session_from_cookie
    for workspace_id in parse_workspace_fallback_ids(",".join(workspace_ids or [])):
        selected = selector(
            cookie,
            workspace_id,
            device_id=str((account or {}).get("device_id") or ""),
            proxy=proxy,
            timeout=timeout,
        )
        attempt = {"workspace_id": workspace_id, "select": _safe_public(selected)}
        if not selected.get("ok"):
            attempts.append(attempt)
            continue
        try:
            body = fetcher(account, proxy=proxy, timeout=timeout)
            workspaces = _extract_workspace_candidates(body)
            actual = _extract_account_id_from_data(body)
            if not actual and len(workspaces) == 1:
                actual = str((workspaces[0] or {}).get("id") or "").strip()
            actual_info = _workspace_by_id(workspaces, actual)
            attempt["actual_workspace_id"] = actual
            attempt["actual_workspace_name"] = str(actual_info.get("name") or actual_info.get("title") or "")
            attempt["fetch"] = {"ok": True, "actual_workspace_id": actual, "actual_workspace_name": attempt["actual_workspace_name"]}
            attempts.append(attempt)
            if actual == workspace_id:
                return {
                    "ok": True,
                    "status": "workspace_switched",
                    "workspace_id": workspace_id,
                    "actual_workspace_id": actual,
                    "actual_workspace_name": attempt["actual_workspace_name"],
                    "attempts": attempts,
                }
        except Exception as exc:
            attempt["fetch"] = {"ok": False, "error": str(exc)[:300]}
            attempts.append(attempt)
    return {
        "ok": False,
        "status": "workspace_switch_failed",
        "error": "no_fallback_workspace_selected",
        "attempts": attempts,
    }


def _fetch_workspace_session(account, proxy=None, timeout=30, fetch_auth_session_func=None):
    if not str((account or {}).get("cookie_header") or "").strip():
        return {"ok": False, "error": "missing_session_cookie"}
    try:
        body = (fetch_auth_session_func or _fetch_auth_session_from_cookie)(account, proxy=proxy, timeout=timeout)
    except Exception as exc:
        if is_transient_transport_error(exc):
            return {
                "ok": False,
                "error": "workspace_check_transport_error",
                "transport_error": str(exc)[:300],
                "inconclusive": True,
            }
        return {"ok": False, "error": str(exc)[:300]}
    actual = _extract_account_id_from_data(body)
    workspaces = _extract_workspace_candidates(body)
    if not actual and len(workspaces) == 1:
        actual = str((workspaces[0] or {}).get("id") or "").strip()
    actual_info = _workspace_by_id(workspaces, actual)
    disabled = _looks_workspace_deactivated(actual_info) or _looks_workspace_deactivated(body)
    account_type = _account_type(account)
    no_workspace = bool(not actual_info and not workspaces and account_type == "free" and not disabled)
    return {
        "ok": bool((actual_info or no_workspace) and not disabled),
        "actual_workspace_id": actual,
        "actual_workspace_name": str(actual_info.get("name") or actual_info.get("title") or ""),
        "available_workspaces": _public_workspaces(workspaces),
        **({"no_workspace": True} if no_workspace else {}),
        **({"error": "workspace_deactivated"} if disabled else {}),
    }


def _switch_candidates(target_workspace_id, fallback_workspace_ids, current_workspace_id=""):
    candidates = []
    for value in [target_workspace_id, *(fallback_workspace_ids or [])]:
        value = str(value or "").strip()
        if value and value != current_workspace_id and value not in candidates:
            candidates.append(value)
    return candidates


def _looks_workspace_deactivated(value):
    if isinstance(value, dict):
        if value.get("disabled") is True or value.get("deactivated") is True:
            return True
        status = str(value.get("status") or value.get("error") or "").strip().lower()
        if status in {"disabled", "deactivated", "workspace_disabled", "workspace_deactivated"}:
            return True
    try:
        text = json.dumps(value, ensure_ascii=False).lower()
    except Exception:
        text = str(value or "").lower()
    return any(marker in text for marker in WORKSPACE_DISABLED_MARKERS)


def _account_type(data):
    if not isinstance(data, dict):
        return "free"
    token_account = _token_account_id(_extract_access_token(data))
    k12_status = str(data.get("k12_status") or _nested_string(data.get("k12"), "status") or "").strip().lower()
    if (token_account and str(data.get("k12_workspace_id") or "") == token_account) or (
        ("k12" in k12_status or "edu" in k12_status)
        and "left" not in k12_status
        and "fallback_free" not in k12_status
    ):
        return "k12"
    for value in (
        data.get("subscription_type"),
        data.get("plan_type"),
        data.get("planType"),
        _nested_string(data.get("account"), "plan_type"),
        _nested_string(data.get("account"), "planType"),
        _nested_string(data.get("auth_session"), "account", "plan_type"),
        _nested_string(data.get("auth_session"), "account", "planType"),
    ):
        normalized = _normalize_account_type(value)
        if normalized:
            return normalized
    for workspace in _extract_workspace_candidates(data.get("auth_session")):
        normalized = _normalize_account_type((workspace or {}).get("account_type"))
        if normalized:
            return normalized
    return "free"


def _workspace_account_type(workspace):
    if not isinstance(workspace, dict):
        return ""
    text = json.dumps(workspace, ensure_ascii=False).lower()
    if workspace.get("is_personal") is True or workspace.get("personal") is True or "personal" in text:
        return "free"
    if "k12" in text or "edu" in text:
        return "k12"
    if "team" in text or "business" in text:
        return "team"
    if "plus" in text:
        return "plus"
    if "pro" in text:
        return "pro"
    if "free" in text:
        return "free"
    return ""


def _normalize_account_type(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "k12" in text or "edu" in text:
        return "k12"
    if "team" in text or "business" in text or "enterprise" in text:
        return "team"
    if "plus" in text:
        return "plus"
    if "pro" in text:
        return "pro"
    if "free" in text or "personal" in text:
        return "free"
    return ""


def _nested_string(node, *path):
    current = node
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _extract_workspace_candidates(value):
    found = []
    seen = set()

    def visit(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            node_id = str(node.get("id") or node.get("workspace_id") or node.get("workspaceId") or node.get("account_id") or node.get("accountId") or "").strip()
            has_workspace_shape = node_id and any(k in node for k in ("name", "title", "is_personal", "isPersonal", "disabled", "deactivated", "role", "plan_type", "planType"))
            if has_workspace_shape and node_id not in seen:
                seen.add(node_id)
                found.append({
                    "id": node_id,
                    "name": str(node.get("name") or node.get("title") or ""),
                    "title": str(node.get("title") or node.get("name") or ""),
                    "is_personal": bool(node.get("is_personal") or node.get("isPersonal") or node.get("personal")),
                    "disabled": bool(node.get("disabled") or node.get("deactivated")),
                    "account_type": _workspace_account_type(node),
                })
            for key, child in node.items():
                if key.lower() in {"access_token", "accesstoken", "refresh_token", "refreshtoken", "cookie", "cookie_header"}:
                    continue
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value)
    return found


def _public_workspaces(workspaces):
    output = []
    for item in workspaces or []:
        if not isinstance(item, dict):
            continue
        if _looks_workspace_deactivated(item):
            continue
        output.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or item.get("title") or ""),
            "is_personal": bool(item.get("is_personal")),
            "account_type": str(item.get("account_type") or ""),
        })
    return output


def _workspace_by_id(workspaces, workspace_id):
    workspace_id = str(workspace_id or "").strip()
    for item in workspaces or []:
        if str((item or {}).get("id") or "").strip() == workspace_id:
            return item or {}
    return {}


def _available_workspace_ids(workspaces, exclude=None):
    exclude = {str(x or "").strip() for x in (exclude or set()) if str(x or "").strip()}
    ids = []
    for item in workspaces or []:
        wid = str((item or {}).get("id") or "").strip()
        if not wid or wid in exclude:
            continue
        if _looks_workspace_deactivated(item):
            continue
        if (item or {}).get("is_personal"):
            continue
        if wid not in ids:
            ids.append(wid)
    return ids


def _personal_workspace(workspaces):
    for item in workspaces or []:
        if (item or {}).get("is_personal") and not _looks_workspace_deactivated(item):
            return item
    return {}


def _public_workspace_result(result):
    output = _safe_public(result)
    fetch = output.get("fetch")
    if isinstance(fetch, dict):
        fetch.pop("body", None)
    return output


def _safe_public(value):
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key.lower() in {"access_token", "authorization", "cookie", "cookie_header"}:
                continue
            output[key] = _safe_public(item)
        return output
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    if isinstance(value, str) and len(value) > 300:
        return value[:300]
    return value
