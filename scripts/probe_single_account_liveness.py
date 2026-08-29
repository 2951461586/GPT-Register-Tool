"""单账号测活：判定注册账号是否掉号（token 失效 / 账号停用）。

用法:
  python scripts/probe_single_account_liveness.py --latest
  python scripts/probe_single_account_liveness.py --session sessions/session_<email>_<ts>.json
  python scripts/probe_single_account_liveness.py --latest --proxy http://127.0.0.1:7890

判定规则（基于 sms_tool.account_liveness.probe_account_liveness 返回）:
  status == "active"                -> 账号存活（ok=True, 2xx）
  status == "token_invalid"         -> 掉号（401/403/token 失效）
  其余 / 检测失败                   -> 不确定（需人工复核）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from sms_tool.config import initialize_runtime_config
from sms_tool.account_liveness import probe_account_liveness


def _resolve_session(arg_session: str | None, latest: bool) -> str:
    if arg_session:
        if not os.path.isfile(arg_session):
            raise SystemExit(f"[!] session 文件不存在: {arg_session}")
        return arg_session
    if not latest:
        raise SystemExit("[!] 必须指定 --session <path> 或 --latest")
    matches = sorted(glob.glob(os.path.join("sessions", "session_*.json")), key=os.path.getmtime, reverse=True)
    if not matches:
        raise SystemExit("[!] sessions/ 下没有任何 session_*.json 文件")
    return matches[0]


def _judge(result: dict) -> str:
    status = result.get("status")
    if status == "active":
        return "存活"
    if status == "token_invalid":
        return "掉号(token_invalid)"
    return "不确定(需人工复核)"


def main() -> int:
    parser = argparse.ArgumentParser(description="单账号测活（掉号判定）")
    parser.add_argument("--session", help="指定 session 文件路径")
    parser.add_argument("--latest", action="store_true", help="自动选 sessions/ 内最新修改的 session 文件")
    parser.add_argument("--proxy", help="显式覆盖测活出口代理（默认走 select_operation_proxy liveness lane）")
    args = parser.parse_args()

    # 激活配置（LegacyConfigView / CFG 跟随 _CURRENT_CONFIG）
    initialize_runtime_config()

    session_path = _resolve_session(args.session, args.latest)
    with open(session_path, encoding="utf-8") as fh:
        account = json.load(fh)

    email = account.get("email", "<unknown>")
    has_token = bool(str(account.get("access_token") or "").strip())
    print(f"[*] session: {session_path}")
    print(f"[*] email:   {email}")
    print(f"[*] access_token 存在: {has_token}")
    if not has_token:
        print("[!] 缺少 access_token，无法测活")
        return 2

    result = probe_account_liveness(account, proxy=args.proxy, timeout=30)
    judgment = _judge(result)

    print("[*] 测活结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n=== 结论: {email} -> {judgment} ===")
    return 0 if judgment == "存活" else 1


if __name__ == "__main__":
    sys.exit(main())
