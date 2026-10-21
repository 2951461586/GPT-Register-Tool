"""Protocol-lane post-signup warm-up (``registration.chatgpt_bootstrap.enabled``).

A protocol registration used to end the moment ``/api/auth/session`` handed out
an access token: one ``wham/usage`` probe, then silence.  A real browser lands on
ChatGPT and fires a few dozen reads (consent, settings, plan check, model list,
conversations ...) before the user does anything.  The 2026-10-11 iCloud cohort
(8 accounts, no 2FA, no warm-up) was revoked within minutes and the one account
re-checked through email OTP answered ``deleted or deactivated``; the reference
project (``turb-gpt-free-register`` ``core/chatgpt_bootstrap.py``) does this
warm-up and then lingers.  Whether the silence is the cause is **not** known --
this is the single-variable switch that makes it measurable.

Contract (inherited from the browser-lane module of the same name): decoration,
never a gate.  Read-only ``GET`` requests only, each failure is counted and
swallowed, a run of consecutive failures stops the walk, and nothing here may
change ``success``/``error`` of the registration.  Requests ride the registration
``curl_cffi`` session, so they carry the same cookies, exit and TLS fingerprint
that created the account -- a different client here would defeat the purpose.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..auth_headers import auth_impersonate, chatgpt_headers
from ..operator_output import emit as _emit
from .flags import registration_flag

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .handlers import RegistrationEmailWorkflow

_LOGGER = logging.getLogger(__name__)

#: Read-only walk, the GET subset of the reference project's authenticated
#: bootstrap (its ``sentinel/chat-requirements`` POSTs are deliberately left out:
#: they need a generated proof and risk a half-built challenge).
WARMUP_PATHS = (
    "/user_granular_consent",
    "/settings/is_adult",
    "/accounts/optimized/check",
    "/me",
    "/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
    "/settings/user",
    "/system_hints?mode=basic",
    "/system_hints?mode=plugins&suggestions=true",
    "/system_hints?mode=custom_agents",
    "/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
    "/calpico/chatgpt/rooms/summary?limit=10&include_pinned=true&include_magic_link=false",
    "/pins",
    "/conversations?offset=0&limit=28&order=updated&is_archived=false&is_starred=false",
    "/aip/first-party/eligibility",
)

#: A browser reports ``-Date.getTimezoneOffset()``; only the exits we register
#: through are listed, anything else reports UTC rather than a guessed zone.
_TZ_OFFSET_MIN = {"IN": 330, "VN": 420, "JP": 540, "KR": 540, "GB": 0, "DE": 60}


def tz_offset_minutes(country: Any) -> int:
    """``-Date.getTimezoneOffset()`` for an exit country; unknown reports UTC."""
    return _TZ_OFFSET_MIN.get(str(country or "").upper(), 0)


_MAX_CONSECUTIVE_FAILURES = 3
_PACE_SECONDS = (0.4, 1.6)
_MAX_DWELL_SECONDS = 300.0


def warmup_enabled(config: Any) -> bool:
    """``registration.chatgpt_bootstrap.enabled`` and ``.authenticated`` (default **off**)."""
    registration = config.get("registration") if isinstance(config, Mapping) else None
    section = registration.get("chatgpt_bootstrap") if isinstance(registration, Mapping) else None
    if not isinstance(section, Mapping):
        return False
    return _leaf_flag(section, "enabled", False) and _leaf_flag(section, "authenticated", True)


def _leaf_flag(section: Mapping[str, Any], key: str, default: bool) -> bool:
    # ``registration_flag`` reads ``registration.<key>``; reuse its two-sided
    # string/bool parsing for a leaf one level deeper.
    return registration_flag({"registration": {key: section.get(key, default)}}, key, default)


def dwell_range(config: Any) -> tuple[float, float]:
    """``registration.post_registration_dwell_seconds_range`` as ``(lo, hi)`` seconds."""
    registration = config.get("registration") if isinstance(config, Mapping) else None
    raw = str((registration or {}).get("post_registration_dwell_seconds_range") or "0,0") if registration else "0,0"
    try:
        values = [float(item.strip()) for item in raw.replace(";", ",").split(",") if item.strip()]
        lo = values[0] if values else 0.0
        hi = values[1] if len(values) > 1 else lo
    except (TypeError, ValueError):
        lo = hi = 0.0
    lo, hi = max(0.0, lo), max(0.0, hi)
    return (hi, lo) if hi < lo else (lo, hi)


def run_post_signup_warmup(
    workflow: RegistrationEmailWorkflow,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    uniform_fn: Callable[[float, float], float] = random.uniform,
) -> dict[str, Any]:
    """Walk :data:`WARMUP_PATHS` with the fresh access token, then dwell. Never raises."""
    s = workflow.runtime
    if not warmup_enabled(workflow.config):
        return {"skipped": True, "reason": "disabled"}
    if not (s.success and s.access_token and s.session is not None):
        return {"skipped": True, "reason": "no_session"}

    country = str(getattr(workflow, "fingerprint_country", "") or "").upper()
    tz = _TZ_OFFSET_MIN.get(country, 0)
    base = f"{str(s.chat_base).rstrip('/')}/backend-api"
    headers = chatgpt_headers(s.device_id, accept="application/json")
    headers["Authorization"] = f"Bearer {s.access_token}"
    headers["Referer"] = f"{str(s.chat_base).rstrip('/')}/"

    statuses: list[int] = []
    failures_in_row = 0
    try:
        for path in WARMUP_PATHS:
            status = 0
            try:
                response = s.session.get(
                    base + path.format(tz=tz),
                    headers=headers,
                    timeout=20,
                    impersonate=auth_impersonate(),
                    allow_redirects=False,
                )
                status = int(getattr(response, "status_code", 0) or 0)
            except Exception:
                status = 0
            statuses.append(status)
            failures_in_row = 0 if 200 <= status < 400 else failures_in_row + 1
            if failures_in_row >= _MAX_CONSECUTIVE_FAILURES:
                break
            sleep_fn(uniform_fn(*_PACE_SECONDS))
        lo, hi = dwell_range(workflow.config)
        dwell = min(_MAX_DWELL_SECONDS, uniform_fn(lo, hi) if hi > lo else lo)
        if dwell > 0:
            sleep_fn(dwell)
    except Exception:
        dwell = 0.0
    result = {
        "ok": bool(statuses) and all(200 <= code < 400 for code in statuses),
        "attempted": len(statuses),
        "ok_count": sum(1 for code in statuses if 200 <= code < 400),
        "statuses": statuses,
        "dwell_seconds": round(dwell, 1),
    }
    _emit(
        _LOGGER,
        "  Post-signup warm-up: ok=%d/%d dwell=%.1fs statuses=%s",
        result["ok_count"],
        result["attempted"],
        result["dwell_seconds"],
        ",".join(str(code) for code in statuses),
    )
    return result


__all__ = ["WARMUP_PATHS", "dwell_range", "run_post_signup_warmup", "tz_offset_minutes", "warmup_enabled"]
