"""Exit-geo resolution: one cache, one endpoint list, one precedence chain.

Precedence
----------
1. **hint** — a country already known from the credential template
   (``proxy_entry.infer_region``) or supplied by the caller. Zero cost, no
   network. Used as-is when no verification is requested.
2. **probe** — measure the real egress. Only runs when the hint is missing, or
   when ``verify_hint`` is on and the caller wants the hint confirmed.
3. **empty** — any failure degrades to an empty :class:`ProxyGeo`; callers fall
   back to their configured locale. Nothing here ever raises.

Why the hint wins by default
----------------------------
The protocol registration path previously called ``infer_region`` synchronously
on every account. Making it always probe would put a network round-trip on the
registration hot path for proxies that already advertise their region. The hint
is authoritative until a probe says otherwise, which keeps the common case free.

Endpoint order
--------------
``cloudflare.com/cdn-cgi/trace`` first: it is plain text (no JSON parse, no
third-party rate limit) and — more importantly — OpenAI sits behind Cloudflare,
so a success there is also a reachability signal for the real target. It only
yields ``ip`` + ``country``, so callers that need ``timezone`` / ``org`` keep
walking the JSON endpoints.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Chrome desktop UA used only for the geo probe request headers.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 6.0
# Successful lookups are cached for 30 min. Proxy exit IPs are sticky but do
# rotate (see ``proxy_entry.rotate_session``), so this must not be forever.
POSITIVE_TTL = 1800.0
# A failed probe is cached briefly only so a dead proxy does not get re-probed
# on every account in a batch. The old code cached failures permanently, which
# meant one network hiccup disabled geo detection for the rest of the process.
NEGATIVE_TTL = 60.0

_TRACE_ENDPOINT = "https://cloudflare.com/cdn-cgi/trace"

# (url, kind) — ``trace`` is plain text, ``json`` returns a geo document.
GEO_ENDPOINTS: tuple[tuple[str, str], ...] = (
    (_TRACE_ENDPOINT, "trace"),
    ("https://ipwho.is/", "json"),
    ("https://ipapi.co/json/", "json"),
    ("https://ipinfo.io/json", "json"),
)

_LOC_RE = re.compile(r"^loc=([A-Za-z]{2})\s*$", re.MULTILINE)
_TRACE_IP_RE = re.compile(r"^ip=([^\s]+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ProxyGeo:
    """One proxy's measured (or inferred) exit geography.

    ``source`` is how the answer was obtained and is carried through so callers
    can decide how much to trust it: ``hint`` is free but unverified, ``probe``
    is measured, ``cache`` is a previous measurement, ``none`` means unknown.
    """

    ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = ""
    org: str = ""
    source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        """Legacy dict shape consumed by ``build_browser_environment``.

        Kept byte-compatible with the old ``_normalize_geo_response`` output so
        existing persisted identity contexts and callers keep working.
        """
        return {
            "ip": self.ip,
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "timezone": self.timezone,
            "org": self.org,
        }

    @property
    def known(self) -> bool:
        return bool(self.country or self.timezone)

    def with_source(self, source: str) -> "ProxyGeo":
        return ProxyGeo(
            ip=self.ip,
            country=self.country,
            region=self.region,
            city=self.city,
            timezone=self.timezone,
            org=self.org,
            source=source,
        )


_EMPTY = ProxyGeo()


def normalize_geo_response(data: Any) -> ProxyGeo:
    """Normalize ipinfo / ipapi / ipwho.is JSON into a :class:`ProxyGeo`.

    Accepts the three providers' slightly different field names and returns the
    empty geo for anything unusable.
    """
    if not isinstance(data, Mapping):
        return _EMPTY
    timezone = data.get("timezone")
    if isinstance(timezone, Mapping):
        timezone = timezone.get("id") or timezone.get("name")
    country = str(
        data.get("country") or data.get("country_code") or data.get("countryCode") or ""
    ).strip().upper()
    org = data.get("org") or data.get("isp")
    if not org and isinstance(data.get("connection"), Mapping):
        org = data["connection"].get("org")
    return ProxyGeo(
        ip=str(data.get("ip") or data.get("query") or ""),
        country=country,
        region=str(data.get("region") or data.get("regionName") or ""),
        city=str(data.get("city") or ""),
        timezone=str(timezone or "").strip(),
        org=str(org or ""),
        source="probe",
    )


def _parse_trace(text: str) -> ProxyGeo:
    """Parse ``cloudflare.com/cdn-cgi/trace`` plain text."""
    body = str(text or "")
    loc = _LOC_RE.search(body)
    if not loc:
        return _EMPTY
    ip_match = _TRACE_IP_RE.search(body)
    return ProxyGeo(
        ip=str(ip_match.group(1) if ip_match else ""),
        country=str(loc.group(1)).strip().upper(),
        source="probe",
    )


def _http_get(url: str, proxy: str, timeout: float) -> tuple[int, str]:
    """GET ``url`` through ``proxy``. Returns ``(status, body)``.

    ``curl_cffi`` is preferred (handles socks5 + TLS impersonation); plain
    ``urllib`` is the fallback for http/https proxies.
    """
    try:
        from curl_cffi.requests import get as cffi_get

        response = cffi_get(
            url,
            proxy=proxy or None,
            impersonate="chrome",
            timeout=timeout,
            headers={"User-Agent": CHROME_UA, "Accept": "*/*"},
        )
        return int(response.status_code or 0), str(response.text or "")
    except Exception as exc:
        last = f"curl_cffi:{type(exc).__name__}: {exc}"
    try:
        import urllib.request

        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        opener = urllib.request.build_opener(handler)
        request = urllib.request.Request(
            url, headers={"User-Agent": CHROME_UA, "Accept": "*/*"}
        )
        with opener.open(request, timeout=timeout) as response:
            return int(response.status or 0), response.read().decode("utf-8", "replace")
    except Exception as exc:
        logger.debug("geo probe failed (%s): urllib:%s", url, exc)
    logger.debug("geo probe failed (%s): %s", url, last)
    return 0, ""


def probe_exit_geo(
    proxy: str | None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    endpoints: tuple[tuple[str, str], ...] | None = None,
    need_timezone: bool = False,
) -> ProxyGeo:
    """Measure a proxy's exit geo. Never raises; returns empty geo on miss.

    Walks :data:`GEO_ENDPOINTS` in order and returns the first usable answer.
    When ``need_timezone`` is set a country-only answer (e.g. from the
    ``cdn-cgi/trace`` endpoint) is kept as a fallback but the walk continues
    looking for a richer document.
    """
    if not proxy:
        return _EMPTY
    best = _EMPTY
    for url, kind in (endpoints or GEO_ENDPOINTS):
        try:
            status, body = _http_get(url, proxy, timeout)
        except Exception:
            continue
        if status != 200 or not body:
            continue
        geo = _parse_trace(body) if kind == "trace" else _parse_json(body)
        if not geo.known:
            continue
        if geo.timezone or not need_timezone:
            return geo
        if best.country == "":
            best = geo
    return best


def _parse_json(body: str) -> ProxyGeo:
    try:
        return normalize_geo_response(json.loads(body))
    except (ValueError, TypeError):
        return _EMPTY


@dataclass
class _CacheEntry:
    geo: ProxyGeo
    expires: float


@dataclass
class GeoResolver:
    """Resolve a proxy's exit country/timezone with one shared cache.

    Usage::

        resolver = shared_geo_resolver()
        geo = resolver.resolve(proxy, hint=infer_region(proxy))
        geo.country  # "JP"
    """

    timeout: float = DEFAULT_TIMEOUT
    enabled: bool = True
    verify_hint: bool = False
    positive_ttl: float = POSITIVE_TTL
    negative_ttl: float = NEGATIVE_TTL
    endpoints: tuple[tuple[str, str], ...] = GEO_ENDPOINTS
    # Injection seam: a caller (or a test) can swap the network probe without
    # losing the shared cache.  Signature is ``(proxy, *, timeout, endpoints,
    # need_timezone) -> ProxyGeo``.
    probe: Callable[..., "ProxyGeo"] | None = None
    _cache: dict[str, _CacheEntry] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def resolve(
        self,
        proxy: str | None,
        *,
        hint: str = "",
        verify_hint: bool | None = None,
        need_timezone: bool = False,
        timeout: float | None = None,
        probe: Callable[..., "ProxyGeo"] | None = None,
    ) -> ProxyGeo:
        """Return the exit geo for ``proxy``.

        ``hint`` is a country already derived from the credential template; it
        short-circuits the probe unless ``verify_hint`` is on. An empty hint
        always probes.
        """
        if not self.enabled or not proxy:
            return _EMPTY
        key = self._cache_key(proxy)
        want_verify = self.verify_hint if verify_hint is None else verify_hint
        code = str(hint or "").strip().upper()

        cached = self._read(key)
        # A country-only answer does not satisfy a caller that needs a clock.
        # Without this check the first (country-only) writer wins the cache and
        # every later timezone request is served an answer with no timezone —
        # which is how a browser ends up with the right country and the wrong
        # local time.
        if cached is not None and (cached.timezone or not need_timezone):
            return cached.with_source("cache")

        geo = _EMPTY
        if want_verify or not code or need_timezone:
            geo = self._probe(key, timeout, probe, need_timezone)
            if geo.known and code and geo.country and geo.country != code:
                logger.warning(
                    "proxy exit %s: template hint %s disagrees with measured %s "
                    "(measured wins)",
                    key, code, geo.country,
                )
        if not geo.known and cached is not None and cached.known:
            # A fresh miss must not erase an answer we already had.
            geo = cached
        if not geo.known and code:
            geo = ProxyGeo(country=code, source="hint")
        if cached is not None and cached.known:
            # Re-probing to find a timezone must not drop fields the previous
            # answer supplied; merge field-by-field.
            geo = ProxyGeo(
                ip=geo.ip or cached.ip,
                country=geo.country or cached.country,
                region=geo.region or cached.region,
                city=geo.city or cached.city,
                timezone=geo.timezone or cached.timezone,
                org=geo.org or cached.org,
                source=geo.source,
            )
        self._write(key, geo)
        return geo

    def _probe(
        self,
        key: str,
        timeout: float | None,
        probe: Callable[..., "ProxyGeo"] | None,
        need_timezone: bool,
    ) -> ProxyGeo:
        """Run the probe callable, swallowing *anything* it raises.

        The built-in probe never raises, but an injected one (a test seam, or a
        caller's legacy function) may. Geo is advisory: a raising probe must
        degrade to "unknown", never break registration.
        """
        probe_fn = probe or self.probe or probe_exit_geo
        try:
            return probe_fn(
                key,
                timeout=self.timeout if timeout is None else float(timeout),
                endpoints=self.endpoints,
                need_timezone=need_timezone,
            )
        except Exception as exc:
            logger.debug("geo probe raised for %s: %s", key, exc)
            return _EMPTY

    def cached(self, proxy: str | None) -> "ProxyGeo | None":
        """Return a cached geo without ever touching the network.

        Launch paths (a browser session being created) must not block on a geo
        probe, but they still want an answer if an upstream stage — the payment
        preflight, the orchestrator — already measured this egress.
        """
        if not proxy:
            return None
        return self._read(self._cache_key(proxy))

    def remember(self, proxy: str | None, geo: "ProxyGeo") -> None:
        """Store an externally measured geo in the shared cache.

        This is the fix for the discarded measurement: the payment preflight
        already probes the real egress country to gate checkout, and used to
        throw that answer away — the fingerprint pool then guessed the country
        again from the credential template and could disagree with it. Writing
        it through the shared cache is what makes the two agree.

        Merges field-by-field so a later, thinner measurement (country-only)
        never blanks a richer one (country + timezone + org).
        """
        if not proxy or not isinstance(geo, ProxyGeo) or not geo.known:
            return
        key = self._cache_key(proxy)
        current = self._read(key)
        merged = ProxyGeo(
            ip=(current.ip if current else "") or geo.ip,
            country=(current.country if current else "") or geo.country,
            region=(current.region if current else "") or geo.region,
            city=(current.city if current else "") or geo.city,
            timezone=(current.timezone if current else "") or geo.timezone,
            org=(current.org if current else "") or geo.org,
            source="probe",
        )
        self._write(key, merged)

    def _cache_key(self, proxy: str) -> str:
        """Normalize to the canonical URL so all proxy spellings share a cache.

        The old callers cached on the *normalized* URL too, but each kept its
        own dict — one for the browser path, one inside the payment proxy
        state. Sharing the key shape is what lets them share the answer.
        """
        try:
            from ..phone_proxy import normalize_proxy_url
        except Exception:
            return str(proxy or "")
        return normalize_proxy_url(proxy) or str(proxy or "")

    def _read(self, key: str) -> ProxyGeo | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires <= time.time():
                self._cache.pop(key, None)
                return None
            return entry.geo

    def _write(self, key: str, geo: ProxyGeo) -> None:
        ttl = self.positive_ttl if geo.known else self.negative_ttl
        with self._lock:
            self._cache[key] = _CacheEntry(geo=geo, expires=time.time() + ttl)

    def invalidate(self, proxy: str | None = None) -> None:
        with self._lock:
            if proxy is None:
                self._cache.clear()
            else:
                self._cache.pop(self._cache_key(proxy), None)


_SHARED: GeoResolver | None = None
_SHARED_LOCK = threading.Lock()


def shared_geo_resolver() -> GeoResolver:
    """Process-wide resolver so every caller shares one geo cache."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = GeoResolver()
        return _SHARED


def reset_shared_geo_resolver() -> None:
    """Drop the shared resolver (tests only)."""
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = None


def remember_proxy_geo(proxy: str | None, geo: ProxyGeo) -> None:
    """Publish an externally measured geo to the process-wide cache."""
    shared_geo_resolver().remember(proxy, geo)


def resolve_proxy_geo(
    proxy: str | None,
    *,
    hint: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    enabled: bool = True,
    need_timezone: bool = False,
) -> ProxyGeo:
    """Convenience wrapper over :meth:`GeoResolver.resolve`.

    Creates a throwaway resolver when the caller needs non-default settings, so
    the shared cache is only used for the default configuration.
    """
    if not enabled or not proxy:
        return _EMPTY
    shared = shared_geo_resolver()
    if timeout == shared.timeout:
        return shared.resolve(
            proxy, hint=hint, need_timezone=need_timezone
        )
    return GeoResolver(timeout=timeout).resolve(
        proxy, hint=hint, need_timezone=need_timezone
    )


__all__ = [
    "CHROME_UA",
    "DEFAULT_TIMEOUT",
    "GEO_ENDPOINTS",
    "GeoResolver",
    "NEGATIVE_TTL",
    "POSITIVE_TTL",
    "ProxyGeo",
    "normalize_geo_response",
    "probe_exit_geo",
    "remember_proxy_geo",
    "reset_shared_geo_resolver",
    "resolve_proxy_geo",
    "shared_geo_resolver",
]
