import json

def _extract_access_token(data):
    if not isinstance(data, dict):
        return ""
    candidates = (
        data.get("access_token"),
        data.get("accessToken"),
        _get_nested(data, ("tokens", "access_token")),
        _get_nested(data, ("tokens", "accessToken")),
        _get_nested(data, ("token", "access_token")),
        _get_nested(data, ("token", "accessToken")),
        _get_nested(data, ("credentials", "access_token")),
        _get_nested(data, ("credentials", "accessToken")),
        (data.get("auth_session") or {}).get("accessToken") if isinstance(data.get("auth_session"), dict) else "",
        (data.get("auth_session") or {}).get("access_token") if isinstance(data.get("auth_session"), dict) else "",
        _get_nested(data, ("auth_session", "session", "accessToken")),
        _get_nested(data, ("auth_session", "session", "access_token")),
    )
    for token in candidates:
        token = str(token or "").strip()
        if token:
            return token
    return ""


def _extract_refresh_token(data):
    """Return an OpenAI/Codex refresh token, avoiding mailbox provider RTs."""
    if not isinstance(data, dict):
        return ""
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    codex_session = data.get("codex_session") if isinstance(data.get("codex_session"), dict) else {}
    candidates = (
        data.get("oauth_refresh_token"),
        data.get("refresh_token"),
        data.get("refreshToken"),
        codex_session.get("refresh_token"),
        codex_session.get("refreshToken"),
        auth_session.get("refreshToken"),
        auth_session.get("refresh_token"),
        session.get("refreshToken"),
        session.get("refresh_token"),
    )
    for token in candidates:
        token = str(token or "").strip()
        if token.startswith(("rt.", "rt_")):
            return token
    return ""


def _extract_id_token(data):
    if not isinstance(data, dict):
        return ""
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    codex_session = data.get("codex_session") if isinstance(data.get("codex_session"), dict) else {}
    candidates = (
        data.get("id_token"),
        data.get("idToken"),
        codex_session.get("id_token"),
        codex_session.get("idToken"),
        auth_session.get("idToken"),
        auth_session.get("id_token"),
        session.get("idToken"),
        session.get("id_token"),
    )
    for token in candidates:
        token = str(token or "").strip()
        if token:
            return token
    return ""


def _jwt_claims(token):
    try:
        import base64

        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _base64url_json(value):
    import base64

    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token_account_id(token):
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    return str(auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id") or "").strip()


def _token_user_id(token):
    claims = _jwt_claims(token)
    profile = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    return str(
        profile.get("user_id")
        or profile.get("id")
        or auth.get("user_id")
        or auth.get("userId")
        or claims.get("user_id")
        or claims.get("userId")
        or claims.get("sub")
        or ""
    ).strip()


def _get_nested(data, path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _extract_user_id_from_data(data):
    if not isinstance(data, dict):
        return ""
    candidates = (
        data.get("user_id"),
        data.get("userId"),
        data.get("chatgpt_user_id"),
        data.get("chatgptUserId"),
        _get_nested(data, ("user", "id")),
        _get_nested(data, ("account", "user_id")),
        _get_nested(data, ("account", "userId")),
        _get_nested(data, ("tokens", "user_id")),
        _get_nested(data, ("tokens", "chatgpt_user_id")),
        _get_nested(data, ("credentials", "user_id")),
        _get_nested(data, ("providerSpecificData", "chatgpt_user_id")),
        _get_nested(data, ("providerSpecificData", "chatgptUserId")),
        _get_nested(data, ("auth_session", "user", "id")),
        _get_nested(data, ("auth_session", "session", "user", "id")),
        _get_nested(data, ("codex_session", "user", "id")),
        _get_nested(data, ("session", "user", "id")),
    )
    for value in candidates:
        value = str(value or "").strip()
        if value:
            return value
    for token_key in ("id_token", "access_token", "accessToken"):
        user_id = _token_user_id(str(data.get(token_key) or "").strip())
        if user_id:
            return user_id
    return ""


def _extract_account_id_from_data(data):
    if not isinstance(data, dict):
        return ""
    candidates = (
        data.get("account_id"),
        data.get("accountId"),
        data.get("chatgpt_account_id"),
        data.get("chatgptAccountId"),
        data.get("workspace_id"),
        data.get("workspaceId"),
        data.get("k12_workspace_id"),
        _get_nested(data, ("account", "id")),
        _get_nested(data, ("tokens", "account_id")),
        _get_nested(data, ("tokens", "accountId")),
        _get_nested(data, ("tokens", "chatgpt_account_id")),
        _get_nested(data, ("tokens", "chatgptAccountId")),
        _get_nested(data, ("credentials", "account_id")),
        _get_nested(data, ("credentials", "chatgpt_account_id")),
        _get_nested(data, ("providerSpecificData", "chatgpt_account_id")),
        _get_nested(data, ("providerSpecificData", "chatgptAccountId")),
        _get_nested(data, ("auth_session", "account", "id")),
        _get_nested(data, ("auth_session", "session", "account", "id")),
        _get_nested(data, ("codex_session", "account", "id")),
        _get_nested(data, ("session", "account", "id")),
    )
    for value in candidates:
        value = str(value or "").strip()
        if value:
            return value
    for token_key in ("id_token", "access_token", "accessToken"):
        account_id = _token_account_id(str(data.get(token_key) or "").strip())
        if account_id:
            return account_id
    return ""

