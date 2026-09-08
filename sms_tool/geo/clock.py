"""Clock facts derived from an IANA timezone name (P1-4).

Two places in the codebase need "what time is it over there": the browser
fingerprint (``timezone_offset_minutes``) and the Sentinel PoW payload.  Both
used to carry their own copy of the answer, and both copies were *written down*
rather than computed:

- ``BROWSER_LOCALE_PROFILES`` hardcodes DST offsets (``us`` = ``-7 * 60``
  "Pacific Daylight Time").  Correct in September, one hour wrong in January.
- ``sentinel/runner.py`` reads ``profile["timezone_offset_minutes"]``, which the
  protocol profile never sets, so it sent ``0`` — a UTC offset next to a
  declared ``America/New_York`` timezone.
- ``sentinel/client.py`` computed the offset itself and, when ZoneInfo failed,
  silently fell back to ``datetime.now().astimezone()``: the *local machine's*
  clock (GMT+0800 on this host) stamped into a proof that claims New York.

🔴 **Windows has no system timezone database.** ``zoneinfo.TZPATH`` is empty
unless the pure-Python ``tzdata`` package is installed, so every
``ZoneInfo(...)`` call raises ``ZoneInfoNotFoundError`` and every caller
silently degrades.  ``tzdata`` is now a hard dependency (see
``requirements.txt``); :func:`tzdata_available` exists so a missing install is
loud instead of invisible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_WARNED = False


def _zoneinfo():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo
    except Exception:  # pragma: no cover - Python < 3.9
        return None


def tzdata_available() -> bool:
    """True when IANA timezone lookups actually work on this host.

    Importing ``zoneinfo`` succeeds on every Python >= 3.9; only *constructing*
    a key fails. So this has to try a real lookup rather than check the import.
    """
    ZoneInfo = _zoneinfo()
    if ZoneInfo is None:
        return False
    try:
        datetime.now(ZoneInfo("UTC"))
    except Exception:
        return False
    return True


def _warn_once() -> None:
    """Log the missing-tzdata problem once per process, not once per account."""
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    logger.warning(
        "IANA timezone database unavailable (install the 'tzdata' package): "
        "timezone offsets fall back to the profile's configured value, which "
        "may be an hour off outside the season it was written in"
    )


def offset_minutes_for_timezone(
    tz_name: str, default: int = 0, *, at: datetime | None = None
) -> int:
    """UTC offset in minutes for ``tz_name`` at ``at`` (default: now), DST included.

    ``at`` exists so the DST boundary can be tested: the whole point of this
    function is that January and July give different answers for
    ``America/New_York``.

    ``default`` is used only when the name is blank/unknown or the tz database
    is missing — it is the caller's pre-existing value, not a guess.
    """
    name = str(tz_name or "").strip()
    if not name:
        return int(default)
    ZoneInfo = _zoneinfo()
    if ZoneInfo is None:
        _warn_once()
        return int(default)
    try:
        moment = (at or datetime.now()).replace(tzinfo=None)
        offset = moment.replace(tzinfo=ZoneInfo(name)).utcoffset()
    except Exception:
        _warn_once()
        return int(default)
    if offset is None:
        return int(default)
    return int(offset.total_seconds() // 60)


def now_in_timezone(tz_name: str) -> datetime:
    """``datetime.now()`` in ``tz_name``, or the local clock if unavailable.

    The Sentinel requirements token needs a *wall-clock string that agrees with
    the timezone the profile claims*. Falling back to the local clock is wrong
    but is the historical behaviour; the caller logs its own warning.
    """
    name = str(tz_name or "").strip()
    ZoneInfo = _zoneinfo()
    if name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(name))
        except Exception:
            _warn_once()
    return datetime.now().astimezone()


def timezone_abbreviation(tz_name: str) -> str:
    """Short zone abbreviation (``EDT``, ``JST``, ``-03``), ``""`` if unknown."""
    name = str(tz_name or "").strip()
    if not name:
        return ""
    ZoneInfo = _zoneinfo()
    if ZoneInfo is None:
        return ""
    try:
        return str(datetime.now(ZoneInfo(name)).tzname() or "")
    except Exception:
        return ""


def formatted_offset(minutes: int) -> str:
    """Format an offset the way a browser reports it: ``-240`` -> ``"-0400"``."""
    total = int(minutes or 0)
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 60:02d}{total % 60:02d}"


def utc_offset(tz_name: str) -> timedelta | None:
    """Raw :class:`~datetime.timedelta` offset, ``None`` when unknown."""
    name = str(tz_name or "").strip()
    if not name:
        return None
    ZoneInfo = _zoneinfo()
    if ZoneInfo is None:
        return None
    try:
        return datetime.now(ZoneInfo(name)).utcoffset()
    except Exception:
        return None


def as_int(value: Any, default: int = 0) -> int:
    """Best-effort int for offset fields coming from config/JSON."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "as_int",
    "formatted_offset",
    "now_in_timezone",
    "offset_minutes_for_timezone",
    "timezone_abbreviation",
    "tzdata_available",
    "utc_offset",
]
