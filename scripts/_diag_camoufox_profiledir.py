# -*- coding: utf-8 -*-
"""决定性对比：camoufox 的 profile 放 F 盘 vs C 盘，谁导致启动卡死。

背景：驱动用 ``runtime/browser_profiles/<driver>/<id>``（CWD 在 F 盘）作为
user_data_dir 以复用浏览器画像；裸跑用 C:\\...\\Temp 临时目录 3 秒就起来了。

**每次只跑一个 target**（单进程），避免 Playwright Sync API 的 asyncio loop 被污染。

用法:
    PYTHONPATH=. python scripts/_diag_camoufox_profiledir.py --target f
    PYTHONPATH=. python scripts/_diag_camoufox_profiledir.py --target c
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=("f", "c"), required=True)
    ap.add_argument("--timeout-ms", type=int, default=90_000)
    args = ap.parse_args()

    from browserforge.fingerprints import Screen
    from camoufox.sync_api import Camoufox

    if args.target == "f":
        label = "F盘 profile (runtime/browser_profiles/... 驱动默认)"
        profile = os.path.abspath(
            os.path.join("runtime", "browser_profiles", "camoufox", "_diag_fdrive")
        )
        os.makedirs(profile, exist_ok=True)
    else:
        label = "C盘 temp profile"
        profile = tempfile.mkdtemp(prefix="cx_cdrive_")

    started = time.monotonic()
    print(f"=== {label} ===", flush=True)
    print(f"    profile = {profile}", flush=True)
    print(f"    timeout = {args.timeout_ms}ms", flush=True)
    try:
        with Camoufox(
            headless=True,
            persistent_context=True,
            user_data_dir=profile,
            screen=Screen(max_width=1280, max_height=900),
            humanize=True,
            geoip=False,
            timeout=args.timeout_ms,  # Camoufox 的 timeout 单位是毫秒
        ) as ctx:
            page = ctx.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
            print(f"[OK] {label} -> {time.monotonic() - started:.1f}s title={page.title()!r}", flush=True)
            return 0
    except Exception as exc:
        print(f"[FAIL] {label} after {time.monotonic() - started:.1f}s "
              f"-> {type(exc).__name__}: {str(exc)[:400]}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
