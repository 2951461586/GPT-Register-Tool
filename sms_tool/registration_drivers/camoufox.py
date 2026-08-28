"""Camoufox registration driver — anti-detect Firefox via Camoufox."""

from typing import Any

from .playwright import run_browser_registration


def run_camoufox_registration(**kwargs: Any) -> dict:
    return run_browser_registration(driver_name="camoufox", **kwargs)


__all__ = ["run_camoufox_registration"]
