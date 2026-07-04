from .k12_identity import _extract_account_id_from_data, _extract_user_id_from_data


def _verify_workspace_session(account, expected_workspace_id, proxy=None, timeout=30, fetch_auth_session_func=None):
    expected = str(expected_workspace_id or "").strip()
    if not expected:
        return {"ok": False, "error": "missing_expected_workspace_id"}
    if not str(account.get("cookie_header") or "").strip():
        return {"ok": False, "error": "missing_session_cookie", "expected_workspace_id": expected}
    if fetch_auth_session_func is None:
        raise RuntimeError("fetch_auth_session_func is required")
    try:
        body = fetch_auth_session_func(account, proxy=proxy, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "expected_workspace_id": expected}
    actual = _extract_account_id_from_data(body) or str(account.get("account_id") or "").strip()
    user_id = _extract_user_id_from_data(body) or str(account.get("user_id") or "").strip()
    return {
        "ok": bool(actual and actual == expected),
        "expected_workspace_id": expected,
        "actual_workspace_id": actual,
        "user_id": user_id,
        **({} if actual == expected else {"error": "workspace_not_switched"}),
    }
