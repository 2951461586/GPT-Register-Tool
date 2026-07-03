import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as curl_requests

from .config import CFG
from .paths import output_dir
from .session_refresh import _load_seed_session
from .storage import get_account_record, list_paypal_accounts, mark_k12_status


DEFAULT_WORKSPACE_ID = "631e1603-06cf-4f0b-b79b-d09fbfcfe98d"


def parse_workspace_ids(value="", file_path=""):
    chunks = []
    if value:
        chunks.append(str(value))
    if file_path:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"workspace file not found: {file_path}")
        chunks.append(path.read_text(encoding="utf-8-sig"))
    if not chunks:
        configured = ((CFG.get("k12") or {}).get("workspace_ids") or "").strip()
        chunks.append(configured or DEFAULT_WORKSPACE_ID)

    ids = []
    seen = set()
    for chunk in chunks:
        for raw in str(chunk or "").replace(",", "\n").splitlines():
            ws_id = raw.strip()
            if not ws_id or ws_id.startswith("#") or ws_id in seen:
                continue
            seen.add(ws_id)
            ids.append(ws_id)
    return ids


def normalize_k12_route(route):
    value = str(route or "").strip().lower()
    if value in {"accept", "join", "invite_accept"}:
        return "accept"
    return "request"


def _extract_access_token(data):
    if not isinstance(data, dict):
        return ""
    candidates = (
        data.get("access_token"),
        data.get("accessToken"),
        (data.get("auth_session") or {}).get("accessToken") if isinstance(data.get("auth_session"), dict) else "",
        (data.get("auth_session") or {}).get("access_token") if isinstance(data.get("auth_session"), dict) else "",
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


def _epoch_from_iso(value):
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def _iso_local_from_epoch(epoch_seconds, tz_hours=8):
    try:
        tz = timezone(timedelta(hours=tz_hours))
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).astimezone(tz).replace(microsecond=0).isoformat()
    except Exception:
        return ""


def _now_local_iso(tz_hours=8):
    tz = timezone(timedelta(hours=tz_hours))
    return datetime.now(tz=tz).replace(microsecond=0).isoformat()


def _build_synthetic_k12_id_token(email, workspace_id, expires_at=""):
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return ""
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": _epoch_from_iso(expires_at) or now + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."


def _token_account_id(token):
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    return str(auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id") or "").strip()


def build_k12_cpa_json(account, workspace_id, now_iso=""):
    """Build the compact CPA JSON used after a K12 workspace join.

    Reference shape:
    access_token/account_id/disabled/email/expired/id_token/last_refresh/refresh_token/type.
    The account_id is intentionally the K12 workspace id.  If the stored id_token
    belongs to another account, generate a synthetic CPA-compatible id_token so
    the exported account id stays consistent.
    """
    raw = account.get("raw") if isinstance(account.get("raw"), dict) else {}
    workspace_id = str(workspace_id or "").strip()
    email = str(raw.get("email") or account.get("email") or "").strip()
    access_token = str(account.get("access_token") or _extract_access_token(raw)).strip()
    refresh_token = _extract_refresh_token(raw)
    access_claims = _jwt_claims(access_token)
    expired = _iso_local_from_epoch(access_claims.get("exp")) if access_claims.get("exp") else ""
    if not expired:
        expired = str(raw.get("expired") or raw.get("expires") or raw.get("expires_at") or "").strip()
    id_token = _extract_id_token(raw)
    id_account_id = _token_account_id(id_token)
    if not id_token or id_account_id != workspace_id:
        id_token = _build_synthetic_k12_id_token(email=email, workspace_id=workspace_id, expires_at=expired)
    return {
        "access_token": access_token,
        "account_id": workspace_id,
        "disabled": bool(raw.get("disabled", False)),
        "email": email,
        "expired": expired,
        "id_token": id_token,
        "last_refresh": str(now_iso or _now_local_iso()),
        "refresh_token": refresh_token,
        "type": "codex",
    }


def _k12_cpa_export_path(email, export_dir=""):
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "codex_exports"
    safe_email = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", (email or "unknown").strip())
    return directory / f"codex-{safe_email}-k12.json"


def export_k12_cpa_json(account, workspace_id, export_dir=""):
    cpa_json = build_k12_cpa_json(account, workspace_id)
    if not cpa_json.get("access_token"):
        return {"ok": False, "error": "missing_access_token"}
    if not cpa_json.get("account_id"):
        return {"ok": False, "error": "missing_workspace_id"}
    path = _k12_cpa_export_path(cpa_json.get("email") or account.get("email") or "", export_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cpa_json, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "email": cpa_json.get("email", ""),
        "account_id": cpa_json.get("account_id", ""),
        "expired": cpa_json.get("expired", ""),
        "refresh_token_status": "oauth_present" if cpa_json.get("refresh_token") else "no_rt",
    }


def _extract_cookie_header(data):
    if not isinstance(data, dict):
        return ""
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    candidates = (
        data.get("cookie_header"),
        data.get("cookies"),
        auth_session.get("cookie_header"),
        auth_session.get("cookies"),
        session.get("cookie_header"),
        session.get("cookies"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _account_from_row(row):
    raw = {}
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    access_token = str(row.get("access_token") or "").strip() or _extract_access_token(raw)
    mailbox = raw.get("mailbox") if isinstance(raw.get("mailbox"), dict) else {}
    return {
        "email": str(row.get("email") or raw.get("email") or "").strip().lower(),
        "access_token": access_token,
        "device_id": str(row.get("device_id") or raw.get("device_id") or "").strip(),
        "cookie_header": str(row.get("cookie_header") or "").strip() or _extract_cookie_header(raw),
        "raw": raw,
        "json_path": str(row.get("json_path") or raw.get("json_path") or "").strip(),
        "mailbox": mailbox,
    }


def load_k12_accounts(emails=None, session_file=""):
    accounts = []
    seen = set()

    if session_file:
        data, _ = _load_seed_session(session_file=session_file)
        email = str(data.get("email") or "").strip().lower()
        token = _extract_access_token(data)
        if email and token:
            row = get_account_record(email)
            raw = {}
            try:
                raw = json.loads((row or {}).get("raw_json") or "{}")
            except Exception:
                raw = {}
            merged = {**raw, **data} if isinstance(raw, dict) else dict(data)
            mailbox = merged.get("mailbox") if isinstance(merged.get("mailbox"), dict) else {}
            accounts.append({
                "email": email,
                "access_token": token,
                "device_id": str(data.get("device_id") or raw.get("device_id") or "").strip(),
                "cookie_header": str(data.get("cookie_header") or _extract_cookie_header(raw)).strip(),
                "raw": merged,
                "json_path": str((row or {}).get("json_path") or data.get("json_path") or "").strip(),
                "mailbox": mailbox,
            })
            seen.add(email)

    for email in emails or []:
        email = str(email or "").strip().lower()
        if not email or email in seen:
            continue
        row = get_account_record(email)
        account = _account_from_row(row) if row else {}
        if account.get("email") and account.get("access_token"):
            accounts.append(account)
            seen.add(account["email"])

    if not emails and not session_file:
        for row in list_paypal_accounts():
            full_row = get_account_record(row.get("email", "")) or row
            account = _account_from_row(full_row)
            email = account.get("email", "")
            if not email or email in seen or not account.get("access_token"):
                continue
            accounts.append(account)
            seen.add(email)

    return accounts


def _refresh_access_token_from_cookie(account, proxy=None, timeout=30):
    cookie = str(account.get("cookie_header") or "").strip()
    if not cookie:
        return ""
    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    response = curl_requests.get(
        f"{chat_base}/api/auth/session",
        headers=headers,
        proxies=proxies,
        timeout=timeout,
        impersonate="chrome",
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"session refresh HTTP {response.status_code}: {response.text[:200]}")
    token = _extract_access_token(body)
    if not token:
        raise RuntimeError(
            "session refresh returned empty accessToken "
            f"(HTTP {response.status_code}, content-type={response.headers.get('content-type', '')}, "
            f"body={response.text[:160]!r})"
        )
    account["access_token"] = token
    return token


def _post_workspace_invite(account, workspace_id, route="request", proxy=None, timeout=30, max_retries=0, retry_backoff=5):
    route = normalize_k12_route(route)
    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    url = f"{chat_base}/backend-api/accounts/{workspace_id}/invites/{route}"
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    headers = {
        "accept": "*/*",
        "authorization": "Bearer " + str(account.get("access_token") or "").strip(),
        "content-type": "application/json",
        "oai-device-id": str(account.get("device_id") or "").strip() or str(uuid.uuid4()),
        "oai-language": "en-US",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    cookie = str(account.get("cookie_header") or "").strip()
    if cookie:
        headers["cookie"] = cookie

    started = time.time()
    attempts = max(0, int(max_retries or 0)) + 1
    last_result = {}
    refresh_errors = []
    for attempt in range(attempts):
        headers["authorization"] = "Bearer " + str(account.get("access_token") or "").strip()
        try:
            response = session.post(
                url,
                headers=headers,
                data="",
                timeout=timeout,
                impersonate="chrome",
            )
            text = response.text or ""
            ok = 200 <= int(response.status_code) < 300
            last_result = {
                "ok": ok,
                "email": account.get("email", ""),
                "workspace_id": workspace_id,
                "route": route,
                "status": response.status_code,
                "body": text[:500],
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
            if ok:
                return last_result
            if response.status_code in (401, 403) and attempt < attempts - 1:
                try:
                    _refresh_access_token_from_cookie(account, proxy=proxy, timeout=timeout)
                    last_result["token_refreshed"] = True
                except Exception as refresh_exc:
                    refresh_errors.append(str(refresh_exc))
                    last_result["refresh_error"] = str(refresh_exc)
        except Exception as exc:
            last_result = {
                "ok": False,
                "email": account.get("email", ""),
                "workspace_id": workspace_id,
                "route": route,
                "status": 0,
                "error": str(exc),
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
        if refresh_errors:
            last_result["refresh_errors"] = refresh_errors[-3:]
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_backoff or 0)))
    return last_result


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


def _accept_invite_after_request(account, workspace_ids, proxy=None, timeout=30, invite_timeout=240, max_retries=0, retry_backoff=5):
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
        accept_result = _post_workspace_invite(
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
    return result


def run_k12_batch(accounts, workspace_ids, route="request", workers=4, proxy=None, timeout=30, max_retries=0, retry_backoff=5, auto_accept=False, invite_timeout=240, export_dir=""):
    route = normalize_k12_route(route)
    accounts = [account for account in accounts or [] if account.get("email") and account.get("access_token")]
    workspace_ids = [str(ws or "").strip() for ws in workspace_ids or [] if str(ws or "").strip()]
    jobs = [(account, ws) for account in accounts for ws in workspace_ids]
    workers = max(1, min(int(workers or 1), 20, len(jobs) or 1))

    print(f"[*] K12 workspace {route}: {len(accounts)} account(s), {len(workspace_ids)} workspace(s), workers={workers}, retries={int(max_retries or 0)}, auto_accept={bool(auto_accept)}")
    if not jobs:
        return {"ok": False, "total": 0, "success": 0, "failed": 0, "results": []}

    ordered = [None] * len(jobs)
    def _run(index, account, workspace_id):
        result = _post_workspace_invite(
            account,
            workspace_id,
            route=route,
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        request_ok = bool(result.get("ok"))
        if request_ok:
            mark_k12_status(
                account.get("email", ""),
                workspace_id=workspace_id,
                status="k12_requested" if route == "request" else "k12_joined",
                result=result,
                access_token=account.get("access_token", ""),
            )
        elif "token_invalidated" in (str(result.get("body") or "") + " " + str(result.get("error") or "")).lower():
            mark_k12_status(
                account.get("email", ""),
                workspace_id=workspace_id,
                status="at_invalid",
                result=result,
                access_token=account.get("access_token", ""),
            )
        if request_ok and auto_accept and route == "request":
            result["auto_accept"] = _accept_invite_after_request(
                account,
                [workspace_id],
                proxy=proxy,
                timeout=timeout,
                invite_timeout=invite_timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
            result["ok"] = bool((result.get("auto_accept") or {}).get("ok"))
        final_joined = bool(result.get("ok")) and (auto_accept or route == "accept")
        if final_joined:
            cpa_export = export_k12_cpa_json(account, workspace_id, export_dir=export_dir)
            result["cpa_export"] = cpa_export
            if cpa_export.get("ok"):
                print(f"    K12 CPA JSON: {cpa_export.get('path')}")
            else:
                result["ok"] = False
                result["error"] = cpa_export.get("error", "k12_cpa_export_failed")
        if result.get("ok"):
            mark_k12_status(
                account.get("email", ""),
                workspace_id=workspace_id,
                status="k12_joined" if (auto_accept or route == "accept") else "k12_requested",
                result=result,
                access_token=account.get("access_token", ""),
            )
        status = result.get("status", 0)
        label = "OK" if result.get("ok") else "FAIL"
        print(f"[{index + 1}/{len(jobs)}] {label} {account.get('email')} -> {workspace_id[:8]} {route} HTTP {status}")
        return index, result

    if workers <= 1:
        for i, (account, workspace_id) in enumerate(jobs):
            _, result = _run(i, account, workspace_id)
            ordered[i] = result
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run, i, account, workspace_id) for i, (account, workspace_id) in enumerate(jobs)]
            for future in as_completed(futures):
                i, result = future.result()
                ordered[i] = result

    results = [result for result in ordered if result is not None]
    success = sum(1 for result in results if result.get("ok"))
    summary = {
        "ok": success == len(results),
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "route": route,
        "workspace_ids": workspace_ids,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
