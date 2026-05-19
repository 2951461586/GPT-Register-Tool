#!/usr/bin/env python3
"""验证代理配置是否正确工作。

用法:
  python verify_proxy.py
"""

import json
import sys

import requests


def test_proxy(proxy_url, test_url="https://ipinfo.io/json"):
    """测试代理连接和出口 IP 地理位置。"""
    proxies = {"http": proxy_url, "https": proxy_url}
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
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _load_config():
    try:
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    cfg = _load_config()
    proxy_cfg = cfg.get("proxy") or {}
    default_proxy = proxy_cfg.get("default")
    pool = proxy_cfg.get("pool") or ([default_proxy] if default_proxy else [])
    paypal_cfg = cfg.get("paypal") or {}
    paypal_proxies = paypal_cfg.get("proxies") or []
    stage_proxies = paypal_cfg.get("stage_proxies") or {}

    print("=" * 60)
    print("代理配置验证")
    print("=" * 60)

    # 测试默认代理
    print(f"\n[默认代理] {default_proxy or '(未配置)'}")
    if default_proxy:
        result = test_proxy(default_proxy)
        if result["ok"]:
            print(f"  [OK] IP: {result['ip']}  国家: {result['country']}  城市: {result['city']}")
        else:
            print(f"  [FAIL] {result['error']}")
    else:
        print("  [SKIP] 未配置")

    # 测试代理池
    if pool:
        print(f"\n[代理池] ({len(pool)} 个)")
        for i, proxy in enumerate(pool):
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  [{i}] {proxy} -> {status}")

    # 测试 PayPal 代理
    if paypal_proxies:
        print(f"\n[PayPal 代理] ({len(paypal_proxies)} 个)")
        for i, proxy in enumerate(paypal_proxies):
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  [{i}] {proxy} -> {status}")

    # 测试 PayPal 分阶段代理
    if stage_proxies:
        print(f"\n[PayPal 分阶段代理]")
        for stage, proxy in stage_proxies.items():
            if proxy == "direct":
                print(f"  {stage}: direct (直连)")
                continue
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  {stage}: {proxy} -> {status}")

    # 测试直连
    print(f"\n[直连]")
    result = test_proxy(None)
    if result["ok"]:
        print(f"  [OK] IP: {result['ip']}  国家: {result['country']}  城市: {result['city']}")
    else:
        print(f"  [FAIL] {result['error']}")

    print("\n" + "=" * 60)
    print("配置来源: config.json")
    print("=" * 60)


if __name__ == "__main__":
    main()
