"""Randomized operation pacing for browser registrations.

Mirrors ``turb-gpt-free-register``'s ``core/humanize.py``.  Rationale: every
account in a batch that sleeps ``time.sleep(0.25)`` at the same five places
shares one identical timing signature, which is a cheap clustering signal for
bot detection.  Randomizing the intervals breaks that signature.

Deliberate deviation from the reference project — **jitter around the proven
baseline instead of replacing it.**  turb swaps each sleep for an unrelated
range (e.g. ``(0.4, 1.2)``); these waits are functionally load-bearing here
(they let a page settle before the next interaction), so here the jitter is
applied *around* the original fixed value and ``enabled=False`` reproduces the
original fixed timing exactly.  Turning the feature off can therefore never
break a registration that works today.

Config (``registration.humanize``), both optional::

    "humanize": {"enabled": true, "factor": 1.0}

``factor`` scales the whole interval, so ``2.0`` doubles every wait — useful
when a proxy or target host is slower than the baseline assumes.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# kind -> (baseline_seconds, jitter_fraction).
# The baseline is the historical fixed sleep; jitter is applied around it.
HUMANIZE_DELAYS: dict[str, tuple[float, float]] = {
    "page_settle": (0.5, 0.5),   # 0.25 .. 0.75s
    "click": (0.25, 0.6),        # 0.10 .. 0.40s
    "state_probe": (0.25, 0.6),  # 0.10 .. 0.40s
    "retry": (0.4, 0.5),         # 0.20 .. 0.60s
    "default": (0.3, 0.5),
}

DEFAULT_HUMANIZE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "factor": 1.0,
}


def humanize_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read ``registration.humanize``; defaults keep the historical timing."""
    merged = dict(DEFAULT_HUMANIZE_CONFIG)
    section = (config or {}).get("registration")
    if isinstance(section, Mapping):
        raw = section.get("humanize")
        if isinstance(raw, Mapping):
            for key in DEFAULT_HUMANIZE_CONFIG:
                if key in raw:
                    merged[key] = raw[key]
    merged["enabled"] = bool(merged["enabled"])
    try:
        factor = float(merged["factor"])
    except (TypeError, ValueError):
        factor = 1.0
    merged["factor"] = factor if factor > 0 else 1.0
    return merged


def delay(
    kind: str = "default",
    *,
    config: Mapping[str, Any] | None = None,
    baseline: float | None = None,
    jitter: float | None = None,
) -> float:
    """Sleep for a randomized interval around the kind's baseline.

    Returns the number of seconds slept.  With the feature disabled this is
    exactly the baseline, i.e. byte-for-byte the old fixed-sleep behaviour.
    """
    base_default, jitter_default = HUMANIZE_DELAYS.get(kind, HUMANIZE_DELAYS["default"])
    base = float(baseline) if baseline is not None else base_default
    spread_default = jitter_default
    cfg = humanize_config(config)

    if not cfg["enabled"]:
        seconds = max(0.0, base)
    else:
        factor = cfg["factor"]
        spread = float(jitter) if jitter is not None else spread_default
        spread = min(max(spread, 0.0), 1.0)
        seconds = base * factor * random.uniform(1.0 - spread, 1.0 + spread)

    seconds = max(0.0, seconds)
    if seconds:
        time.sleep(seconds)
    logger.debug("[humanize] kind=%s seconds=%.3f enabled=%s", kind, seconds, cfg["enabled"])
    return seconds


__all__ = [
    "DEFAULT_HUMANIZE_CONFIG",
    "HUMANIZE_DELAYS",
    "delay",
    "humanize_config",
]
