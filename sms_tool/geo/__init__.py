"""Single-authority proxy exit-geo resolution.

This package exists because exit-country detection used to be implemented
three times, with three different endpoint lists and two separate caches:

- ``proxy_entry.infer_region``        — template match on the credential (guess)
- ``paypal_proxy._probe_proxy_network`` — probe via ip-api / ipwho.is / ipapi.co
- ``browser_fingerprint_pool._query_geo_endpoints`` — probe via ipinfo / ipapi / ipwho.is

The three never shared results, so the protocol path bound its timezone to a
*guessed* country while a real measurement sat unused one call away.  A proxy
whose credential carries no region token therefore fell back to ``UTC`` +
``en-US`` — a Brazilian exit IP reporting a UTC clock is exactly the kind of
contradiction that gets a batch flagged.

:mod:`sms_tool.geo.resolver` collapses all three into one resolver with:

- one cache, keyed on the normalized proxy URL
- one endpoint list, ordered by cost and relevance
- one precedence chain: explicit hint → probe → template inference → empty

Callers keep their existing module-level entry points (they are thin shells
over this package) so the patch seams tests rely on stay intact.
"""

from .clock import (
    formatted_offset,
    now_in_timezone,
    offset_minutes_for_timezone,
    timezone_abbreviation,
    tzdata_available,
)
from .resolver import (
    GEO_ENDPOINTS,
    GeoResolver,
    ProxyGeo,
    normalize_geo_response,
    probe_exit_geo,
    remember_proxy_geo,
    reset_shared_geo_resolver,
    resolve_proxy_geo,
    shared_geo_resolver,
)

__all__ = [
    "GEO_ENDPOINTS",
    "GeoResolver",
    "ProxyGeo",
    "formatted_offset",
    "normalize_geo_response",
    "now_in_timezone",
    "offset_minutes_for_timezone",
    "probe_exit_geo",
    "remember_proxy_geo",
    "reset_shared_geo_resolver",
    "resolve_proxy_geo",
    "shared_geo_resolver",
    "timezone_abbreviation",
    "tzdata_available",
]
