"""诊断：直接 dump Remail 订单搜索响应 + 详情接口，确认 serviceToken/orderNo 是否可取。
仅用于调查两封失败 iCloud 邮箱能否复用，不写入任何业务文件。
"""
from __future__ import annotations

import json
import sys
from urllib.parse import quote

from sms_tool.config import initialize_runtime_config
from sms_tool import mailbox_remail


def redact(obj):
    txt = json.dumps(obj, ensure_ascii=False, default=str)
    return txt


def main() -> int:
    initialize_runtime_config()
    if not mailbox_remail._remail_enabled():
        print("[!] Remail 未启用（缺 API Key）")
        return 2

    emails = sys.argv[1:]
    if not emails:
        print("[!] 用法: _diag_remail_order.py <email1> [email2] ...")
        return 1

    for email in emails:
        email = email.strip().lower()
        print("=" * 70)
        print(f"[*] 搜索订单: {email}")
        try:
            resp = mailbox_remail._remail_request(
                "GET", "/v1/open/orders", auth=True,
                params={"search": email, "limit": 100},
            )
        except Exception as exc:
            print(f"    [!] 搜索失败: {exc}")
            continue

        items = resp.get("items") if isinstance(resp, dict) else []
        print(f"    [+] 搜索返回 items 数: {len(items or [])}")
        for item in (items or []):
            if not isinstance(item, dict):
                continue
            de = str(item.get("deliveryEmail") or "").strip().lower()
            if de != email:
                continue
            print(f"    --- match id={item.get('id')} status={item.get('status')} serviceMode={item.get('serviceMode')}")
            print(f"        含 orderNo: {'orderNo' in item and bool(item.get('orderNo'))}")
            print(f"        含 serviceToken: {'serviceToken' in item and bool(item.get('serviceToken'))}")
            print(f"        全部 keys: {sorted(item.keys())}")
            # 尝试详情接口：先按 id，再按 orderNo
            oid = str(item.get("id") or "").strip()
            ono = str(item.get("orderNo") or "").strip()
            for label, val in (("id", oid), ("orderNo", ono)):
                if not val:
                    continue
                try:
                    detail = mailbox_remail._remail_request(
                        "GET", "/v1/open/orders/" + quote(val, safe=""), auth=True,
                    )
                    if isinstance(detail, dict):
                        print(f"        [详情 by {label}={val}] keys={sorted(detail.keys())}")
                        print(f"            含 serviceToken: {'serviceToken' in detail and bool(detail.get('serviceToken'))}")
                        print(f"            含 orderNo: {'orderNo' in detail and bool(detail.get('orderNo'))}")
                        print(f"            deliveryEmail={detail.get('deliveryEmail')} status={detail.get('status')}")
                    else:
                        print(f"        [详情 by {label}={val}] 非字典: {type(detail)}")
                except Exception as exc:
                    print(f"        [详情 by {label}={val}] 失败: {_short(exc)}")
    return 0


def _short(exc) -> str:
    s = str(exc)
    return s if len(s) <= 200 else s[:200] + "..."


if __name__ == "__main__":
    sys.exit(main())
