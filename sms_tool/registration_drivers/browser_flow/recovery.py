"""Pure recovery policy for browser registration stages.

The browser orchestrator owns side effects; this module only classifies errors
and computes bounded retry/timeout decisions so the policy is testable without
launching Camoufox.
"""

from __future__ import annotations

from collections.abc import Mapping

from ...http_client import is_transient_transport_error


def is_navigation_retryable(error: object) -> bool:
    text = str(error or "").lower()
    return is_transient_transport_error(error) or "page.goto" in text or "navigation" in text and "abort" in text


def stage_timeout(config: Mapping | None, stage: str, default: int, *, minimum: int = 5, maximum: int = 300) -> int:
    registration = config.get("registration") if isinstance(config, Mapping) else {}
    values = registration.get("stage_timeouts") if isinstance(registration, Mapping) else {}
    try:
        value = int(values.get(stage) or default) if isinstance(values, Mapping) else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(value, maximum))


def probe_pending(access_token: object, probe: Mapping | None, success: bool) -> bool:
    """Whether an AT transport failure should be persisted for later probing."""
    try:
        status = int((probe or {}).get("status_code") or 0)
    except (TypeError, ValueError):
        status = 0
    return bool(str(access_token or "").strip()) and not success and status == 0


__all__ = ["is_navigation_retryable", "probe_pending", "stage_timeout"]
