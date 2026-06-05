#!/usr/bin/env python3
"""Outlook/Hotmail OTP extraction web service.

Input formats supported by the UI/API:
  email----password----client_id----refresh_token
  email---password---refresh_token---access_token---0

The password is accepted for operator compatibility but is not persisted and is
not used for Microsoft Graph refresh-token authentication.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MAIL_OTP_CONFIG", BASE_DIR / "config.json"))
STATIC_DIR = BASE_DIR / "static"

OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MS_CLIENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
KEYWORD_RE = re.compile(
    r"openai|chatgpt|验证码|驗證碼|验证|驗證|校验|校驗|代码|代碼|code|verification|verify|login|登录|登入|temporary",
    re.I,
)


def looks_ms_client_id(value: str) -> bool:
    return bool(MS_CLIENT_ID_RE.fullmatch(str(value or "").strip()))


def split_client_refresh(p2: str, p3: str) -> tuple[str, str]:
    p2 = str(p2 or "").strip()
    p3 = str(p3 or "").strip()
    if looks_ms_client_id(p2):
        return p2, p3
    if looks_ms_client_id(p3):
        return p3, p2
    return p2, p3


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


CFG = load_config()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > 128 * 1024:
        return {}
    raw = handler.rfile.read(length).decode("utf-8-sig", "replace")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_account_line(line: str) -> dict[str, str]:
    raw = str(line or "").strip()
    if not raw:
        return {}
    if "\t" in raw:
        parts = [p.strip() for p in raw.split("\t")]
    elif "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
    elif "----" in raw:
        parts = [p.strip() for p in raw.split("----", 3)]
        if len(parts) >= 4:
            client_id, refresh_token = split_client_refresh(parts[2], parts[3])
            return {
                "email": parts[0],
                "password": parts[1],
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        return {}
    elif "---" in raw:
        parts = [p.strip() for p in raw.split("---")]
        if len(parts) >= 3:
            return {
                "email": parts[0],
                "password": parts[1],
                "refresh_token": parts[2],
            }
        return {}
    else:
        parts = [p.strip() for p in re.split(r"-{4,}|\t|\|", raw)]
    if len(parts) >= 4:
        client_id, refresh_token = split_client_refresh(parts[2], parts[3])
        return {
            "email": parts[0],
            "password": parts[1],
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    return {}


def normalize_payload(data: dict[str, Any]) -> dict[str, str]:
    parsed = parse_account_line(str(data.get("account_line") or data.get("line") or ""))
    email = str(data.get("email") or parsed.get("email") or "").strip()
    password = str(data.get("password") or parsed.get("password") or "").strip()
    client_id = str(
        data.get("client_id")
        or data.get("clientId")
        or data.get("token")
        or parsed.get("client_id")
        or parsed.get("token")
        or CFG.get("default_client_id")
        or ""
    ).strip()
    refresh_token = str(
        data.get("refresh_token")
        or data.get("refreshToken")
        or parsed.get("refresh_token")
        or ""
    ).strip()
    subject_keyword = str(data.get("subject_keyword") or "").strip()
    return {
        "email": email,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "subject_keyword": subject_keyword,
    }


def post_form(url: str, form: dict[str, str], timeout: int) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "GPT-Register-MailOTP/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": json.loads(raw or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {"error": raw}
        return {"ok": False, "status": exc.code, "data": data}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": {"error": str(exc)}}


def get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": json.loads(raw or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {"error": raw}
        return {"ok": False, "status": exc.code, "data": data}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": {"error": str(exc)}}


def refresh_graph_token(client_id: str, refresh_token: str) -> dict[str, Any]:
    timeout = int(CFG.get("request_timeout_seconds") or 25)
    token_url = str(CFG.get("token_url") or "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    scope = str(CFG.get("graph_scope") or "https://graph.microsoft.com/.default offline_access")
    result = post_form(
        token_url,
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scope,
        },
        timeout,
    )
    data = result.get("data") or {}
    access = str(data.get("access_token") or "")
    if result.get("ok") and access:
        return {"ok": True, "access_token": access, "refresh_token": str(data.get("refresh_token") or refresh_token)}
    return {
        "ok": False,
        "error": safe_error(data) or f"token_http_{result.get('status')}",
        "status": result.get("status"),
    }


def fetch_graph_messages(access_token: str, limit: int) -> dict[str, Any]:
    timeout = int(CFG.get("request_timeout_seconds") or 25)
    base = str(CFG.get("graph_messages_url") or "https://graph.microsoft.com/v1.0/me/messages")
    query = urllib.parse.urlencode(
        {
            "$top": max(1, min(limit, 50)),
            "$orderby": "receivedDateTime desc",
            "$select": "subject,bodyPreview,body,receivedDateTime,from,toRecipients",
        }
    )
    return get_json(
        base + "?" + query,
        {"Authorization": "Bearer " + access_token, "Accept": "application/json"},
        timeout,
    )


def body_text(msg: dict[str, Any]) -> str:
    body = msg.get("body")
    if isinstance(body, dict):
        text = str(body.get("content") or "")
    else:
        text = str(body or "")
    return re.sub(r"<[^>]+>", " ", html.unescape(text))


def message_text(msg: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(msg.get("subject") or ""),
            str(msg.get("bodyPreview") or ""),
            body_text(msg),
        ]
    )


def score_message(msg: dict[str, Any], subject_keyword: str = "") -> int:
    text = message_text(msg)
    score = 0
    if KEYWORD_RE.search(text):
        score += 10
    if subject_keyword and subject_keyword.lower() in text.lower():
        score += 20
    if OTP_RE.search(text):
        score += 5
    return score


def extract_otp(messages: list[dict[str, Any]], subject_keyword: str = "") -> dict[str, Any]:
    ranked = sorted(messages, key=lambda m: score_message(m, subject_keyword), reverse=True)
    for msg in ranked:
        text = message_text(msg)
        matches = OTP_RE.findall(text)
        if not matches:
            continue
        if score_message(msg, subject_keyword) <= 0:
            continue
        code = matches[0]
        preview = str(msg.get("bodyPreview") or body_text(msg))[:300]
        return {
            "found": True,
            "code": code,
            "subject": str(msg.get("subject") or ""),
            "receivedDateTime": str(msg.get("receivedDateTime") or ""),
            "preview": preview,
        }
    return {"found": False, "code": ""}


def safe_error(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        desc = data.get("error_description") or data.get("message")
        if isinstance(err, dict):
            desc = err.get("message") or desc
            err = err.get("code") or err.get("error")
        return ": ".join(str(x) for x in [err, desc] if x)[:500]
    return str(data or "")[:500]


def _message_sender(msg: dict[str, Any]) -> str:
    sender = msg.get("from")
    if isinstance(sender, dict):
        addr = sender.get("emailAddress")
        if isinstance(addr, dict):
            return str(addr.get("address") or addr.get("name") or "")
    return ""


def _message_summary(msg: dict[str, Any], subject_keyword: str = "") -> dict[str, Any]:
    text = message_text(msg)
    codes = OTP_RE.findall(text)
    matched = bool(codes and score_message(msg, subject_keyword) > 0)
    return {
        "receivedAt": str(msg.get("receivedDateTime") or "")[:19].replace("T", " "),
        "from": _message_sender(msg),
        "subject": str(msg.get("subject") or ""),
        "preview": str(msg.get("bodyPreview") or body_text(msg))[:500],
        "code": codes[0] if matched else "",
        "matched": matched,
    }


def extract_flow(payload: dict[str, Any]) -> dict[str, Any]:
    account = normalize_payload(payload)
    email = account["email"]
    if not EMAIL_RE.match(email):
        return {"ok": False, "error": "邮箱格式无效"}
    if not account["client_id"]:
        return {"ok": False, "error": "缺少客户端ID"}
    if not account["refresh_token"]:
        return {"ok": False, "error": "缺少刷新令牌"}
    token = refresh_graph_token(account["client_id"], account["refresh_token"])
    if not token.get("ok"):
        return {"ok": False, "stage": "token", "email": email, "error": token.get("error") or "刷新令牌换取 access_token 失败"}
    limit = int(payload.get("limit") or CFG.get("message_limit") or 15)
    msgs = fetch_graph_messages(str(token["access_token"]), limit)
    if not msgs.get("ok"):
        return {"ok": False, "stage": "messages", "email": email, "error": safe_error(msgs.get("data")) or "读取邮件失败"}
    values = (msgs.get("data") or {}).get("value") or []
    if not isinstance(values, list):
        values = []
    subject_keyword = account.get("subject_keyword") or ""
    otp = extract_otp(values, subject_keyword)
    messages = [_message_summary(msg, subject_keyword) for msg in values]
    return {
        "ok": True,
        "email": email,
        "message_count": len(values),
        "messages": messages,
        "otp": otp,
        "new_refresh_token": token.get("refresh_token") if token.get("refresh_token") != account["refresh_token"] else "",
        "checked_at": int(time.time()),
    }


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Outlook / Hotmail 收件箱接码</title>
  <link rel="icon" href="/static/favicon.ico" />
  <style>
    :root{--bg:#F0EEE8;--panel:#F8F6F1;--panel2:#EFEAE1;--line:#DED8CD;--line2:#BFB6A8;--text:#2F343A;--sub:#66707A;--muted:#8A929B;--ok:#117A37;--danger:#B42318;--row:#F8F6F1;--rowAlt:#F0EEE8;--rowHover:#EFEAE1;--rowSel:#E8E2D8;--shadow:0 18px 45px rgba(47,52,58,.10)}
    html[data-theme=dark]{--bg:#191D22;--panel:#252A30;--panel2:#2F343A;--line:#3A414A;--line2:#555F69;--text:#E8ECEF;--sub:#B7C0C8;--muted:#8A929B;--ok:#6CE38A;--danger:#FF8A80;--row:#252A30;--rowAlt:#20252B;--rowHover:#2F343A;--rowSel:#39414A;--shadow:0 22px 50px rgba(0,0,0,.28)}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI",Inter,"Microsoft YaHei",Arial,sans-serif}.wrap{max-width:1240px;margin:0 auto;padding:28px 20px 46px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:20px}.brand{display:flex;align-items:center;gap:14px}.brand img{width:48px;height:48px;border-radius:14px;background:var(--panel);box-shadow:var(--shadow);padding:8px}.eyebrow{font-size:12px;color:var(--muted);font-weight:800;letter-spacing:.08em;text-transform:uppercase}.title{font-size:27px;font-weight:850;margin-top:4px}.toggle{width:40px;height:40px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--text);padding:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}.toggle:hover{border-color:var(--line2);background:var(--rowHover)}.toggle svg{width:20px;height:20px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}.layout{display:grid;grid-template-columns:360px minmax(0,1fr);gap:16px}@media(max-width:940px){.layout{grid-template-columns:1fr}.title{font-size:22px}}.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}.cardHead{height:44px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;border-bottom:1px solid var(--line);background:var(--panel2)}.cardHead h2{font-size:14px;margin:0;font-weight:850}.cardBody{padding:16px}.hint{font-size:13px;color:var(--sub);line-height:1.6;margin:0 0 12px}.label{display:block;font-size:12px;font-weight:850;color:var(--muted);margin:12px 0 6px}.input,textarea{width:100%;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);padding:11px 12px;font-size:13px;outline:none}textarea{min-height:86px;resize:none;overflow:hidden;font-family:Consolas,"Cascadia Mono",monospace}.input:focus,textarea:focus{border-color:var(--line2);box-shadow:0 0 0 3px rgba(138,146,155,.14)}.btn{height:36px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--text);font-weight:850;cursor:pointer;padding:0 14px}.btn:hover{border-color:var(--line2);background:var(--rowHover)}.btn.primary{width:100%;height:42px;margin-top:16px;background:var(--text);color:var(--bg);border-color:var(--text)}.status{font-size:12px;color:var(--sub);white-space:nowrap}.inboxTop{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center;margin-bottom:12px}.mailboxName{font-size:14px;font-weight:850;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.codeBox{display:flex;align-items:baseline;gap:12px;justify-content:flex-end}.codeLabel{font-size:12px;color:var(--muted);font-weight:800}.code{font-size:34px;letter-spacing:.12em;font-weight:950;color:var(--ok)}.code.danger{color:var(--danger);letter-spacing:0;font-size:20px}.tableWrap{border:1px solid var(--line);border-radius:10px;overflow-x:hidden;overflow-y:auto;background:var(--panel);height:auto;max-height:440px}.mailTable{width:100%;border-collapse:collapse;min-width:0;table-layout:fixed;font-size:13px}.mailTable th{height:30px;text-align:left;background:var(--rowAlt);color:var(--sub);font-size:12px;font-weight:850;border-bottom:1px solid var(--line);padding:0 10px;position:sticky;top:0;z-index:1}.mailTable td{height:34px;border-bottom:1px solid var(--line);padding:0 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mailTable tr{background:var(--row);cursor:pointer}.mailTable tr:nth-child(even){background:var(--rowAlt)}.mailTable tr:hover{background:var(--rowHover)}.mailTable tr.selected{background:var(--rowSel)}.time{width:152px;color:var(--sub);white-space:nowrap}.from{width:220px;color:var(--text);white-space:nowrap}.otpCol{width:104px;color:var(--ok);font-weight:900;white-space:nowrap;text-align:left}.empty{padding:42px 16px;color:var(--muted);text-align:center}.detail{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);min-height:152px;padding:14px}.detailTitle{font-size:14px;font-weight:850;margin-bottom:8px}.meta{font-size:12px;color:var(--muted);margin-bottom:10px}.preview{font-size:13px;color:var(--sub);line-height:1.65;white-space:pre-wrap}.pill{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:5px 9px;color:var(--sub);font-size:12px;margin:0 6px 6px 0}.footer{margin-top:18px;color:var(--muted);font-size:12px;text-align:center}.small{font-size:12px;color:var(--muted);line-height:1.55}
    html,body{overflow-x:hidden}.tableWrap{overflow-x:hidden;overflow-y:auto;height:auto;max-height:440px}.mailTable{min-width:0;table-layout:fixed}.mailTable th,.mailTable td{min-width:0}.mailTable tbody tr:nth-child(n+13){display:none}textarea{resize:none;overflow:hidden}.subjectCell{width:auto}.mailTable .mobileMeta{display:none}
    @media(max-width:720px){.wrap{padding:16px 12px 28px}.top{align-items:flex-start;gap:10px}.brand img{width:42px;height:42px}.toggle{width:36px;height:36px}.layout{gap:12px}.card{border-radius:14px}.cardHead{height:40px;padding:0 12px}.cardBody{padding:12px}.inboxTop{grid-template-columns:1fr;gap:8px}.codeBox{justify-content:flex-start}.code{font-size:30px}.pill{display:none}.tableWrap{border:0;background:transparent;max-height:none;overflow:visible}.mailTable,.mailTable thead,.mailTable tbody,.mailTable tr,.mailTable td{display:block;width:100%;min-width:0}.mailTable thead{display:none}.mailTable tr{border:1px solid var(--line);border-radius:10px;margin:0 0 8px;padding:9px 10px;background:var(--row);overflow:hidden}.mailTable tr:nth-child(even){background:var(--rowAlt)}.mailTable td{height:auto;border:0;padding:2px 0;white-space:normal;max-width:none}.mailTable td.time{font-size:12px;color:var(--muted)}.mailTable td.from{font-size:12px;color:var(--sub);width:auto}.mailTable td.subjectCell{font-size:14px;font-weight:850;color:var(--text);line-height:1.35}.mailTable td.otpCol{font-size:13px}.detail{min-height:120px}.preview{font-size:12.5px}.mailboxName{white-space:normal}.input,textarea{font-size:16px}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="brand"><img src="/static/black-kitten.png" alt="icon"><div><div class="eyebrow">GPT Register Tool</div><div class="title">Outlook / Hotmail · 收件箱接码</div></div></div>
      <button class="toggle" id="themeBtn" aria-label="切换黑夜" title="切换黑夜"></button>
    </div>
    <div class="layout">
      <section class="card"><div class="cardHead"><h2>账号</h2><span class="status">Graph Mail.Read</span></div><div class="cardBody">
        <p class="hint">粘贴 <b>账号----密码----客户端ID----刷新令牌</b> 后点击刷新；也兼容 <b>账号---密码---刷新令牌---access_token---0</b>，右侧会按项目收件箱样式展示最近邮件并自动提取验证码。</p>
        <label class="label">账号行</label><textarea id="accountLine" placeholder="user@hotmail.com----password----client_id----refresh_token"></textarea>
        <label class="label">邮箱账号</label><input class="input" id="email" placeholder="user@outlook.com">
        <label class="label">客户端 ID</label><input class="input" id="clientId" placeholder="client_id">
        <label class="label">刷新令牌</label><input class="input" id="rt" placeholder="refresh_token">
        <label class="label">密码（可选，仅格式兼容）</label><input class="input" id="password" type="password" placeholder="password">
        <label class="label">主题关键词（可选）</label><input class="input" id="keyword" placeholder="OpenAI / ChatGPT">
        <button class="btn primary" id="submitBtn">刷新收件箱并提取验证码</button><p class="small">页面不落库；如返回新 refresh_token，请手动覆盖旧令牌。</p>
      </div></section>
      <section class="card"><div class="cardHead"><h2>收件箱</h2><span class="status" id="statusText">等待加载</span></div><div class="cardBody">
        <div class="inboxTop"><div><div class="mailboxName" id="mailboxName">未选择邮箱</div><div><span class="pill">时间</span><span class="pill">发件人</span><span class="pill">主题</span></div></div><div class="codeBox"><span class="codeLabel">验证码</span><span id="code" class="code">------</span></div></div>
        <div class="tableWrap"><table class="mailTable"><thead><tr><th class="time">时间</th><th class="from">发件人</th><th>主题</th><th class="otpCol">验证码</th></tr></thead><tbody id="mailRows"><tr><td colspan="4"><div class="empty">等待提交账号后加载收件箱...</div></td></tr></tbody></table></div>
        <div class="detail" id="detail"><div class="detailTitle">邮件详情</div><div class="meta">点击右侧列表中的邮件查看内容预览。</div><div class="preview">暂无邮件。</div></div>
      </div></section>
    </div><div class="footer">mail.liziai.cloud · inbox style · light/dark</div>
  </div>
<script>
const $=id=>document.getElementById(id); const root=document.documentElement; let currentMessages=[];
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
const moonIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.7 6.7 0 0 0 9.8 9.8Z"/></svg>';
const sunIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
function applyTheme(t){root.setAttribute('data-theme',t); localStorage.setItem('theme',t); const btn=$('themeBtn'); const dark=t==='dark'; btn.innerHTML=dark?sunIcon:moonIcon; btn.setAttribute('aria-label',dark?'切换白天':'切换黑夜'); btn.setAttribute('title',dark?'切换白天':'切换黑夜')}
applyTheme(localStorage.getItem('theme')||'light'); $('themeBtn').onclick=()=>applyTheme(root.getAttribute('data-theme')==='dark'?'light':'dark');
function isUuid(v){return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((v||'').trim());}
function applyParts(p){if(p.length<4)return false; $('email').value=p[0]; $('password').value=p[1]; if(isUuid(p[2])||!isUuid(p[3])){$('clientId').value=p[2]; $('rt').value=p.slice(3).join('----');}else{$('clientId').value=p[3]; $('rt').value=p[2];} return true;}
function parseLine(){const line=$('accountLine').value.trim(); if(!line)return; let p=[]; if(line.includes('----')){p=line.split('----').map(x=>x.trim()); if(applyParts(p))return;} if(line.includes('---')){p=line.split('---').map(x=>x.trim()); if(p.length>=3){$('email').value=p[0]; $('password').value=p[1]; $('rt').value=p[2]; return;}} p=line.split(/\t|\|/).map(x=>x.trim()); applyParts(p);}
$('accountLine').addEventListener('blur',parseLine); function setStatus(s){$('statusText').textContent=s;}
function setSelectedCode(m){$('code').classList.remove('danger'); if(m&&m.code){$('code').textContent=m.code;} else if(m){$('code').textContent='无验证码'; $('code').classList.add('danger');} else {$('code').textContent='------';}}
function showDetail(i){document.querySelectorAll('#mailRows tr').forEach((tr,idx)=>tr.classList.toggle('selected',idx===i)); const m=currentMessages[i]; setSelectedCode(m); if(!m){$('detail').innerHTML='<div class="detailTitle">邮件详情</div><div class="meta">暂无邮件</div><div class="preview">暂无邮件。</div>';return;} $('detail').innerHTML=`<div class="detailTitle">${esc(m.subject||'(无主题)')}</div><div class="meta">${esc(m.receivedAt)} · ${esc(m.from||'未知发件人')}${m.code?' · 验证码 '+esc(m.code):' · 当前邮件无验证码'}</div><div class="preview">${esc(m.preview||'')}</div>`;}
function renderInbox(data){currentMessages=data.messages||[]; $('mailboxName').textContent=data.email||'收件箱'; const rows=$('mailRows'); if(!currentMessages.length){rows.innerHTML='<tr><td colspan="4"><div class="empty">最近邮件为空</div></td></tr>'; showDetail(-1); return;} rows.innerHTML=currentMessages.map((m,i)=>`<tr data-i="${i}"><td class="time">${esc(m.receivedAt)}</td><td class="from">${esc(m.from||'')}</td><td class="subjectCell" title="${esc(m.subject||'')}">${esc(m.subject||'(无主题)')}</td><td class="otpCol">${esc(m.code||'')}</td></tr>`).join(''); rows.querySelectorAll('tr').forEach(tr=>tr.onclick=()=>showDetail(Number(tr.dataset.i))); const hit=currentMessages.findIndex(m=>m.matched||m.code); showDetail(hit>=0?hit:0);}
$('submitBtn').onclick=async()=>{parseLine(); $('submitBtn').disabled=true; setStatus('正在加载...'); $('mailboxName').textContent=$('email').value||'收件箱'; $('code').textContent='......'; $('code').classList.remove('danger'); $('mailRows').innerHTML='<tr><td colspan="4"><div class="empty">正在刷新令牌并获取收件箱...</div></td></tr>'; try{const res=await fetch('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_line:$('accountLine').value,email:$('email').value,password:$('password').value,client_id:$('clientId').value,refresh_token:$('rt').value,subject_keyword:$('keyword').value,limit:12})}); const data=await res.json(); if(!data.ok){setStatus('加载失败'); $('code').textContent='FAILED'; $('code').classList.add('danger'); $('mailRows').innerHTML=`<tr><td colspan="4"><div class="empty">${esc(data.error||'加载失败')}</div></td></tr>`; $('detail').innerHTML=`<div class="detailTitle">错误</div><div class="meta">${esc(data.stage||'')}</div><div class="preview">${esc(JSON.stringify(data,null,2))}</div>`; return;} setStatus(`最近 ${data.message_count} 封邮件`); renderInbox(data); if(data.new_refresh_token){setStatus(`最近 ${data.message_count} 封邮件 · RT已更新`);} }catch(e){setStatus('请求异常'); $('code').textContent='ERROR'; $('code').classList.add('danger'); $('mailRows').innerHTML=`<tr><td colspan="4"><div class="empty">${esc(e)}</div></td></tr>`;} finally{$('submitBtn').disabled=false;}};
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "MailOTPWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html", "/mailbox"}:
            raw = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.startswith("/static/"):
            name = Path(path).name
            file_path = STATIC_DIR / name
            if file_path.exists() and file_path.is_file():
                raw = file_path.read_bytes()
                ctype = "image/png" if file_path.suffix.lower() == ".png" else "image/x-icon"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
        if path == "/health":
            json_response(self, 200, {"ok": True})
            return
        json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/extract":
            payload = read_json(self)
            result = extract_flow(payload)
            json_response(self, 200 if result.get("ok") else 400, result)
            return
        json_response(self, 404, {"ok": False, "error": "not_found"})


def main() -> None:
    host = str(CFG.get("bind_host") or os.environ.get("MAIL_OTP_HOST") or "127.0.0.1")
    port = int(os.environ.get("MAIL_OTP_PORT") or CFG.get("port") or 8791)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Mail OTP web listening on http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
