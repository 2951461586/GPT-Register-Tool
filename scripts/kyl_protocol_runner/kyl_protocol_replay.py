#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import random
import re
import string
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any, Dict, Iterable, Optional, Tuple

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for dep_dir in (
    os.environ.get("KYL_PROTOCOL_PYDEPS"),
    str(SCRIPT_DIR / "pydeps"),
    str(pathlib.Path.home() / ".cache/cdp-openai-kyl/pydeps"),
):
    if dep_dir and pathlib.Path(dep_dir).exists():
        sys.path.insert(0, dep_dir)
from curl_cffi import requests  # type: ignore

HOME = pathlib.Path.home()
CPA_ROOT = pathlib.Path(os.environ.get("CPA_ROOT", str(HOME / "CHFProject/GithubProject/CLIProxyAPI.git")))
WORK_DIR = pathlib.Path(os.environ.get("WORK_DIR", str(SCRIPT_DIR / "runtime")))
STATE_PATH = pathlib.Path(os.environ.get("STATE_PATH", str(WORK_DIR / "openai-kyl-batch-state.json")))
TRACE_PATH = pathlib.Path(os.environ.get("TRACE_PATH", str(WORK_DIR / "protocol-trace-with-bodies.raw.jsonl")))
CDP_COOKIE_PATH = pathlib.Path(os.environ.get("CDP_COOKIE_PATH", str(WORK_DIR / "cookies.json")))
KYL_COOKIE_PATH = pathlib.Path(os.environ.get("KYL_COOKIE_PATH", str(WORK_DIR / "kyl-protocol-cookies.json")))
AUTH_DIR = pathlib.Path(os.environ.get("AUTH_DIR", str(CPA_ROOT / "auths")))
CPA_BASE = os.environ.get("CPA_BASE", "http://127.0.0.1:8317/v0/management").rstrip("/")
CPA_ENV = pathlib.Path(os.environ.get("CPA_ENV", str(CPA_ROOT / ".env")))
ACCOUNT_INDEX = int(os.environ.get("ACCOUNT_INDEX", os.environ.get("START_INDEX", "114")))
STATUS_PATH = pathlib.Path(os.environ.get("PROTOCOL_STATUS_PATH", str(WORK_DIR / "protocol-replay.status.jsonl")))
KYL_FINGERPRINT = os.environ.get("KYL_FINGERPRINT", "")
KYL_CHALLENGE_INIT_JSON = os.environ.get("KYL_CHALLENGE_INIT_JSON", "")
DIRECT_CODEX_SAVE = os.environ.get("DIRECT_CODEX_SAVE", "1") != "0"
HTTP_IMPERSONATE = os.environ.get("HTTP_IMPERSONATE", "chrome136")

DOMAIN_CONNECTIONS = {
    "sama.edu.kyl23333.xyz": "conn_01KTJ8SZN8NPCCW5K5F8HZS5B7",
    "samaagi.edu.kyl23333.xyz": "conn_01KTJ8F5Z68H3KMZ4SEY29SNGP",
}
DOMAIN_WORKSPACES = {
    "sama.edu.kyl23333.xyz": "ddf35c74-641f-4b22-80f3-3fb4a3e505c7",
    "samaagi.edu.kyl23333.xyz": "e3cc6931-fd0c-4087-becc-fdaeaf6c9cc7",
}
INVITE_CLIENT_ID = "nWhy1yKYKF2Wqg3oERfR"
CASDOOR_APP = "oai-oauth2-sama-edu"
CASDOOR_SAML_APP = "oai-oauth2-sama2edu"
CASDOOR_PROVIDER = "kyl_challenge"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"

SECRET_QUERY_KEYS = {
    "code",
    "state",
    "token",
    "payload",
    "session_id",
    "verifier_id",
    "login_challenge",
    "consent_challenge",
    "login_verifier",
    "consent_verifier",
    "code_challenge",
    "code_verifier",
}
SECRET_JSON_KEYS = {"access_token", "refresh_token", "id_token", "code", "state", "continue_url", "login_verifier", "consent_verifier", "openai-sentinel-token", "oai-client-auth-session"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(event: str, **fields: Any) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "event": event, **fields}
    with STATUS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)


def sanitize_url(raw: str) -> str:
    if not raw:
        return raw
    try:
        u = urllib.parse.urlsplit(raw)
        pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
        safe_q = []
        for k, v in pairs:
            safe_q.append((k, f"<{len(v)}>" if k in SECRET_QUERY_KEYS else v))
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(safe_q), ""))
    except Exception:
        return raw[:160]


def env_value(raw: str) -> str:
    v = raw.strip()
    if v.startswith("export "):
        v = v[len("export "):].strip()
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        v = v[1:-1]
    return v


def management_key() -> str:
    for name in ("CPA_MANAGEMENT_KEY", "MANAGEMENT_PASSWORD"):
        if os.environ.get(name):
            return os.environ[name]
    raw = CPA_ENV.read_text(encoding="utf-8")
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for name in ("CPA_MANAGEMENT_KEY", "MANAGEMENT_PASSWORD"):
            m = re.match(rf"^(?:export\s+)?{name}=(.*)$", s)
            if m:
                return env_value(m.group(1))
    raise RuntimeError(f"CPA_MANAGEMENT_KEY or MANAGEMENT_PASSWORD not found in {CPA_ENV}")


def cpa_headers() -> Dict[str, str]:
    return {"X-Management-Key": management_key()}


def cpa_get(path: str) -> Dict[str, Any]:
    r = requests.get(CPA_BASE + path, headers=cpa_headers(), timeout=15, impersonate=HTTP_IMPERSONATE)
    if r.status_code >= 400:
        raise RuntimeError(f"CPA GET {path} -> {r.status_code}: {r.text[:240]}")
    return r.json()


def cpa_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    headers = {**cpa_headers(), "Content-Type": "application/json"}
    r = requests.post(CPA_BASE + path, headers=headers, json=body, timeout=20, impersonate=HTTP_IMPERSONATE)
    if r.status_code >= 400:
        raise RuntimeError(f"CPA POST {path} -> {r.status_code}: {r.text[:240]}")
    try:
        return r.json()
    except Exception:
        return {"status": "ok"}


def load_account(index: int) -> Dict[str, Any]:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    accounts = state.get("accounts") or []
    if not (0 <= index < len(accounts)):
        raise RuntimeError(f"account index {index} not found; state has {len(accounts)} accounts")
    return accounts[index]


def existing_auth_emails() -> set[str]:
    out: set[str] = set()
    if not AUTH_DIR.exists():
        return out
    for p in AUTH_DIR.glob("codex-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("email"):
                out.add(str(data["email"]))
        except Exception:
            pass
    return out


def auth_snapshot() -> Dict[str, float]:
    if not AUTH_DIR.exists():
        return {}
    return {str(p): p.stat().st_mtime for p in AUTH_DIR.glob("codex-*.json")}


def changed_auth_files(before: Dict[str, float], email: str) -> list[str]:
    deadline = time.time() + 25
    while time.time() < deadline:
        after = auth_snapshot()
        email_matches = []
        for p in after:
            try:
                data = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
                if data.get("email") == email:
                    email_matches.append(p)
            except Exception:
                pass
        if email_matches:
            return sorted(set(email_matches))
        time.sleep(0.5)
    return []


def b64url_json(segment: str) -> Dict[str, Any]:
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def jwt_claims(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise RuntimeError("JWT token has fewer than two segments")
    return b64url_json(parts[1])


def normalize_plan_type(plan_type: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", (plan_type or "").strip())
    return "-".join(p.lower() for p in parts)


def codex_credential_file_name(email: str, plan_type: str, account_id: str) -> str:
    plan = normalize_plan_type(plan_type)
    if not plan:
        return f"codex-{email}.json"
    if plan == "team":
        digest = hashlib.sha256(account_id.encode()).hexdigest()[:8] if account_id else ""
        return f"codex-{digest}-{email}-{plan}.json"
    return f"codex-{email}-{plan}.json"


def local_time_rfc3339(dt: Optional[datetime] = None) -> str:
    tz = timezone(timedelta(hours=8))
    value = dt or datetime.now(tz)
    return value.astimezone(tz).isoformat(timespec="seconds")


def exchange_codex_tokens(session: requests.Session, code: str, verifier: str) -> Dict[str, Any]:
    body = {
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": CODEX_REDIRECT_URI,
        "code_verifier": verifier,
    }
    last_error = ""
    for attempt in range(1, 4):
        try:
            r = session_post(session, 
                CODEX_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=body,
                timeout=35,
            )
            if r.status_code == 200:
                data = r.json()
                for key in ("access_token", "refresh_token", "id_token"):
                    if not data.get(key):
                        raise RuntimeError(f"token response missing {key}")
                return data
            last_error = f"HTTP {r.status_code}: {r.text[:240]}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < 3:
            time.sleep(1.0 * attempt)
    raise RuntimeError(f"Codex token exchange failed: {last_error}")


def save_codex_auth(token_resp: Dict[str, Any], expected_email: str) -> str:
    claims = jwt_claims(str(token_resp["id_token"]))
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    email = str(claims.get("email") or token_resp.get("email") or "").strip()
    if not email:
        raise RuntimeError("Codex ID token does not contain email")
    if expected_email and email != expected_email:
        raise RuntimeError(f"Codex ID token email mismatch: expected={expected_email} got={email}")

    account_id = str(auth_claims.get("chatgpt_account_id") or claims.get("account_id") or "").strip()
    plan_type = str(auth_claims.get("chatgpt_plan_type") or "").strip()
    expires_in = int(token_resp.get("expires_in") or 0)
    expire_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=expires_in)
    record = {
        "access_token": token_resp["access_token"],
        "account_id": account_id,
        "disabled": False,
        "email": email,
        "expired": local_time_rfc3339(expire_time),
        "id_token": token_resp["id_token"],
        "last_refresh": local_time_rfc3339(),
        "refresh_token": token_resp["refresh_token"],
        "type": "codex",
    }
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    file_name = codex_credential_file_name(email, plan_type, account_id)
    file_path = AUTH_DIR / file_name
    tmp_path = AUTH_DIR / f".{file_name}.tmp"
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, file_path)
    return str(file_path)


def iter_trace() -> Iterable[Dict[str, Any]]:
    if not TRACE_PATH.exists():
        return
    for line in TRACE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def latest_request_record(url_part: str, event: str = "request") -> Optional[Dict[str, Any]]:
    hit = None
    for r in iter_trace():
        if r.get("event") == event and url_part in (r.get("url") or ""):
            hit = r
    return hit


def request_extra_by_id(request_id: str) -> Optional[Dict[str, Any]]:
    hit = None
    for r in iter_trace():
        if r.get("event") == "requestExtra" and r.get("requestId") == request_id:
            hit = r
    return hit


def latest_request_and_extra(url_part: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    req = latest_request_record(url_part, "request")
    return req, request_extra_by_id(req["requestId"]) if req and req.get("requestId") else None


def cookie_pairs(cookie_header: str) -> Iterable[Tuple[str, str]]:
    # Cookie headers here are simple name=value pairs separated by semicolons.
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name:
            yield name.strip(), value.strip()


def seed_domain_cookies(session: requests.Session, domain: str, cookie_header: str) -> int:
    count = 0
    for name, value in cookie_pairs(cookie_header):
        try:
            session.cookies.set(name, value, domain=domain, path="/")
            count += 1
        except Exception:
            pass
    return count


def base_headers_from_trace(url_part: str) -> Dict[str, str]:
    req, extra = latest_request_and_extra(url_part)
    h = dict((req or {}).get("headers") or {})
    hx = dict((extra or {}).get("headers") or {})
    out: Dict[str, str] = {}
    for k in [
        "User-Agent", "user-agent", "accept", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile",
        "sec-ch-ua-platform", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
        "sec-ch-ua-full-version-list", "sec-ch-ua-model", "sec-ch-ua-platform-version",
        "openai-sentinel-token",
    ]:
        val = h.get(k) or hx.get(k)
        if val:
            canonical = "User-Agent" if k.lower() == "user-agent" else k
            out[canonical] = val
    out.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
    out.setdefault("accept", "*/*")
    out.setdefault("accept-language", "zh-CN,zh;q=0.9,en;q=0.8")
    out.setdefault("sec-ch-ua", '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"')
    out.setdefault("sec-ch-ua-mobile", "?0")
    out.setdefault("sec-ch-ua-platform", '"Windows"')
    out.setdefault("sec-ch-ua-arch", '"x86"')
    out.setdefault("sec-ch-ua-bitness", '"64"')
    out.setdefault("sec-ch-ua-full-version", '"148.0.7778.217"')
    out.setdefault("sec-ch-ua-full-version-list", '"Chromium";v="148.0.7778.217", "Google Chrome";v="148.0.7778.217", "Not/A)Brand";v="99.0.0.0"')
    out.setdefault("sec-ch-ua-model", '""')
    out.setdefault("sec-ch-ua-platform-version", '"19.0.0"')
    return out


def seed_cdp_cookie_file(session: requests.Session) -> int:
    if not CDP_COOKIE_PATH.exists():
        return 0
    try:
        data = json.loads(CDP_COOKIE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    count = 0
    for c in data.get("cookies") or []:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        path = c.get("path") or "/"
        if not name or value is None or not domain:
            continue
        try:
            session.cookies.set(str(name), str(value), domain=str(domain), path=str(path))
            count += 1
        except Exception:
            pass
    return count


def sync_openai_auth_cookies(session: requests.Session) -> None:
    names = {
        "auth_provider",
        "login_session",
        "oai-client-auth-session",
        "auth-session-minimized",
        "auth-session-minimized-client-checksum",
        "oai-client-auth-info",
        "unified_session_manifest",
    }
    for c in list(session.cookies.jar):
        if c.domain != "auth.openai.com" or c.name not in names:
            continue
        try:
            session.cookies.set(c.name, c.value, domain=".auth.openai.com", path=c.path or "/")
        except Exception:
            pass


def clear_openai_auth_session_cookies(session: requests.Session) -> None:
    exact_names = {
        "auth_provider",
        "login_session",
        "hydra_redirect",
        "oai-client-auth-session",
        "auth-session-minimized",
        "auth-session-minimized-client-checksum",
        "oai-client-auth-info",
        "unified_session_manifest",
        "workos_oidc_session",
        "workos_oidc_session_legacy",
        "interstitial_csrf_token",
        "rg_context",
        "iss_context",
    }
    prefixes = (
        "oai-login-csrf",
        "oai-consent-csrf",
        "oai-session",
        "wos_client_",
        "ory_hydra",
        "oauth2_",
    )
    for c in list(session.cookies.jar):
        if not any(host in c.domain for host in ("openai.com", "auth.openai.com", "external.auth.openai.com")):
            continue
        if c.name not in exact_names and not c.name.startswith(prefixes):
            continue
        try:
            session.cookies.delete(c.name, domain=c.domain, path=c.path)
        except Exception:
            pass


def clear_casdoor_session_cookies(session: requests.Session) -> int:
    removed = 0
    exact_names = {
        "casdoor_session_id",
        "g_state",
    }
    for c in list(session.cookies.jar):
        if c.domain not in {"oauth.kyl23333.xyz", "oauth.luminet.cn"}:
            continue
        if c.name not in exact_names:
            continue
        try:
            session.cookies.delete(c.name, domain=c.domain, path=c.path)
            removed += 1
        except Exception:
            pass
    return removed


def clear_domain_cookies(session: requests.Session, domain_part: str) -> int:
    removed = 0
    for c in list(session.cookies.jar):
        if domain_part not in c.domain:
            continue
        try:
            session.cookies.delete(c.name, domain=c.domain, path=c.path)
            removed += 1
        except Exception:
            pass
    return removed


def make_session() -> requests.Session:
    s = requests.Session(impersonate=HTTP_IMPERSONATE)
    seeded = {}
    for domain, marker in [
        ("auth.openai.com", "auth.openai.com/api/accounts/authorize/continue"),
        (".auth.openai.com", "auth.openai.com/api/accounts/authorize/continue"),
        (".auth.openai.com", "external.auth.openai.com/sso/oidc"),
        ("oauth.kyl23333.xyz", "oauth.kyl23333.xyz/api/login"),
        ("oauth.luminet.cn", "oauth.luminet.cn/api/login"),
        ("invite.kyl23333.xyz", "invite.kyl23333.xyz/api/v1/oauth/consent"),
    ]:
        _, extra = latest_request_and_extra(marker)
        cookie_header = ((extra or {}).get("headers") or {}).get("cookie") or ""
        if cookie_header:
            seeded[f"{domain} <- {marker}"] = seed_domain_cookies(s, domain, cookie_header)
    cdp_count = seed_cdp_cookie_file(s)
    kyl_count = 0
    if os.environ.get("SEED_STALE_KYL_COOKIES") == "1" and KYL_COOKIE_PATH.exists():
        try:
            data = json.loads(KYL_COOKIE_PATH.read_text(encoding="utf-8"))
            for c in data.get("cookies") or []:
                if c.get("name") and c.get("value") is not None and c.get("domain"):
                    s.cookies.set(str(c["name"]), str(c["value"]), domain=str(c["domain"]), path=str(c.get("path") or "/")); kyl_count += 1
        except Exception:
            pass
    log("protocolCookiesSeeded", domains=seeded, cdpCookieCount=cdp_count, kylCookieCount=kyl_count)
    return s


def session_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        active_session = session if attempt == 1 else clone_http_session(session)
        try:
            resp = getattr(active_session, method)(url, **kwargs)
            if active_session is not session:
                merge_session_cookies(session, active_session)
            return resp
        except Exception as exc:
            last_error = exc
            if active_session is not session:
                try:
                    active_session.close()
                except Exception:
                    pass
            if attempt >= 3:
                break
            log("protocolHttpRetry", method=method.upper(), attempt=attempt, url=sanitize_url(url), error=str(exc)[:180])
            time.sleep(0.8 * attempt)
    raise last_error or RuntimeError(f"{method.upper()} {sanitize_url(url)} failed")


def session_get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    return session_request(session, "get", url, **kwargs)


def session_post(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    return session_request(session, "post", url, **kwargs)


def openai_headers(json_body: bool = False, referer: str = "https://auth.openai.com/log-in") -> Dict[str, str]:
    h = base_headers_from_trace("auth.openai.com/api/accounts/authorize/continue")
    h.update({
        "Accept": "application/json",
        "Referer": referer,
        "Origin": "https://auth.openai.com",
    })
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def generic_headers(origin: Optional[str] = None, referer: Optional[str] = None, json_body: bool = False) -> Dict[str, str]:
    h = base_headers_from_trace("auth.openai.com/api/accounts/authorize/continue")
    # This token is large and only valid for auth.openai.com account API calls.
    h.pop("openai-sentinel-token", None)
    h.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    h.setdefault("Upgrade-Insecure-Requests", "1")
    h.setdefault("Sec-Fetch-Dest", "document")
    h.setdefault("Sec-Fetch-Mode", "navigate")
    h.setdefault("Sec-Fetch-Site", "same-origin")
    h.setdefault("Sec-Fetch-User", "?1")
    if origin:
        h["Origin"] = origin
        origin_host = urllib.parse.urlsplit(origin).netloc
        referer_host = urllib.parse.urlsplit(referer or "").netloc
        if referer_host and referer_host == origin_host:
            h["Sec-Fetch-Site"] = "same-origin"
        else:
            h["Sec-Fetch-Site"] = "same-site" if origin_host.endswith("openai.com") else "cross-site"
    if referer:
        h["Referer"] = referer
    if json_body:
        h["Content-Type"] = "application/json"
        h["Accept"] = "*/*"
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h.pop("Sec-Fetch-User", None)
    return h


def code_verifier() -> str:
    raw = os.urandom(32)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def oauth_state() -> str:
    return os.urandom(16).hex()


def codex_auth_start_url(state: str, verifier: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": s256(verifier),
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    })
    return f"{CODEX_AUTH_URL}?{query}"


def b64_state(query: str) -> str:
    return base64.b64encode(query.encode()).decode()


def decode_b64_state(state: str) -> str:
    return base64.b64decode(state + "=" * ((4 - len(state) % 4) % 4)).decode()


def remove_prompt(raw_url: str) -> str:
    u = urllib.parse.urlsplit(raw_url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "prompt"]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


def json_or_error(resp: requests.Response, label: str) -> Dict[str, Any]:
    if resp.status_code >= 400:
        raise RuntimeError(f"{label} -> HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"{label} non-json HTTP {resp.status_code}: {resp.text[:300]}") from e


def response_continue_url(resp: requests.Response) -> Optional[str]:
    try:
        data = resp.json()
    except Exception:
        return None
    return data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url")


def absolute_url(base: str, loc: str) -> str:
    return urllib.parse.urljoin(base, loc)


def cpa_codex_callback_url(localhost_callback: str) -> Optional[str]:
    p = urllib.parse.urlsplit(localhost_callback)
    if p.scheme == "http" and p.netloc == "localhost:1455" and p.path == "/auth/callback":
        return urllib.parse.urlunsplit(("http", "127.0.0.1:8317", "/codex/callback", p.query, ""))
    return None


def manual_get_until(session: requests.Session, url: str, stop_hosts: set[str], max_hops: int = 12) -> requests.Response:
    cur = url
    last: Optional[requests.Response] = None
    for hop in range(max_hops):
        r = session_get(session, cur, headers=generic_headers(referer=cur), allow_redirects=False, timeout=25)
        last = r
        parsed = urllib.parse.urlsplit(cur)
        log("protocolGet", hop=hop, status=r.status_code, url=sanitize_url(cur))
        if parsed.netloc in stop_hosts and r.status_code < 300:
            return r
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location") or r.headers.get("location")
            if not loc:
                return r
            nxt = absolute_url(cur, loc)
            if urllib.parse.urlsplit(nxt).netloc in stop_hosts:
                return r
            cur = nxt
            continue
        return r
    if last is None:
        raise RuntimeError("no response")
    return last


def redirect_location(resp: requests.Response, base: str) -> Optional[str]:
    loc = resp.headers.get("Location") or resp.headers.get("location")
    return absolute_url(base, loc) if loc else None


def response_set_cookie_names(resp: requests.Response) -> list[str]:
    names: list[str] = []
    for key, value in resp.headers.items():
        if key.lower() != "set-cookie":
            continue
        for part in str(value).split(", "):
            name = part.split("=", 1)[0].strip()
            if name and re.match(r"^[A-Za-z0-9_.:-]+$", name):
                names.append(name)
    return names


def session_cookie_names(session: requests.Session, domain_part: str) -> list[str]:
    names = []
    for c in session.cookies.jar:
        if domain_part in c.domain:
            names.append(c.name)
    return sorted(set(names))


def clone_http_session(src: requests.Session) -> requests.Session:
    dst = requests.Session(impersonate=HTTP_IMPERSONATE)
    for c in list(src.cookies.jar):
        try:
            dst.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except Exception:
            pass
    return dst


def merge_session_cookies(dst: requests.Session, src: requests.Session) -> None:
    for c in list(src.cookies.jar):
        try:
            dst.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except Exception:
            pass


def extract_interstitial_token(html: str) -> Optional[str]:
    pats = [
        r'name=["\']interstitial_token["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']interstitial_token["\']',
        r'interstitial_token["\']?\s*[:=]\s*["\']([^"\']+)',
    ]
    for pat in pats:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def extract_hidden_form_fields(html: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.I):
        name_m = re.search(r"name=[\"']([^\"']+)", tag, flags=re.I)
        value_m = re.search(r"value=[\"']([^\"']*)", tag, flags=re.I)
        if name_m and name_m.group(1) not in fields:
            fields[name_m.group(1)] = value_m.group(1) if value_m else ""
    return fields


def extract_form_action(html: str) -> Optional[str]:
    m = re.search(r"<form\b[^>]*\baction=[\"']([^\"']+)", html, flags=re.I)
    return m.group(1) if m else None


def extract_callback_from_redirects(session: requests.Session, start_url: str, referer: str) -> str:
    cur = start_url
    for hop in range(16):
        r = session_get(session, cur, headers=generic_headers(referer=referer), allow_redirects=False, timeout=25)
        if os.environ.get("DEBUG_PROTOCOL_COOKIES") == "1":
            log(
                "protocolCookieDebug",
                hop=hop,
                setCookieNames=response_set_cookie_names(r),
                openaiCookieNames=session_cookie_names(session, "openai.com"),
            )
        log("protocolFinalAuthGet", hop=hop, status=r.status_code, url=sanitize_url(cur))
        loc = redirect_location(r, cur) if r.status_code in (301, 302, 303, 307, 308) else None
        if loc:
            p = urllib.parse.urlsplit(loc)
            if p.scheme == "http" and p.netloc == "localhost:1455" and p.path == "/auth/callback":
                return cpa_codex_callback_url(loc) or loc
            if p.scheme == "http" and p.netloc == "127.0.0.1:8317" and p.path == "/codex/callback":
                return loc
            cur = loc
            continue
        cont = response_continue_url(r)
        if cont:
            cur = cont
            continue
        # Some OpenAI endpoints return an HTML/JS transition page. Try common URL occurrences.
        m = re.search(r'https?://(?:localhost:1455/auth/callback|127\.0\.0\.1:8317/codex/callback)\?[^"\'<>\\]+', r.text)
        if m:
            return m.group(0).replace("&amp;", "&")
        if r.status_code >= 400:
            raise RuntimeError(f"final auth step HTTP {r.status_code}: {r.text[:360]}")
        raise RuntimeError(f"final auth step stopped without callback: status={r.status_code} url={sanitize_url(cur)} text={r.text[:240]!r}")
    raise RuntimeError("final auth redirect chain exceeded hop limit")


def casdoor_oauth_params(inner: urllib.parse.ParseResult, inner_query: str, code_challenge: str) -> Dict[str, str]:
    q = urllib.parse.parse_qs(inner_query, keep_blank_values=True)
    one = lambda k: (q.get(k) or [""])[0]
    return {
        "_flow": "oidc",
        "_application": one("application") or CASDOOR_APP,
        "clientId": one("client_id"),
        "responseType": one("response_type") or "code",
        "redirectUri": one("redirect_uri"),
        "type": "code",
        "scope": one("scope"),
        "state": one("state"),
        "nonce": one("nonce"),
        # Casdoor's browser client sends empty PKCE fields here even though the
        # upstream invite authorize step uses S256. The verifier is submitted in
        # the JSON body instead.
        "code_challenge_method": "",
        "code_challenge": "",
    }


def casdoor_saml_params(inner_query: str) -> Dict[str, str]:
    q = urllib.parse.parse_qs(inner_query, keep_blank_values=True)
    one = lambda k: (q.get(k) or [""])[0]
    application = one("application") or CASDOOR_SAML_APP
    return {
        "_flow": "saml",
        "_application": application,
        "_samlRequest": one("SAMLRequest"),
        "_relayState": one("RelayState"),
        "clientId": "",
        "responseType": "",
        "redirectUri": "",
        "type": "code",
        "scope": "",
        "state": "",
        "nonce": "",
        "code_challenge_method": "",
        "code_challenge": "",
    }


def casdoor_oauth_params_from_authorize_url(authorize_url: str) -> Dict[str, str]:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query, keep_blank_values=True)
    one = lambda k: (q.get(k) or [""])[0]
    return {
        "_flow": "oidc",
        "_application": CASDOOR_APP,
        "clientId": one("client_id"),
        "responseType": one("response_type") or "code",
        "redirectUri": one("redirect_uri"),
        "type": "code",
        "scope": one("scope"),
        "state": one("state"),
        "nonce": one("nonce"),
        "code_challenge_method": one("code_challenge_method"),
        "code_challenge": one("code_challenge"),
    }


def casdoor_api_login_query(params: Dict[str, str]) -> str:
    keys = [
        "clientId",
        "responseType",
        "redirectUri",
        "type",
        "scope",
        "state",
        "nonce",
        "code_challenge_method",
        "code_challenge",
    ]
    return urllib.parse.urlencode({k: params.get(k, "") for k in keys})


def kyl_consent_callback(session: requests.Session, oauth_origin: str, oauth_url: str, email: str, sub: str) -> Tuple[str, str, Dict[str, str], str]:
    verifier = code_verifier()
    challenge = s256(verifier)
    parsed_oauth = urllib.parse.urlsplit(oauth_url)
    casdoor_app = CASDOOR_SAML_APP if parsed_oauth.netloc == "oauth.luminet.cn" else CASDOOR_APP
    state_raw = "?" + parsed_oauth.query + f"&application={urllib.parse.quote(casdoor_app)}&provider={urllib.parse.quote(CASDOOR_PROVIDER)}&method=signin"
    invite_state = b64_state(state_raw)
    invite_q = urllib.parse.urlencode({
        "client_id": INVITE_CLIENT_ID,
        "redirect_uri": f"{oauth_origin}/callback",
        "scope": "openid profile email",
        "response_type": "code",
        "state": invite_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    invite_authorize = f"https://invite.kyl23333.xyz/oauth/authorize?{invite_q}"

    restore_headers = generic_headers(origin="https://invite.kyl23333.xyz", referer="https://invite.kyl23333.xyz/", json_body=True)
    if KYL_CHALLENGE_INIT_JSON:
        try:
            init_body = json.loads(KYL_CHALLENGE_INIT_JSON)
        except Exception:
            init_body = {}
        if isinstance(init_body, dict) and init_body.get("turnstileToken"):
            init_body.setdefault("fingerprint", KYL_FINGERPRINT)
            r_init = session_post(session,
                "https://invite.kyl23333.xyz/api/v1/challenge/init",
                headers=restore_headers,
                json=init_body,
                timeout=30,
            )
            if r_init.status_code == 200:
                try:
                    init_data = r_init.json()
                except Exception:
                    init_data = {}
                log(
                    "protocolKylChallengeInit",
                    status=r_init.status_code,
                    completed=bool(init_data.get("completed")),
                    accountCount=init_data.get("accountCount") or len(init_data.get("accounts") or []),
                )
            else:
                log("protocolKylChallengeInit", status=r_init.status_code, error=(r_init.text or "")[:160])

    r = session_post(session, 
        "https://invite.kyl23333.xyz/api/v1/challenge/restore",
        headers=restore_headers,
        json={"fingerprint": KYL_FINGERPRINT},
        timeout=25,
    )
    accounts = []
    if r.status_code == 401 and "browser_required" in (r.text or ""):
        # When accounts are supplied by embedded/restored state, challenge/restore is
        # only an optional freshness check. Continue to oauth/consent to find the
        # real authorization blocker instead of failing before the decisive request.
        log("protocolKylRestoreSkipped", status=r.status_code, reason="browser_required", fingerprint=KYL_FINGERPRINT[:18] + "...")
    else:
        restored = json_or_error(r, "KYL challenge restore")
        accounts = restored.get("accounts") or []
        if not any(a.get("sub") == sub or a.get("email") == email for a in accounts):
            raise RuntimeError(f"KYL restore does not include target account; restored={len(accounts)} email={email}")
        log("protocolKylRestore", status=r.status_code, accountCount=len(accounts), fingerprint=KYL_FINGERPRINT[:18] + "...")

    consent_body = {"authorizeUrl": invite_authorize, "accountSub": sub}
    r = session_post(session, 
        "https://invite.kyl23333.xyz/api/v1/oauth/consent",
        headers=restore_headers,
        json=consent_body,
        timeout=30,
    )
    consent = json_or_error(r, "KYL oauth consent")
    callback_url = consent.get("redirectUrl") or consent.get("redirect_url") or consent.get("url")
    if not callback_url:
        raise RuntimeError(f"KYL consent did not return redirectUrl: keys={list(consent.keys())}")
    log("protocolKylConsent", status=r.status_code, callback=sanitize_url(callback_url))

    cb = urllib.parse.urlsplit(callback_url)
    cb_q = dict(urllib.parse.parse_qsl(cb.query, keep_blank_values=True))
    inner_query = decode_b64_state(cb_q["state"])
    inner_params = dict(urllib.parse.parse_qsl(inner_query.lstrip("?"), keep_blank_values=True))
    if parsed_oauth.netloc == "oauth.luminet.cn" or "SAMLRequest" in inner_params:
        oauth_params = casdoor_saml_params(inner_query.lstrip("?"))
    else:
        oauth_params = casdoor_oauth_params(urllib.parse.urlparse(oauth_url), inner_query.lstrip("?"), challenge)
    return callback_url, cb_q["code"], oauth_params, verifier


def openai_continue_to_workos(session: requests.Session, email: str, connection: str, referer: str, phase: str) -> str:
    body = {"username": {"kind": "email", "value": email}}
    r = session_post(session,
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=openai_headers(json_body=True, referer=referer),
        json=body,
        timeout=30,
    )
    data = json_or_error(r, f"OpenAI username continue {phase}")
    sync_openai_auth_cookies(session)
    cont = data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url")
    log(
        "protocolOpenAIUsername",
        phase=phase,
        status=r.status_code,
        page=(data.get("page") or {}).get("type"),
        continueHost=urllib.parse.urlsplit(cont or "").netloc,
    )
    if not cont or (data.get("page") or {}).get("type") == "sso":
        if cont:
            r_sso = session_get(session, cont, headers=generic_headers(referer=referer), allow_redirects=True, timeout=30)
            sync_openai_auth_cookies(session)
            log("protocolOpenAISsoPage", phase=phase, status=r_sso.status_code, finalUrl=sanitize_url(r_sso.url))
        r = session_post(session,
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=openai_headers(json_body=True, referer="https://auth.openai.com/sso"),
            json={"connection": connection, "connection_provider": 2},
            timeout=30,
        )
        data = json_or_error(r, f"OpenAI connection continue {phase}")
        sync_openai_auth_cookies(session)
        cont = data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url")
        log(
            "protocolOpenAIConnection",
            phase=phase,
            status=r.status_code,
            page=(data.get("page") or {}).get("type"),
            continueHost=urllib.parse.urlsplit(cont or "").netloc,
        )
    if not cont:
        raise RuntimeError(f"OpenAI continue missing URL in {phase}: keys={list(data.keys())}")
    return cont


def casdoor_params_from_authorize_url(authorize_url: str) -> Dict[str, str]:
    parsed = urllib.parse.urlsplit(authorize_url)
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if parsed.netloc == "oauth.luminet.cn" or q.get("SAMLRequest"):
        return casdoor_saml_params(parsed.query)
    return casdoor_oauth_params_from_authorize_url(authorize_url)


def finish_workos_from_cont(
    session: requests.Session,
    cont: str,
    email: str,
    sub: str,
    use_kyl_bridge: bool,
    phase: str,
) -> requests.Response:
    if os.environ.get("DROP_PARENT_OPENAI_COOKIES") == "1":
        try:
            session.cookies.clear(domain=".openai.com")
        except Exception:
            pass
        try:
            session.cookies.clear(domain="openai.com")
        except Exception:
            pass

    r = session_get(session, cont, headers=generic_headers(referer="https://auth.openai.com/"), allow_redirects=False, timeout=30)
    log("protocolWorkOSAuthorize", phase=phase, status=r.status_code, url=sanitize_url(cont))
    oauth_url = redirect_location(r, cont)
    if not oauth_url:
        r2 = manual_get_until(session, cont, {"oauth.kyl23333.xyz", "oauth.luminet.cn"})
        oauth_url = r2.url if urllib.parse.urlsplit(r2.url).netloc in {"oauth.kyl23333.xyz", "oauth.luminet.cn"} else redirect_location(r2, cont)
    if not oauth_url or urllib.parse.urlsplit(oauth_url).netloc not in {"oauth.kyl23333.xyz", "oauth.luminet.cn"}:
        raise RuntimeError(f"WorkOS did not yield Casdoor URL in {phase}: status={r.status_code} loc={sanitize_url(oauth_url or '')} text={r.text[:240]!r}")
    log("protocolCasdoorAuthorize", phase=phase, url=sanitize_url(oauth_url))

    parsed_oauth = urllib.parse.urlsplit(oauth_url)
    oauth_origin = f"{parsed_oauth.scheme}://{parsed_oauth.netloc}"
    login_referer = oauth_url

    if use_kyl_bridge:
        callback_url, kyl_code, oauth_params, verifier = kyl_consent_callback(session, oauth_origin, oauth_url, email, sub)
        api_login_q = casdoor_api_login_query(oauth_params)
        application = oauth_params.get("_application") or CASDOOR_APP
        if oauth_params.get("_flow") == "saml":
            login_body = {
                "type": "saml",
                "application": application,
                "provider": CASDOOR_PROVIDER,
                "code": kyl_code,
                "samlRequest": oauth_params.get("_samlRequest") or "",
                "state": application,
                "invitationCode": "",
                "redirectUri": f"{oauth_origin}/callback",
                "method": "signin",
                "codeVerifier": verifier,
            }
        else:
            login_body = {
                "type": oauth_params["responseType"],
                "application": application,
                "provider": CASDOOR_PROVIDER,
                "code": kyl_code,
                "samlRequest": None,
                "state": application,
                "invitationCode": "",
                "redirectUri": f"{oauth_origin}/callback",
                "method": "signin",
                "codeVerifier": verifier,
            }
        login_referer = callback_url
    else:
        oauth_params = casdoor_params_from_authorize_url(oauth_url)
        api_login_q = casdoor_api_login_query(oauth_params)
        application = oauth_params.get("_application") or CASDOOR_APP
        if oauth_params.get("_flow") == "saml":
            original_saml_q = urllib.parse.parse_qs(parsed_oauth.query, keep_blank_values=True)
            one_saml = lambda k: (original_saml_q.get(k) or [""])[0]
            login_body = {
                "application": application,
                "language": "",
                "organization": "xuexi-2",
                "signinMethod": "Password",
                "type": "saml",
                "samlRequest": one_saml("SAMLRequest"),
                "relayState": one_saml("RelayState"),
            }
        else:
            login_body = {
                "application": application,
                "language": "",
                "organization": "xuexi-2",
                "signinMethod": "Password",
                "type": oauth_params["responseType"],
            }
        session_get(session,
            f"{oauth_origin}/api/get-app-login?{api_login_q}",
            headers=generic_headers(origin=oauth_origin, referer=oauth_url, json_body=True),
            timeout=30,
        )

    r = session_post(session,
        f"{oauth_origin}/api/login?{api_login_q}",
        headers=generic_headers(origin=oauth_origin, referer=login_referer, json_body=True),
        json=login_body,
        timeout=30,
    )
    cas = json_or_error(r, f"Casdoor api/login {phase}")
    if cas.get("status") != "ok" or not cas.get("data"):
        raise RuntimeError(f"Casdoor login failed in {phase}: {cas}")

    flow = oauth_params.get("_flow") or "oidc"
    initial_workos_response: Optional[requests.Response] = None
    if flow == "saml" and use_kyl_bridge:
        original_saml_q = urllib.parse.parse_qs(parsed_oauth.query, keep_blank_values=True)
        one_saml = lambda k: (original_saml_q.get(k) or [""])[0]
        saml_login_body = {
            "application": oauth_params.get("_application") or CASDOOR_SAML_APP,
            "language": "",
            "organization": "xuexi-2",
            "signinMethod": "Password",
            "type": "saml",
            "samlRequest": one_saml("SAMLRequest"),
            "relayState": one_saml("RelayState"),
        }
        r = session_post(session,
            f"{oauth_origin}/api/login?{api_login_q}",
            headers=generic_headers(origin=oauth_origin, referer=oauth_url, json_body=True),
            json=saml_login_body,
            timeout=30,
        )
        cas = json_or_error(r, f"Casdoor saml api/login {phase}")
        if cas.get("status") != "ok" or not cas.get("data"):
            raise RuntimeError(f"Casdoor SAML login failed in {phase}: keys={list(cas.keys())} msg={cas.get('msg')}")
        raw_saml_data = cas.get("data")
        log(
            "protocolCasdoorSamlLogin",
            phase=phase,
            status=r.status_code,
            dataType=type(raw_saml_data).__name__,
            dataLen=len(raw_saml_data) if isinstance(raw_saml_data, str) else None,
            dataPrefix=raw_saml_data[:40] if isinstance(raw_saml_data, str) else None,
            data2Type=type(cas.get("data2")).__name__,
            data2Len=len(cas.get("data2")) if isinstance(cas.get("data2"), str) else None,
            data2Prefix=cas.get("data2")[:40] if isinstance(cas.get("data2"), str) else None,
            data2Keys=list(cas.get("data2").keys()) if isinstance(cas.get("data2"), dict) else [],
            data3Type=type(cas.get("data3")).__name__,
            data3Len=len(cas.get("data3")) if isinstance(cas.get("data3"), str) else None,
            data3Prefix=cas.get("data3")[:80] if isinstance(cas.get("data3"), str) else None,
            dataKeys=list(raw_saml_data.keys()) if isinstance(raw_saml_data, dict) else [],
        )

    if flow == "saml":
        saml_data = cas.get("data")
        saml_html = saml_data if isinstance(saml_data, str) else json.dumps(saml_data, ensure_ascii=False)
        action = extract_form_action(saml_html)
        fields = extract_hidden_form_fields(saml_html)
        if not action and isinstance(saml_data, dict):
            action = saml_data.get("url") or saml_data.get("action") or saml_data.get("redirectUrl")
            raw_fields = saml_data.get("fields") or saml_data.get("form") or {}
            if isinstance(raw_fields, dict):
                fields.update({str(k): str(v) for k, v in raw_fields.items()})
            for k in ("SAMLResponse", "RelayState"):
                if saml_data.get(k):
                    fields[k] = str(saml_data[k])
        if isinstance(saml_data, str) and isinstance(cas.get("data2"), dict):
            action = action or cas["data2"].get("redirectUrl") or cas["data2"].get("url")
            fields.setdefault("SAMLResponse", saml_data)
            if oauth_params.get("_relayState"):
                fields.setdefault("RelayState", oauth_params["_relayState"])
        if not action or "SAMLResponse" not in fields:
            raise RuntimeError(f"Casdoor SAML response form not found in {phase}: data_type={type(saml_data).__name__} keys={list(saml_data.keys()) if isinstance(saml_data, dict) else []}")
        initial_workos_response = session_post(session,
            action,
            headers=generic_headers(origin=oauth_origin, referer=f"{oauth_origin}/", json_body=False),
            data=fields,
            allow_redirects=True,
            timeout=35,
        )
        external_cb = action
    else:
        external_cb = oauth_params["redirectUri"] + "?" + urllib.parse.urlencode({"code": cas["data"], "state": oauth_params["state"]})
    log("protocolCasdoorLogin", phase=phase, status=r.status_code, bridge="kyl" if use_kyl_bridge else "session", externalCallback=sanitize_url(external_cb))

    if initial_workos_response is None:
        r = session_get(session, external_cb, headers=generic_headers(referer=login_referer), allow_redirects=True, timeout=35)
    else:
        r = initial_workos_response
    log("protocolExternalCallback", phase=phase, status=r.status_code, finalUrl=sanitize_url(r.url))
    auth_cb_url = r.url if "auth.openai.com/api/accounts/callback/workos" in r.url else None
    if "external.auth.openai.com/sso/signin-consent" in r.url or "interstitial_token" in r.text:
        form_fields = extract_hidden_form_fields(r.text)
        token = form_fields.get("interstitial_token") or extract_interstitial_token(r.text)
        if not token:
            raise RuntimeError(f"WorkOS interstitial token not found in {phase}: {r.text[:360]!r}")
        form_fields["interstitial_token"] = token
        form_fields["action"] = "confirm"
        r = session_post(session,
            "https://external.auth.openai.com/sso/interstitial",
            headers=generic_headers(origin="https://external.auth.openai.com", referer=r.url),
            data=form_fields,
            allow_redirects=False,
            timeout=30,
        )
        loc = redirect_location(r, "https://external.auth.openai.com/sso/interstitial")
        log("protocolWorkOSInterstitial", phase=phase, status=r.status_code, location=sanitize_url(loc or ""))
        if not loc:
            raise RuntimeError(f"WorkOS interstitial no redirect in {phase}: {r.status_code} {r.text[:300]!r}")
        auth_cb_url = loc
    if not auth_cb_url:
        auth_cb_url = r.url
    r = session_get(session, auth_cb_url, headers=generic_headers(referer="https://external.auth.openai.com/"), allow_redirects=True, timeout=35)
    log("protocolOpenAIWorkOSCallback", phase=phase, status=r.status_code, finalUrl=sanitize_url(r.url))
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI WorkOS callback HTTP {r.status_code} in {phase}: {r.text[:360]}")
    return r


def run(index: int) -> None:
    if not KYL_FINGERPRINT:
        raise RuntimeError("Missing KYL_FINGERPRINT")
    account = load_account(index)
    email = account["email"]
    sub = account["sub"]
    domain = email.split("@", 1)[1]
    connection = DOMAIN_CONNECTIONS[domain]
    workspace = DOMAIN_WORKSPACES[domain]
    if email in existing_auth_emails():
        log("protocolSkipExisting", index=index, email=email)
        return

    session = make_session()
    clear_openai_auth_session_cookies(session)
    if os.environ.get("PRESERVE_CASDOOR_SESSION", "0") != "1":
        removed_casdoor_sessions = clear_casdoor_session_cookies(session)
        log("protocolCasdoorSessionsCleared", count=removed_casdoor_sessions)
    before = auth_snapshot()
    start: Dict[str, Any]
    codex_verifier = ""
    if DIRECT_CODEX_SAVE:
        codex_verifier = code_verifier()
        start = {"state": oauth_state()}
        start_url = codex_auth_start_url(start["state"], codex_verifier)
    else:
        start = cpa_get("/codex-auth-url?is_webui=false")
        start_url = remove_prompt(start["url"])
    log(
        "protocolStart",
        index=index,
        email=email,
        sub=sub,
        connection=connection,
        workspace=workspace,
        mode="direct-save" if DIRECT_CODEX_SAVE else "cpa-callback",
        stateLen=len(start.get("state", "")),
        startUrl=sanitize_url(start_url),
    )

    r = session_get(session, start_url, headers=generic_headers(referer="https://auth.openai.com/"), allow_redirects=True, timeout=35)
    log("protocolOpenAIStart", status=r.status_code, finalUrl=sanitize_url(r.url))
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI start HTTP {r.status_code}: {r.text[:300]}")
    auth_bootstrap_url = response_continue_url(r)
    if auth_bootstrap_url:
        r = session_get(session, auth_bootstrap_url, headers=generic_headers(referer=start_url), allow_redirects=True, timeout=35)
        sync_openai_auth_cookies(session)
        log("protocolOpenAIAuthBootstrap", status=r.status_code, finalUrl=sanitize_url(r.url))
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI auth bootstrap HTTP {r.status_code}: {r.text[:300]}")

    body = {"username": {"kind": "email", "value": email}}
    r = session_post(session, 
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=openai_headers(json_body=True, referer=r.url),
        json=body,
        timeout=30,
    )
    data = json_or_error(r, "OpenAI username continue")
    sync_openai_auth_cookies(session)
    log("protocolOpenAIUsername", status=r.status_code, page=(data.get("page") or {}).get("type"), continueHost=urllib.parse.urlsplit(data.get("continue_url") or "").netloc)
    cont = data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url")
    if not cont or (data.get("page") or {}).get("type") == "sso":
        if cont:
            r_sso = session_get(session, cont, headers=generic_headers(referer=r.url), allow_redirects=True, timeout=30)
            sync_openai_auth_cookies(session)
            log("protocolOpenAISsoPage", status=r_sso.status_code, finalUrl=sanitize_url(r_sso.url))
        r = session_post(session, 
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=openai_headers(json_body=True, referer="https://auth.openai.com/sso"),
            json={"connection": connection, "connection_provider": 2},
            timeout=30,
        )
        data = json_or_error(r, "OpenAI connection continue")
        sync_openai_auth_cookies(session)
        cont = data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url")
        log("protocolOpenAIConnection", status=r.status_code, page=(data.get("page") or {}).get("type"), continueHost=urllib.parse.urlsplit(cont or "").netloc)
    if not cont:
        raise RuntimeError(f"OpenAI continue missing URL: keys={list(data.keys())}")

    # WorkOS authorize -> Casdoor authorize URL.
    # Keep OpenAI parent-domain cookies by default: the OAuth/login challenge is bound
    # to the browser session, and clearing these cookies causes
    # login_challenge_not_found_in_session during the WorkOS callback.
    if os.environ.get("DROP_PARENT_OPENAI_COOKIES") == "1":
        try:
            session.cookies.clear(domain=".openai.com")
        except Exception:
            pass
        try:
            session.cookies.clear(domain="openai.com")
        except Exception:
            pass

    r = session_get(session, cont, headers=generic_headers(referer="https://auth.openai.com/"), allow_redirects=False, timeout=30)
    log("protocolWorkOSAuthorize", status=r.status_code, url=sanitize_url(cont))
    oauth_url = redirect_location(r, cont)
    if not oauth_url:
        # Follow a short chain if WorkOS inserted one intermediate page.
        r2 = manual_get_until(session, cont, {"oauth.kyl23333.xyz", "oauth.luminet.cn"})
        oauth_url = r2.url if urllib.parse.urlsplit(r2.url).netloc in {"oauth.kyl23333.xyz", "oauth.luminet.cn"} else redirect_location(r2, cont)
    if not oauth_url or urllib.parse.urlsplit(oauth_url).netloc not in {"oauth.kyl23333.xyz", "oauth.luminet.cn"}:
        raise RuntimeError(f"WorkOS did not yield Casdoor URL: status={r.status_code} loc={sanitize_url(oauth_url or '')} text={r.text[:240]!r}")
    log("protocolCasdoorAuthorize", url=sanitize_url(oauth_url))

    parsed_oauth = urllib.parse.urlsplit(oauth_url)
    oauth_origin = f"{parsed_oauth.scheme}://{parsed_oauth.netloc}"

    if os.environ.get("CASDOOR_BRIDGE", "kyl") == "direct":
        removed_casdoor = clear_domain_cookies(session, urllib.parse.urlsplit(oauth_origin).netloc)
        log("protocolCasdoorCookiesCleared", host=urllib.parse.urlsplit(oauth_origin).netloc, count=removed_casdoor)
        oauth_params = casdoor_oauth_params_from_authorize_url(oauth_url)
        api_login_q = casdoor_api_login_query(oauth_params)
        login_body = {
            "application": oauth_params.get("_application") or CASDOOR_APP,
            "organization": "xuexi-2",
            "signinMethod": "Password",
            "type": oauth_params["responseType"],
            "language": "",
        }
        if os.environ.get("CASDOOR_DIRECT_MODE", "named") != "session":
            login_body.update({"name": email, "password": ""})
        session_get(session,
            f"{oauth_origin}/api/get-app-login?{api_login_q}",
            headers=generic_headers(origin=oauth_origin, referer=oauth_url, json_body=True),
            timeout=30,
        )
        login_referer = oauth_url
    else:
        callback_url, kyl_code, oauth_params, verifier = kyl_consent_callback(session, oauth_origin, oauth_url, email, sub)
        api_login_q = casdoor_api_login_query(oauth_params)
        application = oauth_params.get("_application") or CASDOOR_APP
        if oauth_params.get("_flow") == "saml":
            login_body = {
                "type": "saml",
                "application": application,
                "provider": CASDOOR_PROVIDER,
                "code": kyl_code,
                "samlRequest": oauth_params.get("_samlRequest") or "",
                "state": application,
                "invitationCode": "",
                "redirectUri": f"{oauth_origin}/callback",
                "method": "signin",
                "codeVerifier": verifier,
            }
        else:
            login_body = {
                "type": oauth_params["responseType"],
                "application": application,
                "provider": CASDOOR_PROVIDER,
                "code": kyl_code,
                "samlRequest": None,
                "state": application,
                "invitationCode": "",
                "redirectUri": f"{oauth_origin}/callback",
                "method": "signin",
                "codeVerifier": verifier,
            }
        login_referer = callback_url
    r = session_post(session, 
        f"{oauth_origin}/api/login?{api_login_q}",
        headers=generic_headers(origin=oauth_origin, referer=login_referer, json_body=True),
        json=login_body,
        timeout=30,
    )
    cas = json_or_error(r, "Casdoor api/login")
    if cas.get("status") != "ok" or not cas.get("data"):
        raise RuntimeError(f"Casdoor login failed: {cas}")
    flow = oauth_params.get("_flow") or "oidc"
    initial_workos_response: Optional[requests.Response] = None
    if flow == "saml":
        original_saml_q = urllib.parse.parse_qs(parsed_oauth.query, keep_blank_values=True)
        one_saml = lambda k: (original_saml_q.get(k) or [""])[0]
        saml_login_body = {
            "application": oauth_params.get("_application") or CASDOOR_SAML_APP,
            "language": "",
            "organization": "xuexi-2",
            "signinMethod": "Password",
            "type": "saml",
            "samlRequest": one_saml("SAMLRequest"),
            "relayState": one_saml("RelayState"),
        }
        r = session_post(session,
            f"{oauth_origin}/api/login?{api_login_q}",
            headers=generic_headers(origin=oauth_origin, referer=oauth_url, json_body=True),
            json=saml_login_body,
            timeout=30,
        )
        cas = json_or_error(r, "Casdoor saml api/login")
        if cas.get("status") != "ok" or not cas.get("data"):
            raise RuntimeError(f"Casdoor SAML login failed: keys={list(cas.keys())} msg={cas.get('msg')}")
        raw_saml_data = cas.get("data")
        log(
            "protocolCasdoorSamlLogin",
            status=r.status_code,
            dataType=type(raw_saml_data).__name__,
            dataLen=len(raw_saml_data) if isinstance(raw_saml_data, str) else None,
            dataPrefix=raw_saml_data[:40] if isinstance(raw_saml_data, str) else None,
            data2Type=type(cas.get("data2")).__name__,
            data2Len=len(cas.get("data2")) if isinstance(cas.get("data2"), str) else None,
            data2Prefix=cas.get("data2")[:40] if isinstance(cas.get("data2"), str) else None,
            data2Keys=list(cas.get("data2").keys()) if isinstance(cas.get("data2"), dict) else [],
            data3Type=type(cas.get("data3")).__name__,
            data3Len=len(cas.get("data3")) if isinstance(cas.get("data3"), str) else None,
            data3Prefix=cas.get("data3")[:80] if isinstance(cas.get("data3"), str) else None,
            dataKeys=list(raw_saml_data.keys()) if isinstance(raw_saml_data, dict) else [],
        )

    if flow == "saml":
        saml_data = cas.get("data")
        saml_html = saml_data if isinstance(saml_data, str) else json.dumps(saml_data, ensure_ascii=False)
        action = extract_form_action(saml_html)
        fields = extract_hidden_form_fields(saml_html)
        if not action and isinstance(saml_data, dict):
            action = saml_data.get("url") or saml_data.get("action") or saml_data.get("redirectUrl")
            raw_fields = saml_data.get("fields") or saml_data.get("form") or {}
            if isinstance(raw_fields, dict):
                fields.update({str(k): str(v) for k, v in raw_fields.items()})
            for k in ("SAMLResponse", "RelayState"):
                if saml_data.get(k):
                    fields[k] = str(saml_data[k])
        if isinstance(saml_data, str) and isinstance(cas.get("data2"), dict):
            action = action or cas["data2"].get("redirectUrl") or cas["data2"].get("url")
            fields.setdefault("SAMLResponse", saml_data)
            if oauth_params.get("_relayState"):
                fields.setdefault("RelayState", oauth_params["_relayState"])
        if not action or "SAMLResponse" not in fields:
            raise RuntimeError(f"Casdoor SAML response form not found: data_type={type(saml_data).__name__} keys={list(saml_data.keys()) if isinstance(saml_data, dict) else []}")
        initial_workos_response = session_post(session,
            action,
            headers=generic_headers(origin=oauth_origin, referer=f"{oauth_origin}/", json_body=False),
            data=fields,
            allow_redirects=True,
            timeout=35,
        )
        external_cb = action
    else:
        external_cb = oauth_params["redirectUri"] + "?" + urllib.parse.urlencode({"code": cas["data"], "state": oauth_params["state"]})
    callback_url = login_referer
    log("protocolCasdoorLogin", status=r.status_code, bridge=os.environ.get("CASDOOR_BRIDGE", "kyl"), externalCallback=sanitize_url(external_cb))

    # Complete WorkOS callback and post interstitial if needed.
    if initial_workos_response is None:
        r = session_get(session, external_cb, headers=generic_headers(referer=callback_url), allow_redirects=True, timeout=35)
    else:
        r = initial_workos_response
    log("protocolExternalCallback", status=r.status_code, finalUrl=sanitize_url(r.url))
    auth_cb_url = r.url if "auth.openai.com/api/accounts/callback/workos" in r.url else None
    if "external.auth.openai.com/sso/signin-consent" in r.url or "interstitial_token" in r.text:
        form_fields = extract_hidden_form_fields(r.text)
        token = form_fields.get("interstitial_token") or extract_interstitial_token(r.text)
        if not token:
            raise RuntimeError(f"WorkOS interstitial token not found: {r.text[:360]!r}")
        form_fields["interstitial_token"] = token
        form_fields["action"] = "confirm"
        r = session_post(session, 
            "https://external.auth.openai.com/sso/interstitial",
            headers=generic_headers(origin="https://external.auth.openai.com", referer=r.url),
            data=form_fields,
            allow_redirects=False,
            timeout=30,
        )
        loc = redirect_location(r, "https://external.auth.openai.com/sso/interstitial")
        log("protocolWorkOSInterstitial", status=r.status_code, location=sanitize_url(loc or ""))
        if not loc:
            raise RuntimeError(f"WorkOS interstitial no redirect: {r.status_code} {r.text[:300]!r}")
        auth_cb_url = loc
    if not auth_cb_url:
        # Follow if the last response was a redirect to OpenAI callback.
        auth_cb_url = r.url
    r = session_get(session, auth_cb_url, headers=generic_headers(referer="https://external.auth.openai.com/"), allow_redirects=True, timeout=35)
    log("protocolOpenAIWorkOSCallback", status=r.status_code, finalUrl=sanitize_url(r.url))
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI WorkOS callback HTTP {r.status_code}: {r.text[:360]}")

    consent_page = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    r_consent_page = session_get(session, 
        consent_page,
        headers=generic_headers(referer=r.url),
        allow_redirects=True,
        timeout=35,
    )
    sync_openai_auth_cookies(session)
    log("protocolCodexConsentPage", status=r_consent_page.status_code, finalUrl=sanitize_url(r_consent_page.url))
    secondary_cycle_done = False
    if r_consent_page.status_code >= 400:
        log("protocolSecondCycleStart", reason="consent_page", status=r_consent_page.status_code)
        cont = openai_continue_to_workos(session, email, connection, r.url, "secondary")
        r = finish_workos_from_cont(session, cont, email, sub, use_kyl_bridge=False, phase="secondary")
        secondary_cycle_done = True
        r_consent_page = session_get(session,
            consent_page,
            headers=generic_headers(referer=r.url),
            allow_redirects=True,
            timeout=35,
        )
        sync_openai_auth_cookies(session)
        log("protocolCodexConsentPage", phase="secondary", status=r_consent_page.status_code, finalUrl=sanitize_url(r_consent_page.url))

    r = session_post(session, 
        "https://auth.openai.com/api/accounts/workspace/select",
        headers=openai_headers(json_body=True, referer=r_consent_page.url),
        json={"workspace_id": workspace},
        timeout=35,
    )
    if r.status_code >= 400 and "no_valid_workspaces" in r.text and not secondary_cycle_done:
        log("protocolSecondCycleStart", reason="workspace_select", status=r.status_code)
        cont = openai_continue_to_workos(session, email, connection, r_consent_page.url, "secondary")
        r = finish_workos_from_cont(session, cont, email, sub, use_kyl_bridge=False, phase="secondary")
        secondary_cycle_done = True
        r_consent_page = session_get(session,
            consent_page,
            headers=generic_headers(referer=r.url),
            allow_redirects=True,
            timeout=35,
        )
        sync_openai_auth_cookies(session)
        log("protocolCodexConsentPage", phase="secondary", status=r_consent_page.status_code, finalUrl=sanitize_url(r_consent_page.url))
        r = session_post(session,
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=openai_headers(json_body=True, referer=r_consent_page.url),
            json={"workspace_id": workspace},
            timeout=35,
        )
    ws = json_or_error(r, "OpenAI workspace select")
    cont = ws.get("continue_url") or ((ws.get("page") or {}).get("payload") or {}).get("url")
    log("protocolWorkspaceSelect", status=r.status_code, hasContinue=bool(cont), workspaceCount=len(((ws.get("oai-client-auth-session") or {}).get("workspaces") or [])))
    if not cont:
        raise RuntimeError(f"workspace/select missing continue_url: keys={list(ws.keys())}")

    callback = extract_callback_from_redirects(session, cont, consent_page)
    log("protocolCodexCallback", callback=sanitize_url(callback))
    callback_query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(callback).query, keep_blank_values=True))
    callback_state = callback_query.get("state", "")
    callback_code = callback_query.get("code", "")
    callback_error = callback_query.get("error") or callback_query.get("error_description")
    if callback_state != start.get("state", ""):
        raise RuntimeError("Codex callback state mismatch")
    if callback_error:
        raise RuntimeError(f"Codex callback returned error: {callback_error}")
    if not callback_code:
        raise RuntimeError("Codex callback missing code")

    if DIRECT_CODEX_SAVE:
        token_resp = exchange_codex_tokens(session, callback_code, codex_verifier)
        saved = save_codex_auth(token_resp, email)
        log("protocolDirectSaved", index=index, email=email, file=pathlib.Path(saved).name)
    elif callback.startswith("http://127.0.0.1:8317/codex/callback"):
        rr = requests.get(callback, timeout=20, impersonate=HTTP_IMPERSONATE)
        log("protocolCpaDirectCallback", status=rr.status_code)
    else:
        submit = cpa_post("/oauth-callback", {"provider": "codex", "redirect_url": callback})
        log("protocolCpaOauthCallback", fields=list(submit.keys()))
    changed = changed_auth_files(before, email)
    if not changed:
        status = cpa_get("/get-auth-status?" + urllib.parse.urlencode({"state": start.get("state", "")}))
        raise RuntimeError(f"CPA callback submitted but auth file not observed; status={status}")
    log("protocolDone", index=index, email=email, files=[pathlib.Path(p).name for p in changed])


if __name__ == "__main__":
    run(ACCOUNT_INDEX)
