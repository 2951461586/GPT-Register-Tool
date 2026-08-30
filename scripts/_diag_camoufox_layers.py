# -*- coding: utf-8 -*-
"""分层定位 camoufox 启动卡死：裸跑 -> 逐个加选项，看哪一层挂。

用法:
    PYTHONPATH=. python scripts/_diag_camoufox_layers.py
"""
from __future__ import annotations

import time


def _attempt(label: str, fn, timeout_s: float = 200.0):
    started = time.monotonic()
    print(f"\n=== {label} ===", flush=True)
    try:
        fn()
        print(f"[OK] {label} -> {time.monotonic() - started:.1f}s", flush=True)
        return True
    except Exception as exc:
        print(f"[FAIL] {label} after {time.monotonic() - started:.1f}s "
              f"-> {type(exc).__name__}: {str(exc)[:300]}", flush=True)
        return False


def main() -> int:
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    print("camoufox 裸跑分层诊断（每层独立进程上下文）", flush=True)

    def layer1_bare():
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
            print("   title:", page.title(), flush=True)

    def layer2_persistent():
        import tempfile
        with Camoufox(headless=True, persistent_context=True,
                      user_data_dir=tempfile.mkdtemp(prefix="cx_")) as ctx:
            page = ctx.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
            print("   title:", page.title(), flush=True)

    def layer3_screen():
        import tempfile
        with Camoufox(headless=True, persistent_context=True,
                      user_data_dir=tempfile.mkdtemp(prefix="cx_"),
                      screen=Screen(max_width=1280, max_height=900)) as ctx:
            page = ctx.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
            print("   title:", page.title(), flush=True)

    def layer4_humanize_geoip():
        import tempfile
        with Camoufox(headless=True, persistent_context=True,
                      user_data_dir=tempfile.mkdtemp(prefix="cx_"),
                      screen=Screen(max_width=1280, max_height=900),
                      humanize=True, geoip=False) as ctx:
            page = ctx.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
            print("   title:", page.title(), flush=True)

    _attempt("L1 裸跑 headless", layer1_bare)
    _attempt("L2 + persistent_context", layer2_persistent)
    _attempt("L3 + screen", layer3_screen)
    _attempt("L4 + humanize/geoip (等同驱动配置)", layer4_humanize_geoip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
