#!/usr/bin/env python3
"""验证代理配置是否正确工作。

用法:
  python verify_proxy.py
"""

import sys

import requests

from sms_tool.config import load_merged_config
from sms_tool.phone_proxy import redact_proxy_url


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
        # socks5h needs requests[socks] (pysocks); registration itself goes
        # through httpx/curl_cffi, so this is a tool limitation, not a config error.
        return {"ok": False, "error": f"{e}（requests 测 socks5h 需安装 pysocks，注册链路不受影响）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _load_config():
    """Read the **merged** shards, not the legacy root ``config.json``.

    The root file stops being authoritative once the proxy/runtime/payment
    shards exist; reading it here silently hid the whole ``paypal`` section,
    so every PayPal proxy check in this tool verified an empty list.
    """
    try:
        return load_merged_config()
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
    print(f"\n[默认代理] {redact_proxy_url(default_proxy)}")
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
            print(f"  [{i}] {redact_proxy_url(proxy)} -> {status}")

    # 测试 PayPal 代理
    if paypal_proxies:
        print(f"\n[PayPal 代理] ({len(paypal_proxies)} 个)")
        for i, proxy in enumerate(paypal_proxies):
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  [{i}] {redact_proxy_url(proxy)} -> {status}")

    # 测试 PayPal 分阶段代理
    if stage_proxies:
        print(f"\n[PayPal 分阶段代理]")
        for stage, proxy in stage_proxies.items():
            if proxy == "direct":
                print(f"  {stage}: direct (直连)")
                continue
            result = test_proxy(proxy)
            status = f"OK IP={result['ip']} {result['country']}" if result["ok"] else f"FAIL {result['error']}"
            print(f"  {stage}: {redact_proxy_url(proxy)} -> {status}")

    # 测试直连
    print(f"\n[直连]")
    result = test_proxy(None)
    if result["ok"]:
        print(f"  [OK] IP: {result['ip']}  国家: {result['country']}  城市: {result['city']}")
    else:
        print(f"  [FAIL] {result['error']}")

    print("\n" + "=" * 60)
    print("配置来源: 合并配置分片（proxy.json/runtime.json/payment.json 存在时优先，否则回落 config.json）")
    print("=" * 60)


if __name__ == "__main__":
    main()
