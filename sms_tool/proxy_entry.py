"""Unified, single-authority proxy entry parser and pool loader.

Current project already parses the bare ``host:port:user:pass`` form in two
places (``phone_proxy.normalize_proxy_url`` and ``proxy_pool.UpstreamProxy.
from_url``) with inconsistent behaviour.  This module provides one canonical,
full-format parser that covers:

- ``scheme://user:pass@host:port`` and ``scheme://host:port``
- ``user:pass@host:port`` (scheme defaults to ``default_scheme``)
- bare ``host:port:user:pass`` (provider UI form)
- bare ``host:port`` (no auth)
- IPv6 literals: ``[::1]:port``, ``[::1]:port:user:pass``, ``scheme://[::1]:port``
- socks5 / socks5h scheme aliasing (``socks`` -> ``socks5``)
- a config/env-driven pool loader and a random-or-index chooser (aligned with
  the external paypal-agreement-protocol ``proxy.py`` contract).

It is dependency-free beyond the standard library and reuses the existing
``phone_proxy.redact_proxy_url`` for masked display where available.
"""

from __future__ import annotations

import os
import random
import re
import string
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# Scheme aliases -> canonical scheme.
_SCHEME_ALIASES = {
    "socks": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4a",
    "socks5": "socks5",
    "socks5h": "socks5h",
    "http": "http",
    "https": "https",
}
# socks-family proxies default to port 1080 when the port is missing; http(s)
# proxies cannot be guessed and stay as ``None`` (caller decides).
_SOCKS_DEFAULT_PORT = 1080
_HTTP_SCHEMES = {"http", "https"}
_SOCKS_SCHEMES = {"socks4", "socks4a", "socks5", "socks5h"}


def _mask_credentials(value: str) -> str:
    """Log-safe raw value with any inline credentials replaced by ``***``."""
    text = str(value or "")
    # scheme://user:pass@host -> scheme://***:***@host
    text = re.sub(
        r"(?i)\b((?:https?|socks5h?|socks4a?)://)[^/@\s]+@",
        r"\1***:***@",
        text,
    )
    # bare host:port:user:pass (4 fields) -> host:port:***:***
    text = re.sub(
        r"(?i)\b([a-z0-9.\-\[\]]+:\d+):[^\s'\"]+:[^\s'\"]+",
        r"\1:***:***",
        text,
    )
    return text


@dataclass(frozen=True)
class ProxyEntry:
    """A parsed, normalized proxy endpoint."""

    host: str
    port: int
    username: str
    password: str
    scheme: str = "http"          # canonical scheme
    raw: str = ""                 # original input (masked in ``masked``)

    @classmethod
    def parse(cls, raw: Any, default_scheme: str = "http") -> "ProxyEntry | None":
        """Parse any supported proxy form into a :class:`ProxyEntry`.

        Returns ``None`` when the input is empty or structurally invalid (e.g.
        an http/https proxy without a port, or an unknown scheme).
        """
        entry = parse_proxy(raw, default_scheme=default_scheme)
        return entry

    @property
    def url(self) -> str:
        """Standard ``scheme://user:pass@host:port`` form."""
        return proxy_to_url(self)

    @property
    def masked(self) -> str:
        """Log-safe representation without credentials."""
        return _mask_credentials(self.raw or self.url)

    @property
    def is_socks(self) -> bool:
        return self.scheme in _SOCKS_SCHEMES

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
            "label": self.masked,
        }

    def __repr__(self) -> str:  # avoid leaking credentials in repr/logs
        return f"ProxyEntry(scheme={self.scheme}, host={self.host}, port={self.port})"


def _normalize_scheme(scheme: str) -> str | None:
    key = str(scheme or "").strip().lower()
    return _SCHEME_ALIASES.get(key)


def _normalize_host(host: str) -> str | None:
    host = str(host or "").strip()
    if not host:
        return None
    # strip IPv6 brackets for the bare form, keep the canonical host w/o brackets
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def parse_proxy(raw: Any, default_scheme: str = "http") -> ProxyEntry | None:
    """Parse ``raw`` into a :class:`ProxyEntry`.

    Supported forms (see module docstring).  ``default_scheme`` applies only
    when the input has no scheme; it is itself normalized (``socks`` -> socks5).
    """
    value = str(raw or "").strip()
    if not value:
        return None
    # The desktop field is commonly filled with Chinese punctuation; accept
    # a full-width colon for the provider's four-part form as well.
    value = value.replace("：", ":")

    default = _normalize_scheme(default_scheme) or "http"
    scheme: str = default
    rest = value
    if "://" in value:
        scheme_raw, rest = value.split("://", 1)
        normalized = _normalize_scheme(scheme_raw)
        if normalized is None:
            return None
        scheme = normalized

    # At this point ``rest`` is everything after any ``scheme://`` prefix.
    # It may still carry an explicit ``user:pass@host[:port]`` (from a value
    # like ``user:pass@host:port`` where scheme was implied) or the bare
    # ``host:port:user:pass`` provider form.

    # 1) explicit userinfo: user:pass@host[:port] or user@host[:port]
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        if ":" in userinfo:
            username, password = userinfo.split(":", 1)
        else:
            username, password = userinfo, ""
        host, port = _split_host_port(hostport, scheme)
        host = _normalize_host(host)
        if not host:
            return None
        resolved_port = _resolve_port(port, scheme)
        if resolved_port is None:
            return None
        return ProxyEntry(
            host=host,
            port=resolved_port,
            username=unquote(username),
            password=unquote(password),
            scheme=scheme,
            raw=value,
        )

    # 2) bare host:port:user:pass (4 fields) — provider UI form
    parts = rest.split(":")
    if len(parts) == 4:
        host_candidate, port_candidate, user_candidate, pass_candidate = parts
        if _is_port(port_candidate):
            host = _normalize_host(host_candidate)
            if not host:
                return None
            return ProxyEntry(
                host=host,
                port=int(port_candidate),
                username=unquote(user_candidate),
                password=unquote(pass_candidate),
                scheme=scheme,
                raw=value,
            )
        # else fall through to host:port:password-with-colons?  Treat the
        # extra fields conservatively: not a valid 4-segment form.

    # 3) IPv6 bare: [::1]:port or [::1]:port:user:pass
    if rest.startswith("["):
        host, port = _split_host_port(rest, scheme)
        host = _normalize_host(host)
        if not host:
            return None
        if port is None and len(rest.split(":")) > 2:
            # "[::1]:port:user:pass" — userinfo embedded after the bracketed host
            inner = rest[1:].split("]:", 1)
            if len(inner) == 2:
                port_s, creds = inner[1].split(":", 1)
                if _is_port(port_s):
                    userinfo = creds.split(":", 1)
                    username = unquote(userinfo[0])
                    password = unquote(userinfo[1]) if len(userinfo) > 1 else ""
                    return ProxyEntry(
                        host=host,
                        port=int(port_s),
                        username=username,
                        password=password,
                        scheme=scheme,
                        raw=value,
                    )
        resolved_port = _resolve_port(port, scheme)
        if resolved_port is None:
            return None
        return ProxyEntry(
            host=host,
            port=resolved_port,
            username="",
            password="",
            scheme=scheme,
            raw=value,
        )

    # 4) plain host[:port]
    host, port = _split_host_port(rest, scheme)
    host = _normalize_host(host)
    if not host:
        return None
    resolved_port = _resolve_port(port, scheme)
    if resolved_port is None:
        # http/https without a port is ambiguous -> refuse (caller must give one)
        return None
    return ProxyEntry(
        host=host,
        port=resolved_port,
        username="",
        password="",
        scheme=scheme,
        raw=value,
    )


def _split_host_port(hostport: str, scheme: str) -> tuple[str, int | None]:
    """Split ``host[:port]``; IPv6-aware.  Returns (host, port|None)."""
    hostport = str(hostport or "").strip()
    if hostport.startswith("["):
        # bracketed IPv6: [::1] or [::1]:port
        end = hostport.find("]")
        if end == -1:
            return "", None
        host = hostport[1:end]
        tail = hostport[end + 1 :]
        if tail.startswith(":"):
            port_s = tail[1:]
            return host, int(port_s) if _is_port(port_s) else None
        return host, None
    # unbracketed: could be bare IPv6 without port — not supported as a proxy
    if hostport.count(":") > 1:
        # multiple colons with no brackets: ambiguous, refuse host
        return "", None
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        return host, int(port_s) if _is_port(port_s) else None
    return hostport, None


def _is_port(value: Any) -> bool:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return False
    return 1 <= port <= 65535


def _resolve_port(port: int | None, scheme: str) -> int | None:
    """Final port for an entry, or ``None`` when it cannot be determined.

    An explicit port always wins.  A missing port is only defaulted for the
    socks family (1080); http/https proxy ports cannot be guessed, so a missing
    one makes the entry invalid regardless of which parse branch reached here.
    """
    if port is not None:
        return port
    if scheme in _SOCKS_SCHEMES:
        return _SOCKS_DEFAULT_PORT
    return None


def proxy_to_url(entry: ProxyEntry) -> str:
    """Render a :class:`ProxyEntry` as ``scheme://user:pass@host:port``."""
    host = entry.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if not entry.port else f"{host}:{entry.port}"
    if entry.username:
        auth = quote(entry.username, safe="-._~")
        if entry.password:
            auth += ":" + quote(entry.password, safe="-._~")
        netloc = f"{auth}@{netloc}"
    return urlunsplit((entry.scheme, netloc, "", "", ""))


# ──────────────────── Session / region rotation (single authority) ───────────
#
# Sticky-session id refresh and exit-region retargeting for dynamic upstream
# proxies used to live in both ``phone_proxy`` (registration/phone) and
# ``paypal_proxy`` (payment) with divergent regexes, TTL units, and duplicate
# ``_rebuild_proxy_url`` copies.  They now delegate to the functions below so the
# same provider proxy is rotated identically regardless of the calling flow.
#
# Supported provider templates:
#   * Cliproxy / Novproxy — username carries ``region-XX`` and ``-sid-<id>-t-<n>``.
#   * Kookeey / ippeak    — password shaped ``BASE-CC-SESSION-TTL`` (TTL like
#     ``5m`` / ``30s`` / ``1h`` / ``1d``); the TTL unit set is the superset of
#     both historical implementations so seconds/days sessions rotate too.

_USER_REGION_RE = re.compile(r"(^|-)region-[A-Za-z]{2}(?=-|$)")
_USER_SID_RE = re.compile(r"(?<=-sid-)[A-Za-z0-9]+(?=-t-|-|$)")
_KOOKEEY_PW_RE = re.compile(
    r"^(?P<base>.+?)-(?P<cc>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])$"
)
_INFER_USER_REGION_RE = re.compile(r"region-([A-Za-z]{2})(?=$|[-_:])")
_INFER_KOOKEEY_PW_RE = re.compile(r"^.+?-([A-Za-z]{2})-[A-Za-z0-9]+-\d+[smhd]$")
_INFER_USER_TAIL_RE = re.compile(r"-([A-Za-z]{2})(?:-[A-Za-z0-9]+)?$")
_IPWO_CUSTOM_ZONE_RE = re.compile(
    r"(?P<prefix>(?:^|[_-])custom[_-]zone[_-])(?P<cc>[A-Za-z]{2})(?=$|[_-])",
    re.IGNORECASE,
)


def _random_session_id(length: int, *, digits_only: bool = False) -> str:
    """Random replacement id preserving the original length (min 1)."""
    alphabet = string.digits if digits_only else (string.ascii_letters + string.digits)
    size = max(1, int(length or 8))
    return "".join(random.choice(alphabet) for _ in range(size))


def rebuild_proxy_credentials(parsed: Any, username: str, password: str) -> str:
    """Rebuild a proxy URL from a ``urlsplit`` result with new user/pass.

    Preserves scheme, host (IPv6-safe), port, path, query, and fragment.  Auth is
    omitted entirely when ``username`` is empty rather than emitting ``:@host``.
    """
    host = parsed.hostname or ""
    if not host:
        return parsed.geturl()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if username:
        auth = quote(username, safe="-._~")
        if password:
            auth += ":" + quote(password, safe="-._~")
        host = f"{auth}@{host}"
    return urlunsplit((parsed.scheme or "http", host, parsed.path, parsed.query, parsed.fragment))


def retarget_region(proxy: str, iso_code: str) -> str:
    """Change only the exit region/country, preserving the sticky session id.

    Handles Cliproxy ``region-XX``, IPWO ``custom_zone_XX`` usernames, and the
    Kookeey ``BASE-CC-SESSION-TTL`` password shape. Returns the input unchanged
    when no known template matches.
    """
    value = str(proxy or "")
    iso = str(iso_code or "").strip().upper()
    if not value or not iso:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username and not password:
        return value
    changed = False
    new_user, count = _USER_REGION_RE.subn(lambda m: f"{m.group(1)}region-{iso}", username, count=1)
    if count:
        username = new_user
        changed = True
    new_user, count = _IPWO_CUSTOM_ZONE_RE.subn(
        lambda match: f"{match.group('prefix')}{iso}", username, count=1
    )
    if count:
        username = new_user
        changed = True
    match = _KOOKEEY_PW_RE.match(password)
    if match:
        password = f"{match.group('base')}-{iso}-{match.group('sid')}-{match.group('ttl')}"
        changed = True
    return rebuild_proxy_credentials(parsed, username, password) if changed else value


def rotate_session(proxy: str, iso_code: str = "") -> str:
    """Refresh the sticky session id, optionally retargeting the exit region.

    With an empty ``iso_code`` only the session id changes (region preserved);
    with a country the region is retargeted in the same pass.  Numeric-only
    Kookeey session ids stay numeric and keep their original length.
    """
    value = str(proxy or "")
    if not value:
        return value
    iso = str(iso_code or "").strip().upper()
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username and not password:
        return value
    changed = False
    if iso:
        new_user, count = _USER_REGION_RE.subn(lambda m: f"{m.group(1)}region-{iso}", username, count=1)
        if count:
            username = new_user
            changed = True
        new_user, count = _IPWO_CUSTOM_ZONE_RE.subn(
            lambda match: f"{match.group('prefix')}{iso}", username, count=1
        )
        if count:
            username = new_user
            changed = True
    new_user, count = _USER_SID_RE.subn(lambda m: _random_session_id(len(m.group(0))), username, count=1)
    if count:
        username = new_user
        changed = True
    match = _KOOKEEY_PW_RE.match(password)
    if match:
        country = iso or match.group("cc").upper()
        sid = _random_session_id(len(match.group("sid")), digits_only=match.group("sid").isdigit())
        password = f"{match.group('base')}-{country}-{sid}-{match.group('ttl')}"
        changed = True
    return rebuild_proxy_credentials(parsed, username, password) if changed else value


def infer_region(proxy: str) -> str:
    """Best-effort exit-country inference from a proxy's credential template."""
    value = str(proxy or "")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except Exception:
        return ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    match = _INFER_USER_REGION_RE.search(username)
    if match:
        return match.group(1).upper()
    match = _IPWO_CUSTOM_ZONE_RE.search(username)
    if match:
        return match.group("cc").upper()
    match = _INFER_KOOKEEY_PW_RE.match(password)
    if match:
        return match.group(1).upper()
    match = _INFER_USER_TAIL_RE.search(username)
    return match.group(1).upper() if match else ""


def load_proxy_pool(
    config: Mapping[str, Any] | None = None,
    *,
    env_prefix: str = "PROXY",
    keys: Iterable[str] = ("proxy_pool", "proxies"),
) -> list[ProxyEntry]:
    """Load a proxy pool from (in priority order) environment variables then
    ``config``.

    Environment: ``<PREFIX>_POOL`` (comma / newline separated) and
    ``<PREFIX>_URL`` (single URL).  Config: entries under ``keys`` from a dict
    (``paypal`` section is the usual container).  Invalid entries are skipped.
    """
    prefix = str(env_prefix or "PROXY").strip().upper()
    raw_items: list[str] = []

    for var in (f"{prefix}_POOL", f"{prefix}_POOL_URL"):
        value = (os.environ.get(var) or "").strip()
        if value:
            raw_items.extend(_split_pool_text(value))

    env_single = (os.environ.get(f"{prefix}_URL") or "").strip()
    if env_single:
        raw_items.append(env_single)

    if isinstance(config, Mapping):
        cfg = dict(config)
        # also allow nested containers like {"paypal": {"proxy_pool": [...]}}
        nested = {}
        for nested_key in ("paypal", "protocol_payments", "proxy"):
            if isinstance(cfg.get(nested_key), Mapping):
                nested.update(dict(cfg[nested_key]))
        search = {**nested, **cfg}
        for key in keys:
            value = search.get(key)
            if isinstance(value, str):
                raw_items.extend(_split_pool_text(value))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str):
                        raw_items.extend(_split_pool_text(item))

    entries: list[ProxyEntry] = []
    seen: set[tuple[str, str, int, str]] = set()
    for raw in raw_items:
        entry = parse_proxy(raw)
        if entry is None:
            continue
        key = (entry.scheme, entry.host, entry.port, entry.username)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def _split_pool_text(text: str) -> list[str]:
    """Split a pool string on commas and/or newlines, stripping empties."""
    result: list[str] = []
    for part in str(text or "").replace("\n", ",").split(","):
        part = part.strip()
        if part:
            result.append(part)
    return result


def choose_proxy_entry(
    pool: Iterable[ProxyEntry] | None,
    index: int | None = None,
) -> ProxyEntry | None:
    """Pick a proxy entry from ``pool`` — random, or by ``index`` (clamped)."""
    entries = list(pool or [])
    if not entries:
        return None
    if index is not None:
        idx = int(index) % len(entries)
        return entries[idx]
    return random.choice(entries)


def resolve_proxy_value(
    raw: Any,
    default_scheme: str = "http",
) -> str:
    """Resolve a single ``--proxy`` CLI value to one canonical proxy URL.

    The frontend batch/extract modules pass a single ``--proxy`` string that may
    hold any of:

    - one URL-form proxy (``scheme://user:pass@host:port``)
    - one bare credential proxy (``host:port:user:pass``)
    - a proxy pool (comma / newline separated) — the first usable candidate is
      picked and normalized into a canonical endpoint.

    Returns ``""`` when nothing usable is found.  The returned value is always
    canonical URL form, so callers (``payment_stage_args`` / batch routing) can
    rely on a single-URL string downstream.
    """
    value = str(raw or "").strip()
    if not value:
        return ""

    # A pool string usually contains a comma or a newline.  Split, then pick the
    # first entry that parses into a canonical endpoint.  A single value with no
    # separators also passes through here so every form is normalized uniformly.
    parts = _split_pool_text(value)
    for part in parts:
        entry = parse_proxy(part, default_scheme=default_scheme)
        if entry is not None:
            return entry.url
    return ""


def parse_proxy_list(raw: Iterable[Any], default_scheme: str = "http") -> list[ProxyEntry]:
    """Parse an iterable of proxy strings into entries (dedup by endpoint)."""
    entries: list[ProxyEntry] = []
    seen: set[tuple[str, str, int, str]] = set()
    for item in raw or []:
        entry = parse_proxy(item, default_scheme=default_scheme)
        if entry is None:
            continue
        key = (entry.scheme, entry.host, entry.port, entry.username)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def build_proxy_config(
    enabled: bool | None = None,
    index: int | None = None,
    config: Mapping[str, Any] | None = None,
    *,
    env_prefix: str = "PROXY",
) -> dict[str, Any]:
    """Build a proxy config dict aligned with the external proxy.py contract.

    Returns ``{"enabled": bool, "entry": ProxyEntry|None, "proxy_url": str}``.
    ``enabled`` defaults to the ``<PREFIX>_ENABLED`` env var (or False).
    """
    prefix = str(env_prefix or "PROXY").strip().upper()
    if enabled is None:
        env_enabled = (os.environ.get(f"{prefix}_ENABLED") or "").strip().lower()
        enabled = env_enabled in {"1", "true", "yes", "on"}
    pool = load_proxy_pool(config, env_prefix=env_prefix)
    entry = choose_proxy_entry(pool, index=index) if enabled and pool else None
    return {
        "enabled": bool(enabled),
        "entry": entry,
        "proxy_url": entry.url if entry else "",
    }
