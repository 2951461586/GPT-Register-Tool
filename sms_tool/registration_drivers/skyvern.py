"""Skyvern Browser Sessions registration driver."""

from typing import Any

from .playwright import run_browser_registration


def run_skyvern_registration(**kwargs: Any) -> dict:
    return run_browser_registration(driver_name="skyvern", **kwargs)


__all__ = ["run_skyvern_registration"]
