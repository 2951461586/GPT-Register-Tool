"""Browser Use Cloud registration driver."""

from typing import Any

from .playwright import run_browser_registration


def run_browser_use_registration(**kwargs: Any) -> dict:
    return run_browser_registration(driver_name="browser_use", **kwargs)


__all__ = ["run_browser_use_registration"]
