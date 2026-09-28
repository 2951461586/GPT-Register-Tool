"""curl_cffi transport bridge for sentinel_bridge.js on Windows/SOCKS proxies."""
from __future__ import annotations

import json
import sys

from curl_cffi import requests


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    response = requests.request(
        method=str(payload.get("method") or "GET"),
        url=str(payload.get("url") or ""),
        headers={str(k): str(v) for k, v in (payload.get("headers") or {}).items()},
        data=payload.get("body") if payload.get("body") is not None else None,
        proxy=str(payload.get("proxy") or "") or None,
        timeout=float(payload.get("timeout") or 120),
        impersonate="chrome136",
    )
    sys.stdout.write(json.dumps({
        "status": int(response.status_code),
        "body": response.text,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
