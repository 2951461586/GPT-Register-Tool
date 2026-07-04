import re
import time
from html import unescape
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as curl_requests

def _mailbox_from_account(account):
    mailbox = account.get("mailbox") if isinstance(account.get("mailbox"), dict) else {}
    provider = str(mailbox.get("provider") or "").strip()
    email = str(mailbox.get("email") or account.get("email") or "").strip()
    refresh_token = str(mailbox.get("refresh_token") or "").strip()
    if not email:
        return None
    if provider != "cfworker" and not refresh_token:
        return None
    from .mailbox import MailboxAccount
    return MailboxAccount(
        email=email,
        password=str(mailbox.get("password") or "").strip(),
        refresh_token=refresh_token,
        access_token=str(mailbox.get("access_token") or "").strip(),
        token=str(mailbox.get("token") or "").strip(),
        source=str(mailbox.get("source") or "").strip(),
        provider=provider,
    )


def _message_text(msg):
    body = (msg or {}).get("body")
    if isinstance(body, dict):
        body = body.get("content") or ""
    parts = [
        str((msg or {}).get("subject") or ""),
        str((msg or {}).get("bodyPreview") or ""),
        str(body or ""),
        str((msg or {}).get("text") or ""),
        str((msg or {}).get("html") or ""),
    ]
    return unescape("\n".join(parts))


def _extract_invite_url(text):
    cleaned = unescape(str(text or "")).replace("\\u0026", "&")
    for match in re.findall(r"https?://[^\s\"'<>）)]+", cleaned, flags=re.I):
        url = match.rstrip(".,;!]")
        lower = url.lower()
        if "k12-invite" in lower or ("chatgpt.com" in lower and "invite" in lower):
            return url
    return ""


def _workspace_id_from_invite_url(invite_url, fallback_ids=None):
    fallback_ids = fallback_ids or []
    try:
        parsed = urlparse(str(invite_url or ""))
        values = parse_qs(parsed.query)
        for key in ("wId", "wid", "workspace_id", "workspaceId"):
            value = (values.get(key) or [""])[0]
            if value:
                return str(value).strip()
        path_match = re.search(r"/accounts/([0-9a-f-]{20,})/|/workspace/([0-9a-f-]{20,})", parsed.path, re.I)
        if path_match:
            return next(v for v in path_match.groups() if v)
    except Exception:
        pass
    return str(fallback_ids[0] if fallback_ids else "").strip()


def _poll_k12_invite_url(account, timeout=240, proxy=None):
    mailbox = _mailbox_from_account(account)
    if mailbox is None:
        return {"ok": False, "error": "missing_mailbox_credentials"}
    from .mailbox import _fetch_mailbox_messages
    deadline = time.time() + max(1, int(timeout or 240))
    interval = 3.0
    last_error = "invite_not_found"
    while time.time() < deadline:
        try:
            for msg in _fetch_mailbox_messages(mailbox, limit=25, proxy=proxy):
                text = _message_text(msg)
                if "k12-invite" not in text.lower() and "invite" not in text.lower():
                    continue
                url = _extract_invite_url(text)
                if url:
                    return {"ok": True, "url": url, "mailbox": mailbox.email}
        except Exception as exc:
            last_error = str(exc)
        else:
            last_error = "invite_not_found"
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return {"ok": False, "error": last_error}


def _open_invite_url_with_session(account, invite_url, proxy=None, timeout=30):
    cookie = str(account.get("cookie_header") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    if cookie:
        headers["cookie"] = cookie
    response = curl_requests.get(
        invite_url,
        headers=headers,
        proxies=proxies,
        timeout=timeout,
        impersonate="chrome",
        allow_redirects=True,
    )
    return {
        "ok": 200 <= int(response.status_code) < 400,
        "status": response.status_code,
        "url": getattr(response, "url", invite_url),
        "body": (response.text or "")[:300],
    }


def _accept_invite_after_request(account, workspace_ids, proxy=None, timeout=30, invite_timeout=240, max_retries=0, retry_backoff=5, post_workspace_invite_func=None):
    print(f"    Waiting K12 invite mail: {account.get('email', '')} timeout={invite_timeout}s", flush=True)
    invite = _poll_k12_invite_url(account, timeout=invite_timeout, proxy=proxy)
    result = {"ok": False, "invite": invite}
    if not invite.get("ok"):
        result["error"] = invite.get("error") or "invite_not_found"
        return result
    invite_url = str(invite.get("url") or "")
    workspace_id = _workspace_id_from_invite_url(invite_url, workspace_ids)
    result["invite_url"] = invite_url
    result["workspace_id"] = workspace_id
    open_result = _open_invite_url_with_session(account, invite_url, proxy=proxy, timeout=timeout)
    result["open_result"] = open_result
    accept_result = {}
    if workspace_id:
        post_func = post_workspace_invite_func
        if post_func is None:
            raise RuntimeError("post_workspace_invite_func is required")
        accept_result = post_func(
            account,
            workspace_id,
            route="accept",
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        result["accept_result"] = accept_result
    result["ok"] = bool((accept_result or {}).get("ok")) if workspace_id else bool(open_result.get("ok"))
    if not result["ok"]:
        result["error"] = (accept_result or {}).get("error") or (accept_result or {}).get("body") or open_result.get("body") or "invite_accept_failed"
