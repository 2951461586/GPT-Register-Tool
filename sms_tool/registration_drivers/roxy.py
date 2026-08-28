"""RoxyBrowser registration driver."""

from typing import Any

from .playwright import run_browser_registration
from .external_sessions import RoxySeleniumSession


def run_roxy_registration(**kwargs: Any) -> dict:
    return run_browser_registration(driver_name="roxy", **kwargs)


def run_roxy_selenium_registration(**kwargs: Any) -> dict:
    """Run the shared registration state machine through Roxy Chromedriver."""
    config = kwargs.get("config") or {}
    registration = dict(config.get("registration") or {})
    drivers = dict(registration.get("drivers") or {})
    roxy = dict(drivers.get("roxy") or {})
    roxy["backend"] = "selenium"
    drivers["roxy"] = roxy
    registration["drivers"] = drivers
    config = dict(config)
    config["registration"] = registration
    kwargs["config"] = config
    def factory(_driver: str, **session_kwargs: Any):
        session_kwargs.pop("config", None)
        return RoxySeleniumSession(config=config, **session_kwargs)

    return run_browser_registration(driver_name="roxy", session_factory=factory, **kwargs)


__all__ = ["run_roxy_registration", "run_roxy_selenium_registration"]
