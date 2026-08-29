"""凭邮箱地址从 Remail 找回已购订单的 serviceToken + orderNo，写成 token 文件。

用途：之前 --buy-remail-mailbox 注册失败但订单已购（邮箱+token 仍在 Remail 侧），
此脚本找回凭据，配合 --mailbox-file 复用既有邮箱重跑注册，避免重复购买。

用法:
  python scripts/recover_remail_tokens.py <email1> <email2> ... [--out <file>]
"""
from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

from sms_tool.config import initialize_runtime_config
from sms_tool import mailbox_remail


def recover(email: str) -> dict | None:
    mailbox = SimpleNamespace(email=email.strip().lower(), order_no="", token="", purchase_id="")
    try:
        # 搜索接口返回的项含 orderNo 但不含 serviceToken
        search = mailbox_remail._lookup_remail_order(mailbox)
    except Exception as exc:
        print(f"[!] {email}: 查找失败 -> {exc}")
        return None
    if not isinstance(search, dict):
        print(f"[!] {email}: 返回非字典")
        return None
    order_no = str(search.get("orderNo") or "").strip()
    status = str(search.get("status") or "").strip().lower()
    if not order_no:
        print(f"[!] {email}: 搜索项缺 orderNo (status={status})")
        return None
    # 详情接口（按 orderNo）才返回 serviceToken
    try:
        detail = mailbox_remail._fetch_remail_order_detail(search)
    except Exception as exc:
        print(f"[!] {email}: 详情接口失败 -> {exc}")
        return None
    token = str(detail.get("serviceToken") or "").strip()
    if not token:
        print(f"[!] {email}: 详情接口仍缺 serviceToken (status={status})")
        return None
    email_out = str(detail.get("deliveryEmail") or "").strip().lower() or email.strip().lower()
    purchase_id = str(detail.get("id") or "").strip()
    print(f"[*] {email_out}: status={status} orderNo={order_no} token_len={len(token)}")
    return {"email": email_out, "token": token, "order_no": order_no, "purchase_id": purchase_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("emails", nargs="+", help="要找回的 Remail 邮箱")
    parser.add_argument("--out", default="runtime/recovered_remail_icloud.txt")
    args = parser.parse_args()

    initialize_runtime_config()
    if not mailbox_remail._remail_enabled():
        print("[!] Remail 未启用（缺 API Key），无法找回")
        return 2

    rows = []
    for email in args.emails:
        row = recover(email)
        if row:
            rows.append(row)

    if not rows:
        print("[!] 未找回任何可用邮箱")
        return 1

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(f"remail://{r['email']}---{r['token']}---{r['order_no']}---{r['purchase_id']}\n")
    print(f"[*] 已写入 {len(rows)} 条到 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
