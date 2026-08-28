"""Registration driver contracts and optional browser implementations."""

from .base import BROWSER_REGISTRATION_DRIVERS, RegistrationDriver, normalize_registration_driver

__all__ = ["BROWSER_REGISTRATION_DRIVERS", "RegistrationDriver", "normalize_registration_driver"]
