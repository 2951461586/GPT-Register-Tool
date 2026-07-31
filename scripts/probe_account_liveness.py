#!/usr/bin/env python3
"""轻量账号存活探测：扫一批账号的 ChatGPT /me 状态，统计 200/401/deactivated 比例。

只用已保存的 access_token 打一次 ``/backend-api/me``，**不做邮箱 OTP relogin**
（relogin 才会消耗邮箱额度）。因此这是一个便宜、快速的批量存活抽查：

  200                    -> 存活且 AT 有效
  401                    -> AT 失效（过期，或账号被封导致 AT 失效）
  403 / body deactivated -> 明确封禁
  其他                    -> 限流/网络/未知

按 email 列表从 ``sessions/`` 找每个账号最新的 session JSON 读 AT，并发探测。
不打印 AT、代理或响应正文，只输出聚合比例。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def load_config_proxy() -> str:
    try:
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    proxy = cfg.get("proxy", {}) if isinstance(cfg.get("proxy"), dict) else {}
    for key in ("registration", "default"):
        value = str(proxy.get(key) or "").strip()
        if value:
            return value
    pool = proxy.get("pool")
    if isinstance(pool, list) and pool:
        return str(pool[0] or "").strip()
    return ""


def latest_session(sessions_dir: str, email: str) -> Path | None:
    matches = glob.glob(os.path.join(sessions_dir, f"session_{email}_*.json"))
    if not matches:
        return None
    return Path(max(matches, key=os.path.getmtime))


def read_account(path: Path) -> tuple[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", 0.0
    return str(data.get("access_token") or "").strip(), float(data.get("created_at") or 0)


def probe(access_token: str, proxy: str, endpoint: str, timeout: int) -> tuple[int, str]:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": _UA, "Accept": "application/json"}
    try:
        from curl_cffi import requests as cr

        resp = cr.get(endpoint, headers=headers, proxies=proxies, timeout=timeout, impersonate="chrome")
        return int(getattr(resp, "status_code", 0)), str(getattr(resp, "text", "") or "")[:300]
    except Exception:
        try:
            import requests

            resp = requests.get(endpoint, headers=headers, proxies=proxies, timeout=timeout)
            return int(resp.status_code), str(resp.text or "")[:300]
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"[:200]


def classify(status: int, body: str) -> str:
    low = str(body or "").lower()
    deactivated = "deactivat" in low or "account_deactivated" in low or "has been deleted" in low
    if status == 200:
        return "alive"
    if deactivated:
        return "deactivated"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if status == -1:
        return "error"
    return f"http_{status}"


def main() -> int:
    parser = argparse.ArgumentParser(description="轻量账号存活探测（不 relogin）")
    parser.add_argument("--email-file", default="runtime/at200_emails.txt")
    parser.add_argument("--sessions-dir", default="sessions")
    parser.add_argument("--proxy", default=None, help="覆盖代理；默认读 config.json proxy.registration/default")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--endpoint", default="https://chatgpt.com/backend-api/me")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json-out", default="", help="把明细写入 JSON 文件（含 email→category）")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    proxy = args.proxy if args.proxy is not None else load_config_proxy()
    emails = [line.strip() for line in Path(args.email_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not emails:
        print("email 列表为空")
        return 2

    now = time.time()
    tasks: list[tuple[str, str, float, str | None]] = []
    for email in emails:
        session_file = latest_session(args.sessions_dir, email)
        if session_file is None:
            tasks.append((email, "", 0.0, "no_session"))
            continue
        access_token, created = read_account(session_file)
        if not access_token:
            tasks.append((email, "", created, "no_token"))
            continue
        tasks.append((email, access_token, created, None))

    def work(task: tuple[str, str, float, str | None]) -> dict:
        email, access_token, created, pre = task
        age = round((now - created) / 60, 1) if created else None
        if pre:
            return {"email": email, "category": pre, "status": None, "age_min": age}
        status, body = probe(access_token, proxy, args.endpoint, args.timeout)
        return {"email": email, "category": classify(status, body), "status": status, "age_min": age}

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for row in pool.map(work, tasks):
            results.append(row)

    counts = Counter(row["category"] for row in results)
    total = len(results)
    labels = {
        "alive": "存活 (HTTP 200)",
        "unauthorized": "AT 失效 (401)",
        "deactivated": "已封禁 (deactivated)",
        "forbidden": "禁止 (403)",
        "rate_limited": "限流 (429)",
        "error": "网络错误",
        "no_session": "无 session 文件",
        "no_token": "session 无 AT",
    }
    order = ["alive", "unauthorized", "deactivated", "forbidden", "rate_limited", "error", "no_session", "no_token"]

    print("=" * 48)
    print(f"账号存活探测: {total} 个账号   代理={'已配置' if proxy else '直连'}")
    print("=" * 48)
    for key in order + [c for c in counts if c not in order]:
        n = counts.get(key)
        if n:
            print(f"  {labels.get(key, key):24} {n:3}  ({n * 100 // total}%)")
    ages = [row["age_min"] for row in results if row.get("age_min")]
    if ages:
        print(f"\n账号年龄(探测时): {min(ages):.0f} - {max(ages):.0f} 分钟")
    alive = counts.get("alive", 0)
    dead = total - alive
    print(f"\n存活率: {alive}/{total} = {alive * 100 // total if total else 0}%    非存活: {dead} ({dead * 100 // total if total else 0}%)")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
