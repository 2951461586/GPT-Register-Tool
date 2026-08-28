"""CloakBrowser registration driver."""

from typing import Any

from .playwright import run_browser_registration


def run_cloak_registration(**kwargs: Any) -> dict:
    return run_browser_registration(driver_name="cloak", **kwargs)


__all__ = ["run_cloak_registration"]
