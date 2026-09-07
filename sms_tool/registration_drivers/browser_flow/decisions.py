"""Pure decision fragments lifted out of ``run_browser_registration``.

Everything here is deterministic and side-effect free: no browser, no network,
no clock. That is the whole point -- these carry real branching logic but were
only reachable by driving a complete browser session, so a wrong branch showed
up as a failed registration rather than a failed test.

``run_browser_registration`` itself stays a linear script on purpose: its
statement order *is* the protocol (warm-up deliberately after 2FA enrollment,
two post-OTP reload rounds, ...). Splitting it by line count would destroy
that. Extracting the decisions is how it becomes testable without touching the
sequence.

Callers must reach these through the module namespace (``decisions.foo()``),
never ``from .decisions import foo`` -- see the patch-surface note at the top of
``orchestrator.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

# Applied only to the local Playwright driver; external anti-detect browsers
# (Roxy/Cloak/Camoufox/cloud) manage their own screen.
DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900


def attempt_number(proxy_metadata: Mapping[str, Any] | None) -> int:
    """Retry ordinal for this attempt, never below 1.

    ``0``, ``None`` and ``""`` all collapse to 1 via ``or``, negatives are
    clamped by ``max``, and junk strings fall through the except. A value below
    1 would silently disable the isolated retry profile below.
    """
    try:
        return max(1, int((proxy_metadata or {}).get("attempt") or 1))
    except (TypeError, ValueError):
        return 1


def browser_profile_key(account_key: str, attempt: int) -> str:
    """On-disk profile id for this attempt.

    Retries get their own profile so a stale auth page cannot make the next
    retry miss the signup email field.
    """
    if attempt <= 1:
        return account_key
    return f"{account_key}__retry{attempt}"


def playwright_viewport(
    profile: Mapping[str, Any], driver_name: str
) -> tuple[int, int] | None:
    """Screen size for the local Playwright driver, ``None`` for everyone else."""
    if driver_name != "playwright":
        return None
    return (
        int(profile.get("screen_width") or DEFAULT_VIEWPORT_WIDTH),
        int(profile.get("screen_height") or DEFAULT_VIEWPORT_HEIGHT),
    )


def aligned_locale_timezone(
    profile: Mapping[str, Any], locale: str, timezone_id: str
) -> tuple[str, str]:
    """Override the configured locale/timezone with the fingerprint pool's.

    Only overrides when the pool actually supplies a value -- an empty string
    here must keep the configured default, not blank it out.
    """
    language = profile.get("navigator_language")
    if language:
        locale = str(language)
    tz = profile.get("timezone_iana")
    if tz:
        timezone_id = str(tz)
    return locale, timezone_id


def geo_affinity_country(geo: Mapping[str, Any] | None, enabled: bool) -> str:
    """Egress country to pin onto ``proxy_affinity``, or ``""`` to leave it alone.

    Mirrors the protocol path, which records the registration country from the
    exit proxy credential. Without this, headless registrations stored
    ``registration_country=""`` and could not be attributed to a region.
    """
    if not enabled:
        return ""
    return str((geo or {}).get("country") or "").strip().upper()


def registration_state_and_basis(success: bool, probe_pending: bool) -> tuple[str, str]:
    """Final ``registration_state`` / ``registration_success_basis`` pair.

    Both strings are derived from the same two flags, so they are computed
    together -- keeping them as two separate expressions in the caller is how
    they drift apart.
    """
    if probe_pending:
        return "at_probe_pending", "at_probe_pending" if not success else "at_http_200"
    return ("active" if success else "failed"), ("at_http_200" if success else "")


def needs_chat_base_navigation(chat_base: str, page_url: str) -> bool:
    """True when the page is not yet on the chat host and needs a goto.

    Hostname comparison is lowercased on both sides; an unparseable or empty
    ``chat_base`` means "do not navigate".
    """
    chat_host = str(urlsplit(chat_base or "").hostname or "").lower()
    if not chat_host:
        return False
    return chat_host not in str(page_url or "").lower()
