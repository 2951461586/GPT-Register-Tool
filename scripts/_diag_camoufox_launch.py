# -*- coding: utf-8 -*-
"""指纹浏览器启动诊断：隔离「驱动本身不可用」与「代理导致卡死」。

用法:
    PYTHONPATH=. python scripts/_diag_camoufox_launch.py --driver camoufox
    PYTHONPATH=. python scripts/_diag_camoufox_launch.py --driver cloak
    PYTHONPATH=. python scripts/_diag_camoufox_launch.py --driver cloak --proxy ...

不带 --proxy 时能启动 = 驱动本身可用；不带能起、带上不行 = 代理/桥接问题。
"""
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", default="camoufox", help="camoufox / cloak / roxy / playwright")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--timeout-ms", type=int, default=240_000)
    ap.add_argument("--geoip", action="store_true")
    args = ap.parse_args()

    from sms_tool.config import initialize_runtime_config

    initialize_runtime_config()

    from sms_tool.registration_drivers.external_sessions import create_browser_session

    driver = str(args.driver or "camoufox").strip().lower()
    cfg = {
        "registration": {
            "drivers": {
                driver: {
                    "humanize": True,
                    "geoip": bool(args.geoip),
                    "use_proxy": bool(args.proxy),
                    "max_width": 1280,
                    "max_height": 900,
                }
            }
        }
    }

    print(f"[*] launching {driver} headless={args.headless} proxy={'yes' if args.proxy else 'no'} "
          f"geoip={bool(args.geoip)} timeout={args.timeout_ms}ms")
    started = time.monotonic()
    session = None
    try:
        session = create_browser_session(
            driver,
            config=cfg,
            proxy=args.proxy or None,
            headless=args.headless,
            timeout_ms=args.timeout_ms,
            locale="en-US",
            timezone_id="America/New_York",
        )
        session.__enter__()
        elapsed = time.monotonic() - started
        print(f"[OK] camoufox context ready in {elapsed:.1f}s")
        page = session.page
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60_000)
        print(f"[OK] goto chatgpt.com ok, title={page.title()!r}")
        return 0
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[FAIL] after {elapsed:.1f}s -> {type(exc).__name__}: {str(exc)[:400]}")
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
