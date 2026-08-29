"""RoxyBrowser registration driver."""

from typing import Any

from .playwright import run_browser_registration


def run_roxy_registration(**kwargs: Any) -> dict:
    """Run the shared registration state machine through Roxy's CDP track.

    Roxy owns the Chromium process, fingerprint and proxy for each profile;
    this driver only starts/stops the profile via Roxy's local REST API and
    attaches through Playwright's connect_over_cdp -- the same contract as the
    other anti-detect drivers (Cloak/Camoufox/AdsPower).
    """
    return run_browser_registration(driver_name="roxy", **kwargs)


__all__ = ["run_roxy_registration"]
