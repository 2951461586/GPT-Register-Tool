"""CLI boundary for proxy-pool administration (list / test / screen).

Translates ``argparse`` values into :mod:`sms_tool.proxy.admin` calls and emits
one JSON document. The domain module owns masking, probing and screening; this
module owns only "which command did the operator ask for".

Kept separate from ``commands/accounts.py`` (which owns account lanes) so the
proxy flags do not drag the account/payment catalogue into a diagnostic run.
"""

from __future__ import annotations

import json
from typing import Any


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_proxy_command(args: Any, *, config: Any = None) -> bool:
    """Execute the selected proxy command. Returns True (handled) or raises."""
    from ..desktop_ipc import emit_result
    from ..diagnostics import safe_print
    from ..proxy import admin

    if config is None:
        from ..config import CFG

        config = CFG
    lane = str(getattr(args, "proxy_read_lane", "") or admin.DEFAULT_LANE)
    if getattr(args, "proxy_list", False):
        # A list is local-only: the probe knobs are irrelevant, so they are not
        # even parsed here (an unparsable --proxy-workers must not fail a list).
        payload = admin.list_pool(config, lane=lane)
    else:
        country = str(getattr(args, "proxy_country", "") or "")
        workers = _as_int(getattr(args, "proxy_workers", 8) or 8, 8)
        timeout = _as_float(getattr(args, "proxy_timeout", 6.0) or 6.0, 6.0)
        pool = admin.resolve_pool(config, lane=lane, explicit=getattr(args, "proxy_pool", None))
        if getattr(args, "proxy_test", False):
            payload = admin.test_pool(pool, country=country, workers=workers, timeout=timeout)
        else:
            payload = admin.screen_report(
                pool,
                country=country,
                workers=workers,
                quality_min=_as_int(getattr(args, "proxy_quality_min", 0) or 0, 0),
                timeout=timeout,
                config=config,
            )
        payload["lane"] = lane
        payload["pool_size"] = len(pool)

    if bool(getattr(args, "desktop_ipc", False)):
        emit_result(payload, enabled=True)
    else:
        # ``safe_print`` (not ``print``) keeps the bare-print ratchet flat: the
        # ratchet exists to route operator-facing lines through one seam.
        safe_print(json.dumps(payload, ensure_ascii=False, indent=2))
    return True


__all__ = ["run_proxy_command"]
