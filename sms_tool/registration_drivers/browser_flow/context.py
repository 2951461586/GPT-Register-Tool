"""Preparation of browser registration runtime context."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from ..external_sessions import _driver_config
from .dom_fields import _config_value


@dataclass(frozen=True)
class BrowserRegistrationContext:
    driver_config: Mapping[str, Any]
    headless: bool
    timeout: int
    locale: str
    timezone_id: str
    otp_timeout: int
    start_url: str
    chat_base: str
    auth_base: str


def prepare_browser_context(config: Mapping[str, Any], driver_name: str, browser_headless: bool | None) -> BrowserRegistrationContext:
    selected = _driver_config(config, driver_name)
    if browser_headless is not None:
        headless = bool(browser_headless)
    elif "open_headless" in selected:
        headless = bool(selected.get("open_headless"))
    elif "headless" in selected:
        headless = bool(selected.get("headless"))
    else:
        headless = bool(_config_value(config, "browser_headless", True))
    timeout = int(_config_value(config, "browser_timeout_seconds", 90) or 90)
    locale = str(_config_value(config, "browser_locale", "en-US") or "en-US")
    timezone_id = str(_config_value(config, "browser_timezone", "America/New_York") or "America/New_York")
    email_cfg = config.get("email_registration") if isinstance(config.get("email_registration"), Mapping) else {}
    chat_cfg = config.get("chatgpt") if isinstance(config.get("chatgpt"), Mapping) else {}
    chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
    auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    return BrowserRegistrationContext(
        selected,
        headless,
        timeout,
        locale,
        timezone_id,
        int(email_cfg.get("otp_timeout") or 300),
        str(selected.get("start_url") or f"{chat_base}/auth/login"),
        chat_base,
        auth_base,
    )
