"""Reference-ordered password registration lane (``registration.reference_lane``).

The reference project (``turb-gpt-free-register`` ``main.py::run_registration``)
registers in one fixed order::

    preflight -> anonymous ChatGPT bootstrap -> providers -> csrf -> signin
    (login_or_signup + prompt=login) -> follow authorize -> GET /create-account/password
    -> password-page Sentinel bundle -> user/register (email + password)
    -> GET email-otp/send -> wait code -> validate (authorize_continue Sentinel)
    -> external_url / callback branch, or about-you -> oauth_create_account Sentinel
    -> create_account -> callback + /api/auth/session -> authenticated bootstrap

This repo already carried most of those behaviours as default-off, single-variable
toggles (measured one at a time between 2026-10-07 and 2026-10-11).  This module
is the one switch that runs them **together**, in that order, on the password
lane.  It owns three things:

* :func:`apply_reference_lane` -- the preset: the operator's explicit
  ``registration.*`` values always win; only keys they left unset are filled in,
  and ``email_registration.registration_mode`` is forced to ``password``.
* :func:`pre_signin` -- the two stages the toggles could not express, an anonymous
  ChatGPT bootstrap and the ``providers`` read, run before ``csrf``.
* :func:`prime_password_bundle` -- the password page's Sentinel iframe load
  (``frame.html`` + three flows sharing one ``p``).

Everything else is the existing workflow; this module adds no stage of its own to
``RegistrationEmailWorkflow.run`` and never changes a verdict.  Bootstrap GETs are
decoration: each failure is swallowed, as in the reference (``strict=False``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from . import endpoints
from .auth_headers import auth_impersonate, chatgpt_headers, nextauth_headers
from .operator_output import emit as _emit
from . import registration_sentinel_stages as _sentinel_stages
from .registration_flags import registration_flag
from .registration_warmup import tz_offset_minutes
from .sanitizer import describe_exception

_LOGGER = logging.getLogger(__name__)

LANE_KEY = "reference_lane"

#: ``registration.<key>`` toggles the lane turns on unless the operator set them.
#: Each one is documented where it is read (``auth_flow.steps`` and the
#: ``registration_*_stages`` modules); together they are the reference's request shapes.
LANE_FLAGS = (
    "signin_screen_hint_login_or_signup",
    "signin_prompt_login",
    "turb_signin_authorize_context",
    "prime_create_account_password",
    "prime_navigation_headers",
    "prime_password_page_fatal",
    "otp_navigation_headers",
    "otp_validate_sentinel",
    "otp_external_url_branch",
    "prime_about_you_page",
)

#: The reference opens the sign-in funnel from the login page.
LOGIN_PAGE_REFERER = endpoints.CHATGPT_LOGIN_PAGE
_ANON_BASE = endpoints.CHATGPT_BACKEND_ANON
_CES_SETTINGS = endpoints.CHATGPT_CES_SETTINGS
_PROVIDERS = endpoints.CHATGPT_AUTH_PROVIDERS


def reference_lane_enabled(config: Any) -> bool:
    """``registration.reference_lane`` (default **off**)."""
    return registration_flag(config, LANE_KEY, False)


def apply_reference_lane(config: Any) -> Any:
    """Return ``config`` with the lane preset applied, or ``config`` itself when off.

    The returned mapping is a plain-``dict`` copy (callers re-freeze it through
    ``runtime_config_scope``); the input is never mutated.  An explicit operator
    value -- including ``False`` -- is kept, so a lane run can still switch one
    toggle off to bisect it.
    """
    if not isinstance(config, Mapping) or not reference_lane_enabled(config):
        return config
    root = dict(config)
    registration = dict(config.get("registration") or {})
    for key in LANE_FLAGS:
        registration.setdefault(key, True)
    bootstrap = dict(registration.get("chatgpt_bootstrap") or {})
    bootstrap.setdefault("enabled", True)
    bootstrap.setdefault("authenticated", True)
    registration["chatgpt_bootstrap"] = bootstrap
    humanize = dict(registration.get("humanize") or {})
    humanize.setdefault("enabled", True)
    registration["humanize"] = humanize
    root["registration"] = registration
    email = dict(config.get("email_registration") or {})
    email["registration_mode"] = "password"
    root["email_registration"] = email
    return root


def anonymous_bootstrap(self: Any) -> list[int]:
    """The reference's pre-signin anonymous reads of ``chatgpt.com``.

    ``accounts/check``, the CES settings, ``me`` and the regional pricing config --
    exactly the set the 2026-09-14 web trace shows on the login page, nothing
    more.  Returns the status codes (``0`` for a transport failure).
    """
    s = self.runtime
    country = str(getattr(self, "fingerprint_country", "") or "").upper()
    tz = tz_offset_minutes(country)
    urls = [
        f"{_ANON_BASE}/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        _CES_SETTINGS,
        f"{_ANON_BASE}/me",
    ]
    if country:
        urls.append(f"{_ANON_BASE}/checkout_pricing_config/configs/{country}")
    statuses: list[int] = []
    for url in urls:
        if url == _CES_SETTINGS:
            headers = nextauth_headers(s.device_id, session_id=s.session_logging_id, referer=LOGIN_PAGE_REFERER)
        else:
            headers = chatgpt_headers(
                s.device_id, accept="application/json", referer=LOGIN_PAGE_REFERER, session_id=s.session_logging_id
            )
        status = 0
        try:
            response = s.session.get(
                url, headers=headers, timeout=20, impersonate=auth_impersonate(), allow_redirects=False
            )
            status = int(getattr(response, "status_code", 0) or 0)
        except Exception as exc:
            _LOGGER.debug("anonymous bootstrap read failed: %s", describe_exception(exc))
        statuses.append(status)
    _emit(_LOGGER, "  Anonymous bootstrap: statuses=%s", ",".join(str(code) for code in statuses))
    return statuses


def read_providers(self: Any) -> int:
    """``GET /api/auth/providers`` -- the reference's step 1, before ``csrf``."""
    s = self.runtime
    status = 0
    try:
        response = s.session.get(
            _PROVIDERS,
            headers=nextauth_headers(s.device_id, session_id=s.session_logging_id, referer=LOGIN_PAGE_REFERER),
            timeout=20,
            impersonate=auth_impersonate(),
            allow_redirects=False,
        )
        status = int(getattr(response, "status_code", 0) or 0)
    except Exception as exc:
        _LOGGER.debug("providers read failed: %s", describe_exception(exc))
    _emit(_LOGGER, "  Auth providers: status=%s", status)
    return status


def pre_signin(self: Any) -> None:
    """Anonymous bootstrap, then ``providers`` -- everything before ``csrf``."""
    _emit(_LOGGER, "  Reference lane: pre-signin bootstrap")
    anonymous_bootstrap(self)
    read_providers(self)


def prime_password_bundle(self: Any) -> None:
    """Load the password page's Sentinel iframe and keep its `username_password_create` token.

    Runs right before `user/register` (the reference does it after the
    password-page navigation).  The mint itself lives in
    `registration_sentinel_stages` next to the other Sentinel stages.
    """
    _sentinel_stages.prime_password_sentinel_bundle(self, reference_page=True)


__all__ = [
    "LANE_FLAGS",
    "LOGIN_PAGE_REFERER",
    "anonymous_bootstrap",
    "apply_reference_lane",
    "pre_signin",
    "prime_password_bundle",
    "read_providers",
    "reference_lane_enabled",
]
