from curl_cffi import requests as curl_requests


class MailboxTokenExpiredError(RuntimeError):
    """Raised when the mailbox refresh token is permanently invalid (invalid_grant)."""
    pass


def ms_oauth_refresh(mailbox, cfg, proxy=None, scope_override=None):
    client_id = getattr(mailbox, "token", "") or cfg.get("oauth_client_id", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
    scope = scope_override or cfg.get("oauth_scope", "https://graph.microsoft.com/.default offline_access")
    token_url = cfg.get("oauth_token_url", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    if not mailbox.refresh_token:
        raise RuntimeError("mailbox refresh_token is required")
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": mailbox.refresh_token,
        "scope": scope,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = curl_requests.post(token_url, data=data, proxies=proxies, impersonate="chrome124", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code != 200:
        error_code = str((body.get("error_codes") or [body.get("error") or ""])[0]) if isinstance(body, dict) else ""
        if "invalid_grant" in str(body).lower() or "9002313" in error_code:
            raise MailboxTokenExpiredError(f"mailbox token expired (invalid_grant): {mailbox.email}")
        raise RuntimeError(f"mailbox token refresh failed: {body}")
    access_token = body.get("access_token", "")
    if not access_token:
        raise RuntimeError("mailbox token refresh returned empty access token")
    if body.get("refresh_token"):
        mailbox.refresh_token = body["refresh_token"]
    mailbox.access_token = access_token
    return access_token
