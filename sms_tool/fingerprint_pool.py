"""Protocol fingerprint pool for rotating registration identities.

Combines the browser impersonation profile (Chrome version, UA, sec-ch-ua)
with the geo profile (timezone, locale, language) into a unified
``ProtocolEnvironmentProfile``.  A ``FingerprintPool`` distributes these
profiles round-robin across registrations so consecutive accounts don't
share the same fingerprint cluster.

The pool reads from the existing ``auth_headers`` profile tables so the
canonical fingerprint definitions remain in one place.  This module adds:
- formal ``ProtocolEnvironmentProfile`` with ``validate()``
- thread-safe round-robin ``FingerprintPool``
- integration seam for ``registration_handlers``
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProtocolEnvironmentProfile:
    """A complete protocol registration fingerprint.

    Combines browser impersonation, user-agent, sec-ch-ua headers, and
    geo-specific timezone/locale into a single identity that can be
    applied to a protocol registration session.
    """

    name: str
    impersonate: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'
    timezone: str = "America/New_York"
    lang: str = "en-US"
    lang_full: str = "en-US,en;q=0.9"
    country: str = "US"

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty if valid)."""
        errors: list[str] = []
        if not self.name.strip():
            errors.append("name must not be blank")
        if not self.impersonate.strip():
            errors.append("impersonate must not be blank")
        if not self.user_agent.strip():
            errors.append("user_agent must not be blank")
        # sec_ch_ua is intentionally optional: Firefox emits no Sec-CH-UA client
        # hints, so a blank value is valid for Firefox-family profiles.
        if not self.timezone.strip():
            errors.append("timezone must not be blank")
        if not self.lang.strip():
            errors.append("lang must not be blank")
        return errors

    @property
    def headers(self) -> dict[str, str]:
        """Return the sec-ch-ua headers for this profile."""
        return {
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
            "accept-language": self.lang_full,
        }

    def apply_to(self, headers: dict[str, str]) -> dict[str, str]:
        """Merge fingerprint headers into an existing headers dict."""
        result = dict(headers)
        result.update(self.headers)
        if self.user_agent:
            result["User-Agent"] = self.user_agent
        return result


def _build_profiles() -> list[ProtocolEnvironmentProfile]:
    """Build validated profiles from the auth_headers profile tables."""
    from .auth_headers import AUTH_FINGERPRINT_PROFILES, _GEO_PROFILES

    profiles: list[ProtocolEnvironmentProfile] = []
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {item.value for item in BrowserType}
    except Exception:
        supported = set(AUTH_FINGERPRINT_PROFILES)
    # The canonical profile key must stay separate from geo.  Geo is bound at
    # selection time from the account's proxy affinity (see ``_with_geo``), so
    # the only hard-coded value here is the DEFAULT geo used when no proxy is
    # supplied at all -- it is overridden on every ``next(proxy)`` call.
    geo_key = "US"
    geo = _GEO_PROFILES.get(geo_key, {})
    for browser_name, browser_cfg in AUTH_FINGERPRINT_PROFILES.items():
        if browser_name not in supported:
            continue
        profile = ProtocolEnvironmentProfile(
            name=browser_name,
            impersonate=str(browser_cfg.get("impersonate") or browser_name),
            user_agent=str(browser_cfg.get("user_agent") or ""),
            sec_ch_ua=str(browser_cfg.get("sec_ch_ua") or ""),
            sec_ch_ua_mobile=str(browser_cfg.get("sec_ch_ua_mobile") or "?0"),
            sec_ch_ua_platform=str(browser_cfg.get("sec_ch_ua_platform") or '"Windows"'),
            timezone=str(geo.get("timezone") or "America/New_York"),
            lang=str(geo.get("lang") or "en-US"),
            lang_full=str(geo.get("lang_full") or "en-US,en;q=0.9"),
            country=geo_key,
        )
        if not profile.validate():
            profiles.append(profile)
    return profiles


class FingerprintPool:
    """Thread-safe round-robin pool of protocol fingerprint profiles.

    Usage::

        pool = FingerprintPool.from_config(config)
        profile = pool.next()
        headers = profile.apply_to(base_headers)
    """

    def __init__(self, profiles: list[ProtocolEnvironmentProfile] | None = None) -> None:
        self._profiles = profiles or _build_profiles()
        self._index = 0
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "FingerprintPool":
        """Create a pool, optionally filtering by config settings."""
        pool = cls()
        if not isinstance(config, Mapping):
            return pool
        registration = config.get("registration", {})
        if not isinstance(registration, Mapping):
            return pool
        fp_cfg = registration.get("fingerprint_pool", {})
        if not isinstance(fp_cfg, Mapping):
            return pool
        # Filter by allowed countries if configured
        allowed_countries = fp_cfg.get("allowed_countries")
        if isinstance(allowed_countries, (list, tuple)) and allowed_countries:
            allowed = {str(c).upper() for c in allowed_countries}
            pool._profiles = [p for p in pool._profiles if p.country in allowed]
        return pool

    def _with_geo(self, profile: "ProtocolEnvironmentProfile", proxy: str | None) -> "ProtocolEnvironmentProfile":
        """Override a profile's locale/timezone to match the proxy egress.

        Without a proxy the profile keeps its default (US) geo; when a proxy is
        supplied the real exit country drives the timezone/lang so the protocol
        fingerprint stays consistent with the registration egress instead of
        being hard-coded to US.
        """
        if not proxy:
            return profile
        try:
            from .paypal_proxy import infer_proxy_country
            from .auth_headers import _GEO_PROFILES
        except Exception:
            return profile
        country = infer_proxy_country(proxy)
        geo = _GEO_PROFILES.get(country) if country else None
        if not geo:
            return profile
        return ProtocolEnvironmentProfile(
            name=profile.name,
            impersonate=profile.impersonate,
            user_agent=profile.user_agent,
            sec_ch_ua=profile.sec_ch_ua,
            sec_ch_ua_mobile=profile.sec_ch_ua_mobile,
            sec_ch_ua_platform=profile.sec_ch_ua_platform,
            timezone=str(geo.get("timezone") or profile.timezone),
            lang=str(geo.get("lang") or profile.lang),
            lang_full=str(geo.get("lang_full") or profile.lang_full),
            country=country,
        )

    def next(self, proxy: str | None = None) -> ProtocolEnvironmentProfile:
        """Return the next profile (round-robin), geo-aligned to the proxy exit."""
        if not self._profiles:
            # Fallback to a default profile
            return self._with_geo(
                ProtocolEnvironmentProfile(
                    name="default",
                    impersonate="chrome146",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                    sec_ch_ua='"Chromium";v="146", "Google Chrome";v="146", "Not.A/Brand";v="99"',
                ),
                proxy,
            )
        with self._lock:
            profile = self._profiles[self._index % len(self._profiles)]
            self._index += 1
        return self._with_geo(profile, proxy)

    def select(self, name: str, proxy: str | None = None) -> ProtocolEnvironmentProfile | None:
        """Select a specific profile by name, geo-aligned to the proxy exit."""
        canonical = str(name or "").strip().lower().split("_", 1)[0]
        for p in self._profiles:
            if p.name == canonical:
                return self._with_geo(p, proxy)
        return None

    @property
    def size(self) -> int:
        return len(self._profiles)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._profiles]


_SHARED_POOLS: dict[str, FingerprintPool] = {}
_SHARED_POOLS_LOCK = threading.Lock()


def _pool_config_key(config: Mapping[str, Any] | None) -> str:
    registration = config.get("registration", {}) if isinstance(config, Mapping) else {}
    fp_cfg = registration.get("fingerprint_pool", {}) if isinstance(registration, Mapping) else {}
    return json.dumps(fp_cfg if isinstance(fp_cfg, Mapping) else {}, sort_keys=True, default=str)


def shared_fingerprint_pool(config: Mapping[str, Any] | None = None) -> FingerprintPool:
    """Return one process-lifetime pool for an equivalent configuration."""
    key = _pool_config_key(config)
    with _SHARED_POOLS_LOCK:
        pool = _SHARED_POOLS.get(key)
        if pool is None:
            pool = FingerprintPool.from_config(config)
            _SHARED_POOLS[key] = pool
        return pool


__all__ = [
    "FingerprintPool",
    "ProtocolEnvironmentProfile",
    "shared_fingerprint_pool",
]
