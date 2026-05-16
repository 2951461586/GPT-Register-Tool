import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG

# ==========================================
# Outlook mailbox integration
# Compatible with nb-register outlook_token.txt:
# email---password---refresh_token---access_token---0
# ==========================================
@dataclass
class MailboxAccount:
    email: str
    password: str = ""
    refresh_token: str = ""
    access_token: str = ""
    source: str = ""


OTP_RE = re.compile(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")


def _email_cfg():
    return CFG.get("email_registration", {})


def _default_nb_register_token_file():
    path = _email_cfg().get("nb_register_path", r"F:\epsoft\nb-register")
    return str(Path(path) / "outlook-register-service" / "Results" / "outlook_token.txt")


def _mailbox_from_config(args=None):
    args = args or argparse.Namespace()
    email = (getattr(args, "email", None) or _email_cfg().get("email") or "").strip().lower()
    if not email:
        return None
    return MailboxAccount(
        email=email,
        password=(getattr(args, "email_password", None) or _email_cfg().get("password") or "").strip(),
        refresh_token=(getattr(args, "email_refresh_token", None) or _email_cfg().get("refresh_token") or "").strip(),
        access_token=(getattr(args, "email_access_token", None) or _email_cfg().get("access_token") or "").strip(),
        source="config",
    )


def _parse_mailbox_token_file(path):
    records = []
    token_path = Path(path)
    if not token_path.exists():
        return records
    for line_no, raw in enumerate(token_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("---", 4)
        if len(parts) < 3:
            print(f"[!] Skip malformed mailbox line {token_path}:{line_no}")
            continue
        email, password, refresh_token = (part.strip() for part in parts[:3])
        access_token = parts[3].strip() if len(parts) >= 4 else ""
        if not email or not refresh_token:
            continue
        records.append(MailboxAccount(
            email=email.lower(),
            password=password,
            refresh_token=refresh_token,
            access_token=access_token,
            source=str(token_path),
        ))
    return records


def _parse_mailbox_password_file(path):
    records = []
    password_path = Path(path)
    if not password_path.exists():
        return records
    for line_no, raw in enumerate(password_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            print(f"[!] Skip malformed mailbox line {password_path}:{line_no}")
            continue
        email, password = (part.strip() for part in line.split(":", 1))
        if not email:
            continue
        records.append(MailboxAccount(
            email=email.lower(),
            password=password,
            source=str(password_path),
        ))
    return records


def _load_mailbox_pool(args=None):
    args = args or argparse.Namespace()
    direct = _mailbox_from_config(args)
    if direct:
        return [direct]
    configured = getattr(args, "mailbox_file", None) or _email_cfg().get("token_file")
    token_file = configured or _default_nb_register_token_file()
    return _parse_mailbox_token_file(token_file)


def _pick_mailbox(index=0, args=None):
    pool = _load_mailbox_pool(args)
    if not pool:
        return None
    return pool[index % len(pool)]


def _outlook_register_cfg():
    return CFG.get("outlook_register", {})


def _default_nb_register_outlook_dir():
    base = _email_cfg().get("nb_register_path") or _outlook_register_cfg().get("nb_register_path") or r"F:\epsoft\nb-register"
    return Path(base) / "outlook-register-service"


def _outlook_results_dir(args=None):
    args = args or argparse.Namespace()
    configured = (
        getattr(args, "outlook_results_dir", None)
        or _outlook_register_cfg().get("results_dir")
        or _email_cfg().get("results_dir")
    )
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).parent / "outlook_results").resolve()


def _outlook_script_path(args=None):
    args = args or argparse.Namespace()
    configured = getattr(args, "outlook_script", None) or _outlook_register_cfg().get("script_path")
    if configured:
        return Path(configured).resolve()
    return (_default_nb_register_outlook_dir() / "camoufox_register.py").resolve()


def _record_key(record):
    return (record.email or "").strip().lower()


def _new_mailbox_records(before, after):
    before_keys = {_record_key(item) for item in before}
    return [item for item in after if _record_key(item) and _record_key(item) not in before_keys]


def _outlook_env(proxy="", results_dir=None):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cfg = _outlook_register_cfg()
    env_map = {
        "OUTLOOK_REGISTER_OAUTH_VERIFICATION_CODE": cfg.get("oauth_verification_code", ""),
        "OUTLOOK_REGISTER_OAUTH_VERIFICATION_CODE_FILE": cfg.get("oauth_verification_code_file", ""),
        "OUTLOOK_REGISTER_EMAIL_ATTEMPTS": str(cfg.get("email_attempts", "")),
        "OUTLOOK_REGISTER_BOT_PROTECTION_WAIT": str(cfg.get("bot_protection_wait", "")),
    }
    for key, value in env_map.items():
        if str(value).strip():
            env[key] = str(value).strip()
    if results_dir:
        env["OUTLOOK_REGISTER_RESULTS_DIR"] = str(results_dir)
    if proxy:
        env["OUTLOOK_REGISTER_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
        env.setdefault("no_proxy", "localhost,127.0.0.1")
    return env


def _run_outlook_register_once(args=None, proxy=""):
    args = args or argparse.Namespace()
    script = _outlook_script_path(args)
    if not script.exists():
        raise FileNotFoundError(f"Outlook register script not found: {script}")
    results_dir = _outlook_results_dir(args)
    results_dir.mkdir(parents=True, exist_ok=True)
    unlogged = results_dir / "unlogged_email.txt"
    before = _parse_mailbox_password_file(unlogged)
    cfg = _outlook_register_cfg()
    suffix = getattr(args, "outlook_suffix", None) or cfg.get("email_suffix", "@outlook.com")
    max_retries = str(getattr(args, "outlook_max_captcha_retries", None) or cfg.get("max_captcha_retries", 10))
    timeout = int(getattr(args, "outlook_timeout", None) or cfg.get("timeout_seconds", 900))

    cmd = [
        sys.executable,
        "-u",
        str(script),
        "--suffix",
        suffix,
        "--max-retries",
        max_retries,
        "--results-dir",
        str(results_dir),
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])
    if getattr(args, "outlook_debug", False) or cfg.get("debug", False):
        cmd.append("--debug")

    print(f"[*] Outlook register: {script}")
    completed = subprocess.run(
        cmd,
        cwd=str(script.parent),
        env=_outlook_env(proxy, results_dir=results_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout if timeout > 0 else None,
    )
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip())
    after = _parse_mailbox_password_file(unlogged)
    new_records = _new_mailbox_records(before, after)
    if completed.returncode != 0 and not new_records:
        raise RuntimeError(f"Outlook register failed with exit code {completed.returncode}")
    if not new_records:
        raise RuntimeError("Outlook register completed but no new mailbox was written")
    return new_records[-1]


def _run_outlook_oauth(mailbox, args=None, proxy=""):
    args = args or argparse.Namespace()
    script = _outlook_script_path(args)
    if not script.exists():
        raise FileNotFoundError(f"Outlook register script not found: {script}")
    cfg = _outlook_register_cfg()
    email_cfg = _email_cfg()
    client_id = cfg.get("oauth_client_id") or email_cfg.get("oauth_client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    redirect_url = cfg.get("oauth_redirect_url") or email_cfg.get("oauth_redirect_url") or "https://login.microsoftonline.com/common/oauth2/nativeclient"
    scope_value = cfg.get("oauth_scope") or email_cfg.get("oauth_scope") or "offline_access https://graph.microsoft.com/Mail.Read"
    scopes = [part for part in str(scope_value).replace(",", " ").split() if part]
    timeout = int(getattr(args, "outlook_oauth_timeout", None) or cfg.get("oauth_timeout_seconds", 120))
    payload = {
        "email": mailbox.email,
        "password": mailbox.password,
        "proxy": proxy,
        "client_id": client_id,
        "redirect_url": redirect_url,
        "scopes": scopes,
    }
    code = (
        "import json, sys\n"
        "from camoufox_register import outlook_oauth\n"
        "payload=json.loads(sys.stdin.read() or '{}')\n"
        "result=outlook_oauth(email=payload.get('email',''), password=payload.get('password',''), "
        "proxy=payload.get('proxy',''), client_id=payload.get('client_id',''), "
        "redirect_url=payload.get('redirect_url',''), scopes=payload.get('scopes') or [])\n"
        "print(json.dumps(result, ensure_ascii=False), flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        cwd=str(script.parent),
        env=_outlook_env(proxy, results_dir=_outlook_results_dir(args)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(json.dumps(payload), timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"Outlook OAuth timed out after {timeout}s")
    if stderr:
        print(stderr.strip())
    try:
        result = json.loads((stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Outlook OAuth returned invalid JSON: {exc}; stdout={stdout[:500]!r}") from exc
    if process.returncode != 0:
        raise RuntimeError(f"Outlook OAuth exited with code {process.returncode}: {result}")
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or result.get("error_message") or "Outlook OAuth failed"))
    refresh_token = str(result.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("Outlook OAuth succeeded but returned no refresh_token")
    mailbox.refresh_token = refresh_token
    mailbox.access_token = str(result.get("access_token") or "").strip()
    mailbox.source = "outlook_batch_register"
    return mailbox


def _append_outlook_token_record(mailbox, results_dir):
    if not mailbox.refresh_token:
        return
    token_file = Path(results_dir) / "outlook_token.txt"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        _record_key(item)
        for item in _parse_mailbox_token_file(token_file)
    }
    if _record_key(mailbox) in existing:
        return
    with token_file.open("a", encoding="utf-8") as f:
        f.write(f"{mailbox.email}---{mailbox.password}---{mailbox.refresh_token}---{mailbox.access_token}---0\n")


def _save_mailbox_session_json(mailbox, output_dir, pattern="mailbox_{email}_{timestamp}.json"):
    if not mailbox.refresh_token:
        return ""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    safe_email = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", mailbox.email or "unknown")
    path = Path(output_dir) / pattern.format(email=safe_email, timestamp=int(time.time()))
    payload = {
        "email": mailbox.email,
        "password": mailbox.password,
        "refresh_token": mailbox.refresh_token,
        "access_token": mailbox.access_token,
        "source": mailbox.source,
        "created_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def run_outlook_batch(count=1, args=None):
    args = args or argparse.Namespace()
    cfg = _outlook_register_cfg()
    proxy = getattr(args, "proxy", None) or cfg.get("proxy") or CFG.get("proxy", {}).get("default") or ""
    results_dir = _outlook_results_dir(args)
    output_dir = getattr(args, "output_dir", None) or cfg.get("output_directory") or str(results_dir)
    pattern = cfg.get("filename_pattern", "mailbox_{email}_{timestamp}.json")
    skip_oauth = bool(getattr(args, "outlook_skip_oauth", False) or cfg.get("skip_oauth", False))

    registered = []
    saved = []
    failures = []
    for index in range(max(1, int(count or 1))):
        print(f"\n{'#' * 40}")
        print(f"  Outlook mailbox {index + 1}/{count}")
        print(f"{'#' * 40}")
        try:
            mailbox = _run_outlook_register_once(args=args, proxy=proxy)
            print(f"[*] Outlook mailbox registered: {mailbox.email}")
            if not skip_oauth:
                mailbox = _run_outlook_oauth(mailbox, args=args, proxy=proxy)
                _append_outlook_token_record(mailbox, results_dir)
                out_path = _save_mailbox_session_json(mailbox, output_dir, pattern=pattern)
                if out_path:
                    saved.append(out_path)
                    print(f"[*] Saved mailbox session: {out_path}")
            registered.append(mailbox)
        except Exception as e:
            print(f"[!] Outlook mailbox failed: {e}")
            failures.append(str(e))
    return {
        "success": bool(registered) and not failures,
        "registered": len(registered),
        "saved": len(saved),
        "failures": failures,
        "paths": saved,
    }


def _ms_oauth_refresh(mailbox):
    cfg = _email_cfg()
    client_id = cfg.get("oauth_client_id", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
    scope = cfg.get("oauth_scope", "offline_access https://graph.microsoft.com/Mail.Read")
    token_url = cfg.get("oauth_token_url", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    if not mailbox.refresh_token:
        raise RuntimeError("mailbox refresh_token is required")
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": mailbox.refresh_token,
        "scope": scope,
    }
    r = curl_requests.post(token_url, data=data, impersonate="chrome", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code != 200:
        raise RuntimeError(f"mailbox token refresh failed: {body}")
    access_token = body.get("access_token", "")
    if not access_token:
        raise RuntimeError("mailbox token refresh returned empty access token")
    if body.get("refresh_token"):
        mailbox.refresh_token = body["refresh_token"]
    mailbox.access_token = access_token
    return access_token


def _extract_otp_from_text(text):
    match = OTP_RE.search(text or "")
    return match.group(2) if match else ""


def _fetch_mailbox_messages(mailbox, limit=25):
    cfg = _email_cfg()
    token = mailbox.access_token or _ms_oauth_refresh(mailbox)
    graph_url = cfg.get("graph_messages_url", "https://graph.microsoft.com/v1.0/me/messages")
    params = {
        "$top": str(max(1, min(int(limit or 25), 100))),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,bodyPreview,body,toRecipients,ccRecipients,bccRecipients,internetMessageHeaders,receivedDateTime",
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
    }
    r = curl_requests.get(graph_url, params=params, headers=headers, impersonate="chrome", timeout=30)
    if r.status_code in (401, 403):
        token = _ms_oauth_refresh(mailbox)
        headers["Authorization"] = "Bearer " + token
        r = curl_requests.get(graph_url, params=params, headers=headers, impersonate="chrome", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"Graph messages failed: {body}")
    return body.get("value", [])


def _message_recipients(msg):
    recipients = []
    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in msg.get(key) or []:
            address = (((item or {}).get("emailAddress") or {}).get("address") or "").strip().lower()
            if address:
                recipients.append(address)
    for header in msg.get("internetMessageHeaders") or []:
        name = str((header or {}).get("name") or "").strip().lower()
        value = str((header or {}).get("value") or "")
        if name in {"to", "cc", "bcc", "delivered-to", "x-original-to", "x-forwarded-to"}:
            recipients.extend(addr.lower() for addr in re.findall(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", value))
    return set(recipients)


def _poll_email_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0):
    keyword = (subject_keyword or "").lower()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for msg in _fetch_mailbox_messages(mailbox):
                subject = str(msg.get("subject") or "")
                if keyword and keyword not in subject.lower():
                    continue
                recipients = _message_recipients(msg)
                if mailbox.email.lower() not in recipients and recipients:
                    continue
                body = str(msg.get("bodyPreview") or "") + "\n"
                body += str(((msg.get("body") or {}).get("content")) or "")
                otp = _extract_otp_from_text(body)
                if otp:
                    print(f" code:{otp}!")
                    return otp
        except Exception as e:
            print(f"[mailbox poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(5)
    print(" timeout")
    return None

