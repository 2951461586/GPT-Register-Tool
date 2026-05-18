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


def main():
    print("=" * 60)
    print("代理配置验证")
    print("=" * 60)

    # 测试端口 17912 (日本代理池)
    print("\n[1] 测试端口 17912 (日本代理池)...")
    result = test_proxy("socks5h://127.0.0.1:17912")
    if result["ok"]:
        print(f"  [OK] 连接成功")
        print(f"  IP: {result['ip']}")
        print(f"  国家: {result['country']}")
        print(f"  城市: {result['city']}")
        print(f"  运营商: {result['org']}")
        if result["country"] == "JP":
            print(f"  [OK] 日本出口 - 可以触发 Coupon")
        else:
            print(f"  [WARN] 非日本出口 - 无法触发 Coupon")
            print(f"  请确保 Clash 中已启用 JP-Exit 代理组")
    else:
        print(f"  [FAIL] 连接失败: {result['error']}")
        print(f"  请确保 Clash Verge 已启动并应用配置")

    # 测试端口 7897 (Clash mixed-port)
    print("\n[2] 测试端口 7897 (Clash mixed-port)...")
    result = test_proxy("socks5h://127.0.0.1:7897")
    if result["ok"]:
        print(f"  [OK] 连接成功")
        print(f"  IP: {result['ip']}")
        print(f"  国家: {result['country']}")
        print(f"  城市: {result['city']}")
    else:
        print(f"  [FAIL] 连接失败: {result['error']}")

    # 测试直连
    print("\n[3] 测试直连 (无代理)...")
    result = test_proxy(None)
    if result["ok"]:
        print(f"  [OK] 连接成功")
        print(f"  IP: {result['ip']}")
        print(f"  国家: {result['country']}")
        print(f"  城市: {result['city']}")
    else:
        print(f"  [FAIL] 连接失败: {result['error']}")

    print("\n" + "=" * 60)
    print("代理配置说明")
    print("=" * 60)
    print("""
端口 17912: 日本代理池专用端口
  - 用于: ChatGPT Checkout, Stripe Init, Stripe PM
  - 出口: JP (日本, 触发 Coupon)
  - 配置: Clash listeners -> JP-Exit 代理组

端口 7897: Clash mixed-port
  - 用于: 通用代理
  - 出口: 取决于当前选择的节点

直连: 无代理
  - 用于: Stripe Confirm (pm-redirects.stripe.com)
  - 出口: 本地 IP

配置文件:
  - config.json: proxy.default = socks5h://127.0.0.1:17912
  - config.json: paypal.stage_proxies.* = socks5h://127.0.0.1:17912
  - gen_pp_link.py: PP_PROXIES = ["socks5h://127.0.0.1:17912"]
""")


if __name__ == "__main__":
    main()
