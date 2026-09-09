"""Persistent account profile configuration, separate from process ownership."""

from pathlib import Path
from typing import Any, Mapping

from ...paths import runtime_dir


def _browser_profile_dir(driver: str, profile_id: str, config: Mapping[str, Any] | None = None) -> str:
    safe_id = "".join(c if c.isalnum() or c in "-._" else "_" for c in str(profile_id or ""))
    if not safe_id or safe_id in {".", ".."}:
        safe_id = "default"
    return str(runtime_dir(config or {}) / "browser_profiles" / driver / safe_id)


def _inject_screen_size(
    config: Mapping[str, Any], driver: str, size: tuple[int, int] | None,
) -> Mapping[str, Any]:
    """Feed the pooled screen size into a driver that can consume it (P1-3).

    Camoufox reads ``max_width`` / ``max_height`` from its driver config; without
    this it falls back to a hardcoded 1280x900 and ``BROWSER_PROFILE_POOL`` never
    reaches it. An explicitly configured ``max_width`` / ``max_height`` always wins,
    so this can only fill in the value, never override an operator's choice.
    """
    if not size or driver != "camoufox":
        return config
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        return config
    mutable = dict(config)
    registration = dict(mutable.get("registration") or {})
    drivers = dict(registration.get("drivers") or {})
    driver_cfg = dict(drivers.get(driver) or {})
    changed = False
    if not str(driver_cfg.get("max_width") or "").strip():
        driver_cfg["max_width"] = width
        changed = True
    if not str(driver_cfg.get("max_height") or "").strip():
        driver_cfg["max_height"] = height
        changed = True
    if not changed:
        return config
    drivers[driver] = driver_cfg
    registration["drivers"] = drivers
    mutable["registration"] = registration
    return mutable


def _inject_browser_profile(
    config: Mapping[str, Any], driver: str, browser_identity: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not browser_identity or not browser_identity.get("profile_id"):
        return config
    mutable = dict(config)
    registration = dict(mutable.get("registration") or {})
    drivers = dict(registration.get("drivers") or {})
    driver_cfg = dict(drivers.get(driver) or {})
    if driver in {"camoufox", "cloak", "playwright"}:
        if not str(driver_cfg.get("user_data_dir") or "").strip():
            driver_cfg["user_data_dir"] = _browser_profile_dir(
                driver, str(browser_identity["profile_id"]), config=mutable
            )
    elif driver == "roxy":
        driver_cfg.setdefault("delete_profile_after_run", False)
    drivers[driver] = driver_cfg
    registration["drivers"] = drivers
    mutable["registration"] = registration
    return mutable
