"""Roxy 无头浏览器出口诊断（不买邮箱、不注册）。

复现真实注册时的 Roxy + IPWO US 代理 setup，验证无头浏览器能否访问 chatgpt.com。
用法:
    set ROXY_API_TOKEN=xxx
    set DIAG_PROXY_URL=http://user:pass@host:port
    python scripts/_diag_roxy_egress.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import curl_cffi.requests as http

# 敏感信息走环境变量，禁止硬编码（曾误提交进版本库）
TOKEN = os.environ.get("ROXY_API_TOKEN", "")
WS = 149427
PROJ = 160525
API = "http://127.0.0.1:50000"
# 与失败 run 中 e921 挂的代理一致（proxy.pool 第一条 IPWO US）
PROXY_URL = os.environ.get("DIAG_PROXY_URL", "")


def _hdr():
    return {"token": TOKEN, "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _api(method, path, body=None):
    url = f"{API}/{path.lstrip('/')}"
    r = http.request(method, url, headers=_hdr(), json=body, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def _proxy_info():
    from urllib.parse import urlsplit, unquote
    p = urlsplit(PROXY_URL)
    proto = "SOCKS5" if p.scheme.startswith("socks5") else p.scheme.upper()
    info = {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": proto, "ipType": "IPV4",
            "protocol": proto, "host": p.hostname, "port": str(p.port)}
    if p.username:
        info["proxyUserName"] = unquote(p.username)
    if p.password:
        info["proxyPassword"] = unquote(p.password)
    return info


def main():
    if not TOKEN or not PROXY_URL:
        print("[!] 请先设置环境变量 ROXY_API_TOKEN 与 DIAG_PROXY_URL")
        return 1

    from playwright.sync_api import sync_playwright

    dir_id = None
    try:
        # 1) create
        st, body = _api("POST", "browser/create", {
            "workspaceId": WS, "projectId": PROJ,
            "name": f"diag-{int(time.time()*1000)}",
            "os": "Windows", "proxyInfo": _proxy_info(),
        })
        print(f"[create] http={st} code={body.get('code')} msg={body.get('msg')}")
        dir_id = (body.get("data") or {}).get("id") or body.get("data", {}).get("dirId") or body.get("id") or body.get("dirId")
        if not dir_id:
            print("[!] create 未返回 dirId，无法继续"); return 1
        print(f"[create] dirId={dir_id}")

        # 2) open headless
        st, body = _api("POST", "browser/open", {
            "workspaceId": WS, "dirId": dir_id, "args": [], "forceOpen": True, "headless": True,
        })
        print(f"[open] http={st} code={body.get('code')} msg={body.get('msg')}")
        data = body.get("data") or {}
        ws = data.get("ws") or data.get("wsEndpoint") or data.get("debuggerWsUrl")
        print(f"[open] ws={ws}")
        if not ws:
            print("[!] open 未返回 ws，无法 CDP 连接"); return 1

        # 3) CDP connect + navigate
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws, timeout=30000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            # 出口国
            try:
                country = page.evaluate("""async () => {
                  try { const r = await fetch('https://ipwho.is/', {credentials:'omit'}); const b = await r.json();
                    return String(b.country_code||'').toUpperCase(); } catch(e){ return 'ERR:'+e; } }""")
            except Exception as e:
                country = f"EVAL_ERR:{e}"
            print(f"[egress] 实际出口国 = {country}")
            # goto chatgpt.com
            try:
                resp = page.goto("https://chatgpt.com", timeout=30000, wait_until="domcontentloaded")
                print(f"[chatgpt] status={resp.status if resp else None} final_url={page.url}")
                title = page.title()
                print(f"[chatgpt] title={title!r}")
            except Exception as e:
                print(f"[chatgpt] 导航失败: {type(e).__name__}: {e}")
            browser.close()
        return 0
    finally:
        if dir_id:
            try:
                _api("POST", "browser/close", {"workspaceId": WS, "dirId": dir_id})
                _api("POST", "browser/delete", {"workspaceId": WS, "dirIds": [dir_id]})
                print(f"[cleanup] 已关闭并删除临时 profile {dir_id}")
            except Exception as e:
                print(f"[cleanup] 清理失败: {e}")


if __name__ == "__main__":
    sys.exit(main())
