#!/usr/bin/env python3
"""验证代理配置是否正确工作。

用法:
  python scripts/verify_proxy.py
  python scripts/verify_proxy.py --chatgpt
  python scripts/verify_proxy.py --chatgpt --timeout 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from sms_tool.config import ConfigError, load_merged_config  # noqa: E402
from sms_tool.phone.phone_proxy import redact_proxy_url  # noqa: E402
from sms_tool.proxy.edge_probe import CLEAN, DEFAULT_CHAT_BASE, probe_openai_edge  # noqa: E402


def test_proxy(proxy_url, test_url="https://ipinfo.io/json"):
    """测试代理连接和出口 IP 地理位置。"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get(test_url, proxies=proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "ok": True,
                "ip": data.get("ip", ""),
                "country": data.get("country", ""),
                "city": data.get("city", ""),
                "org": data.get("org", ""),
            }
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except requests.exceptions.InvalidSchema as e:
        return {"ok": False, "error": f"{e}（requests 测 socks5h 需安装 pysocks，注册链路不受影响）"}
    except (requests.RequestException, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}


def _load_config():
    """Read the **merged** shards, not the legacy root ``config.json``."""
    try:
        return load_merged_config()
    except (ConfigError, OSError, ValueError, TypeError):
        return {}


def _chat_base(config):
    chatgpt = config.get("chatgpt")
    if not isinstance(chatgpt, dict):
        chatgpt = {}
    return str(chatgpt.get("chat_base_url") or DEFAULT_CHAT_BASE).rstrip("/") or DEFAULT_CHAT_BASE


def _report(label, proxy, config, *, with_chatgpt, timeout):
    """Print one proxy's connectivity result and, when asked, its edge verdict."""
    print(f"\n[{label}] {redact_proxy_url(proxy)}")
    if not proxy:
        print("  [SKIP] 未配置")
        return
    result = test_proxy(proxy)
    if result["ok"]:
        print(f"  [OK] IP: {result['ip']}  国家: {result['country']}  城市: {result['city']}")
    else:
        print(f"  [FAIL] {result['error']}")
    if with_chatgpt:
        verdict = probe_openai_edge(proxy, chat_base=_chat_base(config), timeout=timeout)
        detail = f"http {verdict.http_status}" if verdict.http_status else (verdict.error or "-")
        if verdict.blocked_by_cloudflare:
            detail += " cloudflare-challenge"
        flag = "OK" if verdict.status == CLEAN else "FAIL"
        print(f"  [{flag}] chatgpt-edge: {verdict.status} ({detail}, {verdict.elapsed_ms}ms)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="验证代理配置（连通性，可选 ChatGPT 边缘探测）")
    parser.add_argument(
        "--chatgpt",
        action="store_true",
        help="额外匿名探测 ChatGPT 登录边缘（只读）：区分 clean / blocked / degraded / dead",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="边缘探测超时秒数（默认 15）")
    args = parser.parse_args(argv)

    cfg = _load_config()
    proxy_cfg = cfg.get("proxy") or {}
    default_proxy = proxy_cfg.get("default")
    pool = proxy_cfg.get("pool") or ([default_proxy] if default_proxy else [])
    paypal_cfg = cfg.get("paypal") or {}
    paypal_proxies = paypal_cfg.get("proxies") or []
    stage_proxies = paypal_cfg.get("stage_proxies") or {}

    print("=" * 60)
    print("代理配置验证" + ("（含 ChatGPT 边缘探测）" if args.chatgpt else ""))
    print("=" * 60)

    _report("默认代理", default_proxy, cfg, with_chatgpt=args.chatgpt, timeout=args.timeout)

    if pool:
        print(f"\n[代理池] ({len(pool)} 个)")
        for i, proxy in enumerate(pool):
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  [{i}] {redact_proxy_url(proxy)} -> {status}")
            if args.chatgpt:
                verdict = probe_openai_edge(proxy, chat_base=_chat_base(cfg), timeout=args.timeout)
                print(f"      chatgpt-edge: {verdict.status} (http {verdict.http_status or verdict.error})")

    if paypal_proxies:
        print(f"\n[PayPal 代理] ({len(paypal_proxies)} 个)")
        for i, proxy in enumerate(paypal_proxies):
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  [{i}] {redact_proxy_url(proxy)} -> {status}")

    if stage_proxies:
        print("\n[PayPal 分阶段代理]")
        for stage, proxy in stage_proxies.items():
            if proxy == "direct":
                print(f"  {stage}: direct (直连)")
                continue
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  {stage}: {redact_proxy_url(proxy)} -> {status}")

    print("\n[直连]")
    if args.chatgpt:
        _report("直连", None, cfg, with_chatgpt=False, timeout=args.timeout)
    result = test_proxy(None)
    if result["ok"]:
        print(f"  [OK] IP: {result['ip']}  国家: {result['country']}  城市: {result['city']}")
    else:
        print(f"  [FAIL] {result['error']}")

    print("\n" + "=" * 60)
    print("配置来源: 合并配置分片（proxy.json/runtime.json/payment.json 存在时优先，否则回落 config.json）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
