"""Persistent account profile configuration, separate from process ownership."""

from pathlib import Path
from typing import Any, Mapping


def _browser_profile_dir(driver: str, profile_id: str) -> str:
    safe_id = "".join(c if c.isalnum() or c in "-._" else "_" for c in str(profile_id or ""))
    if not safe_id or safe_id in {".", ".."}:
        safe_id = "default"
    return str(Path("runtime") / "browser_profiles" / driver / safe_id)


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
            driver_cfg["user_data_dir"] = _browser_profile_dir(driver, str(browser_identity["profile_id"]))
    elif driver == "roxy":
        driver_cfg.setdefault("delete_profile_after_run", False)
    drivers[driver] = driver_cfg
    registration["drivers"] = drivers
    mutable["registration"] = registration
    return mutable
