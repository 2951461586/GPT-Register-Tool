#!/usr/bin/env python3
"""Prune orphaned endpoint records from the shared proxy-health file.

The health tracker keys rows by ``host:port`` (and ``host:port#sid-<hash>``).
When a pool is swapped to a new provider the old rows stay behind forever --
they are never queried, but they make the file (and any census) misleading.

This tool keeps only the endpoints that the **active** pools resolve to
(``proxy_registry.census``), i.e. registration + mailbox + payment.  Dry-run by
default; pass ``--apply`` to rewrite under the same OS file lock the tracker
uses.

    python scripts/proxy_health_prune.py
    python scripts/proxy_health_prune.py --apply
    python scripts/proxy_health_prune.py --path runtime/remail_proxy_health.json --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool import proxy_registry as proxy_registry  # noqa: E402
from sms_tool.config import load_merged_config  # noqa: E402
from sms_tool.cross_process_gate import cross_process_write_lock  # noqa: E402
from sms_tool.paths import runtime_file  # noqa: E402
from sms_tool.proxy_health import ProxyHealthTracker  # noqa: E402


def _endpoint_of(key: str) -> str:
    return str(key).split("#", 1)[0].lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune orphaned proxy-health endpoints.")
    parser.add_argument("--apply", action="store_true", help="rewrite the file (default: dry-run)")
    parser.add_argument("--path", default="", help="health file (default: runtime/registration_proxy_health.json)")
    args = parser.parse_args(argv)

    config = load_merged_config()
    active = {ep.lower() for ep in proxy_registry.census(config)["union_endpoints"]}
    path = Path(args.path) if args.path else runtime_file(config, "registration_proxy_health.json")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"unreadable or missing: {path}")
        return 1
    if not isinstance(data, dict):
        print(f"not a mapping: {path}")
        return 1

    kept = {k: v for k, v in data.items() if _endpoint_of(k) in active}
    removed = {k: v for k, v in data.items() if _endpoint_of(k) not in active}
    removed_endpoints = sorted({_endpoint_of(k) for k in removed})

    print(f"file:      {path}")
    print(f"active:    {len(active)} endpoint(s) {sorted(active)}")
    print(f"before:    {len(data)} key(s)")
    print(f"keep:      {len(kept)} key(s)")
    print(f"drop:      {len(removed)} key(s) across {len(removed_endpoints)} orphan endpoint(s)")
    for endpoint in removed_endpoints:
        count = sum(1 for k in removed if _endpoint_of(k) == endpoint)
        print(f"    - {endpoint}  {count} key(s)")

    if not args.apply:
        print("(dry-run; pass --apply to rewrite)")
        return 0

    lock_path = path.with_suffix(path.suffix + ".lock")
    with cross_process_write_lock(lock_path):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(kept, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
    # Keep the tracker import referenced so the file/format contract stays obvious.
    _ = ProxyHealthTracker
    print(f"applied: {path} now holds {len(kept)} key(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
