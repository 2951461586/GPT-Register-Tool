"""Stable seams shared by protocol and browser registration drivers.

Single source of truth
----------------------
The pure registration-driver vocabulary (:class:`RegistrationDriver`,
:class:`BrowserDriverSpec`, :data:`DRIVERS`, :data:`BROWSER_REGISTRATION_DRIVERS`,
:data:`KNOWN_DRIVER_ALIASES`, :func:`normalize_registration_driver`,
:func:`driver_choices`, :func:`driver_capabilities`) lives in
:mod:`sms_tool.kernel.driver_env` so ``sms_tool.config`` and
``sms_tool.registration`` can consume it without importing the browser-driver
package. Everything is re-exported here unchanged, alongside
:class:`BrowserRegistrationError`.
"""

from __future__ import annotations

from ..kernel.driver_env import (
    _ALIAS_TO_KEY as _ALIAS_TO_KEY,
    BROWSER_REGISTRATION_DRIVERS as BROWSER_REGISTRATION_DRIVERS,
    BrowserDriverSpec as BrowserDriverSpec,
    DRIVERS as DRIVERS,
    KNOWN_DRIVER_ALIASES as KNOWN_DRIVER_ALIASES,
    RegistrationDriver as RegistrationDriver,
    driver_capabilities as driver_capabilities,
    driver_choices as driver_choices,
    normalize_registration_driver as normalize_registration_driver,
)


class BrowserRegistrationError(RuntimeError):
    """Expected browser-flow failure with a stable, sanitized error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "browser_registration_failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}{': ' + self.detail if self.detail else ''}")
