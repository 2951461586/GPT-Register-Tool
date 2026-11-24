"""Protocol HTTP backend seam: ``curl_cffi`` (default) or ``wreq``.

Why a seam exists at all
------------------------
The registration lane's TLS/HTTP2 stack is part of its fingerprint.  Two
independent implementations are worth having because they fail differently: a
JA3/Akamai blocklist or a libcurl-specific edge rule that stops ``curl_cffi``
does not automatically stop a Rust stack (``wreq``), and vice versa.  The
reference project exposes exactly this choice (``PROTOCOL_HTTP_CLIENT`` =
``curl_cffi`` | ``wreq``) and this module is the port of that idea onto this
repository's session construction.

What it deliberately does **not** do
------------------------------------
Default to ``wreq``, or fall back to it silently.  ``curl_cffi`` is the stack
every measured HAR, every fingerprint literal and the whole ``tls_fingerprint``
override were built against; a run that silently swapped its transport would
still look identical in the logs while answering a different ClientHello.  So:

* the default is ``curl_cffi`` and stays there until a controlled live A/B says
  otherwise (the same convention ``sentinel/runner.py`` uses for the Node
  worker);
* ``wreq`` selected but not installed / not able to run the chosen profile is a
  **fail-closed** error with a stable token, never a downgrade;
* the per-account extension permutation is curl-only (it is a libcurl option).
  A ``wreq`` run therefore gets the preset's own order -- the module says so in
  the fingerprint summary instead of claiming a rotation it did not perform.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Mapping
from datetime import timedelta
from http.client import responses
from typing import Any

from ..kernel.error_classification import INTERNAL_EXCEPTION_TYPES

_LOGGER = logging.getLogger(__name__)

BACKENDS = ("curl_cffi", "wreq")
DEFAULT_BACKEND = "curl_cffi"
#: Env name kept identical to the reference project's switch so an operator's
#: muscle memory (and its docs) transfers.
BACKEND_ENV = "PROTOCOL_HTTP_CLIENT"
#: Config key inside ``email_registration``.
BACKEND_KEY = "protocol_http_backend"
#: ``wreq`` is Rust; its wheels start at CPython 3.11.
MIN_PYTHON = (3, 11)


class ProtocolBackendError(RuntimeError):
    """A backend was selected that cannot serve this run."""


def _section() -> Mapping:
    """The live ``email_registration`` section (frozen mapping included)."""
    try:
        from ..config import CFG

        raw_get = getattr(CFG, "raw_get", None)
        raw: object = raw_get("email_registration") if callable(raw_get) else CFG.get("email_registration")
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        return {}
    return raw if isinstance(raw, Mapping) else {}


def configured_backend() -> str:
    """``curl_cffi`` or ``wreq``; env wins over config.

    An unknown value raises instead of falling back: a typo in a shard must not
    silently keep the default stack while the operator believes otherwise.
    """
    raw = str(os.environ.get(BACKEND_ENV) or "").strip().lower()
    if not raw:
        raw = str(_section().get(BACKEND_KEY) or "").strip().lower()
    if not raw:
        return DEFAULT_BACKEND
    if raw not in BACKENDS:
        raise ProtocolBackendError(f"protocol_http_backend_unknown:{raw}")
    return raw


def wreq_module() -> Any:
    """The imported ``wreq`` module, or ``None`` when it is not installed."""
    if importlib.util.find_spec("wreq") is None:
        return None
    try:
        import wreq
    except ImportError:  # pragma: no cover - find_spec said otherwise
        return None
    return wreq


def wreq_client_class() -> Any:
    """``wreq.blocking.Client`` -- **not** ``wreq.Client``.

    ``wreq.Client`` is the asyncio client: its ``request()`` returns a
    coroutine, so the obvious import produces
    ``TypeError: 'wreq.Coroutine' object does not support the context manager
    protocol`` at the first real request.  Measured on wreq 0.13.0
    (2026-11-19) against a local HTTP server.
    """
    module = wreq_module()
    if module is None:
        return None
    try:
        from wreq import blocking
    except ImportError:  # pragma: no cover - find_spec said the package exists
        return None
    return getattr(blocking, "Client", None)


_WREQ_PROFILE_MAP_CACHE: tuple[Any, dict[str, str]] = (None, {})
_CURL_BROWSER_TYPE_CACHE: tuple[Any, frozenset[str]] = (None, frozenset())


def wreq_profile_map() -> dict[str, str]:
    """``{lowercased profile name: attribute name}`` for the installed ``wreq``."""
    global _WREQ_PROFILE_MAP_CACHE
    module = wreq_module()
    if module is None:
        return {}
    profile_cls = getattr(module, "Profile", None)
    if profile_cls is None:
        return {}
    cached_cls, cached_map = _WREQ_PROFILE_MAP_CACHE
    if cached_cls is profile_cls:
        return dict(cached_map)
    names = [name for name in dir(profile_cls) if not name.startswith("_")]
    mapping = {name.lower(): name for name in names}
    _WREQ_PROFILE_MAP_CACHE = (profile_cls, mapping)
    return dict(mapping)


def _safari_candidates(token: str) -> tuple[str, ...]:
    """Map our ``safari`` tokens (``17_0``, ``17_2_ios``, ``184``, ``260``) to exact ``wreq`` names.

    No version or platform aliasing:
    - ``*_ios`` maps only to ``safariios*`` (never desktop ``safari*``).
    - A non-zero minor (e.g. ``184`` -> ``18_4``) matches only ``safari18_4``
      (never falls back to ``safari18``).
    - A zero minor (``17_0``, ``18_0``, ``260``) checks both ``{prefix}{major}_0``
      and ``{prefix}{major}`` because ``wreq`` spells 17.0 as ``Safari17_0``
      and 18.0/26.0 as ``Safari18``/``Safari26``.
    """
    is_ios = token.endswith("_ios")
    core = token.removesuffix("_ios") if is_ios else token
    prefix = "safariios" if is_ios else "safari"
    if "_" in core:
        major, _, minor = core.partition("_")
    elif len(core) > 2:
        major, minor = core[:2], core[2:]
    else:
        major, minor = core, ""
    if not major:
        return ()
    if minor and minor != "0":
        return (f"{prefix}{major}_{minor}",)
    if minor == "0":
        return (f"{prefix}{major}_0", f"{prefix}{major}")
    return (f"{prefix}{major}",)


def _profile_candidates(impersonate: str) -> tuple[str, ...]:
    name = str(impersonate or "").strip().lower()
    if not name:
        return ()
    if name.startswith("safari"):
        return _safari_candidates(name.removeprefix("safari"))
    return (name,)


def wreq_profile(impersonate: str) -> Any:
    """The ``wreq.Profile`` member that matches ``impersonate``, or ``None``.

    No version aliasing: a profile our table calls ``firefox155`` must not be
    served by wreq's ``Firefox152`` preset, because the UA would then claim a
    version whose ClientHello is three releases older.  ``None`` is the honest
    answer and the caller drops or rejects the profile.
    """
    module = wreq_module()
    mapping = wreq_profile_map()
    if module is None or not mapping:
        return None
    for candidate in _profile_candidates(impersonate):
        attribute = mapping.get(candidate)
        if attribute:
            return getattr(module.Profile, attribute, None)
    return None


def supported_impersonations() -> set[str]:
    """Profile names the *active* backend can actually run.

    ``auth_headers`` filters its table through this, so a backend that cannot
    serve a preset drops it from selection instead of failing later in the
    preflight (or, worse, serving a different ClientHello under the same UA).
    """
    from .auth_headers import AUTH_FINGERPRINT_PROFILES

    names = set(AUTH_FINGERPRINT_PROFILES)
    try:
        backend = configured_backend()
    except ProtocolBackendError:
        return set()
    if backend == "wreq":
        if wreq_module() is None:
            return set()
        return {name for name in names if wreq_profile(name) is not None}
    try:
        from curl_cffi.requests.impersonate import BrowserType

        global _CURL_BROWSER_TYPE_CACHE
        cached_enum, cached_values = _CURL_BROWSER_TYPE_CACHE
        if cached_enum is BrowserType:
            installed = cached_values
        else:
            installed = frozenset(item.value for item in BrowserType)
            _CURL_BROWSER_TYPE_CACHE = (BrowserType, installed)
    except INTERNAL_EXCEPTION_TYPES:
        return names
    return {name for name in names if name in installed}


def capabilities() -> dict[str, object]:
    """One flat report for ``doctor``/logs; never raises."""
    try:
        backend = configured_backend()
    except ProtocolBackendError as exc:
        return {"backend": "", "available": False, "reason": str(exc), "profiles": 0}
    profiles = len(supported_impersonations())
    if backend == "wreq":
        installed = wreq_module() is not None
        return {
            "backend": backend,
            "available": installed and profiles > 0,
            "reason": "" if installed else "wreq_not_installed",
            "profiles": profiles,
        }
    return {"backend": backend, "available": profiles > 0, "reason": "", "profiles": profiles}


def _new_curl_session(proxy: str = "") -> Any:
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        session.trust_env = False
    return session


def new_session(*, proxy: str = "") -> Any:
    """Build the protocol session for the configured backend.

    ``curl_cffi`` keeps the historical shape (bare session + explicit proxies)
    so callers that install extra options afterwards keep working; ``wreq``
    returns the adapter below, which mimics the subset of the ``requests``
    surface this lane uses.
    """
    backend = configured_backend()
    if backend == "wreq":
        return WreqSession(proxy=proxy)
    return _new_curl_session(proxy=proxy)


class WreqCookies:
    """``session.cookies`` facade over a native ``wreq.Jar``.

    ``jar`` is a *write-through* ``RequestsCookieJar``: reads are a snapshot, but
    ``jar.set_cookie(...)`` is mirrored into the native jar.  Without that,
    ``registration/checkpoint.py`` would restore a cookie into a Python object
    the transport never reads -- a restore that reports success and changes
    nothing.
    """

    def __init__(self, native: Any, module: Any) -> None:
        self.native = native
        self._wreq = module
        self._mirror: Any = None

    def _mirror_jar(self) -> Any:
        if self._mirror is None:
            self._mirror = _WriteThroughJar(self.native, self._wreq)
        return self._mirror

    def set(self, name: str, value: str, domain: str = "", path: str = "/", secure: bool = False) -> None:
        if not domain:
            raise ValueError("wreq Cookie 必须指定 domain")
        origin = f"{'https' if secure else 'http'}://{domain.lstrip('.')}{path}"
        self.native.add(self._wreq.Cookie(name, str(value), domain=domain, path=path, secure=secure), origin)

    def get(self, name: str, default: Any = None, domain: Any = None, path: Any = None) -> Any:
        return self.jar.get(name, default, domain=domain, path=path)

    @property
    def jar(self) -> Any:
        from requests.cookies import RequestsCookieJar

        jar = self._mirror_jar()
        # Refresh from the native jar without dropping the write-through hook:
        # a full replace would unlink set_cookie from the transport.
        jar.clear()
        for item in self._snapshot():
            RequestsCookieJar.set_cookie(jar, item)
        return jar

    def _snapshot(self) -> list[Any]:
        from requests.cookies import create_cookie

        items = []
        for cookie in self.native.get_all():
            item = create_cookie(
                cookie.name,
                cookie.value,
                domain=cookie.domain or "",
                path=cookie.path or "/",
                secure=bool(cookie.secure),
                rest={"HttpOnly": None} if getattr(cookie, "http_only", False) else {},
            )
            if not cookie.domain:
                item.domain_specified = False
            items.append(item)
        return items

    def clear(self) -> None:
        self.native.clear()


class _WriteThroughJar:
    """``RequestsCookieJar`` whose ``set_cookie`` also reaches the transport."""

    def __new__(cls, native: Any, module: Any) -> Any:
        from requests.cookies import RequestsCookieJar

        class _Jar(RequestsCookieJar):
            def set_cookie(self, cookie: Any) -> None:  # noqa: N802 - stdlib name
                RequestsCookieJar.set_cookie(self, cookie)
                _mirror_cookie(native, module, cookie)

        return _Jar()


def _mirror_cookie(native: Any, module: Any, cookie: Any) -> None:
    domain = str(getattr(cookie, "domain", "") or "")
    if not domain:
        # host-only cookies carry no domain; without an origin wreq cannot store
        # them, and inventing a host would place the cookie on the wrong site.
        return
    path = str(getattr(cookie, "path", "") or "/")
    origin = f"{'https' if getattr(cookie, 'secure', False) else 'http'}://{domain.lstrip('.')}{path}"
    try:
        native.add(
            module.Cookie(
                cookie.name,
                str(cookie.value),
                domain=domain,
                path=path,
                secure=bool(getattr(cookie, "secure", False)),
            ),
            origin,
        )
    except (AttributeError, TypeError, ValueError):
        # A mirror failure must not fail the caller: the cookie is still in the
        # Python jar, and the transport simply keeps its previous state.
        return


class WreqSession:
    """``requests``-shaped client over ``wreq``'s blocking client.

    Scope is exactly the surface this lane uses (``get``/``post``/``request``,
    ``cookies``, ``proxies``, ``close``).  Anything else raises ``TypeError``
    rather than being ignored -- a silently dropped ``verify=False`` or
    ``allow_redirects`` would change behaviour without a trace.
    """

    _sms_http_backend = "wreq"

    def __init__(self, *, proxy: str = "", impersonate: str = "") -> None:
        module = wreq_module()
        client_class = wreq_client_class()
        if module is None or client_class is None:
            raise ProtocolBackendError("protocol_http_backend_missing:wreq")
        from .auth_headers import auth_impersonate

        requested = impersonate or auth_impersonate()
        profile = wreq_profile(requested)
        if profile is None:
            raise ProtocolBackendError(f"protocol_http_backend_profile_unsupported:{requested}")
        self._module = module
        self.impersonate = requested
        self._jar = module.Jar()
        self._client: Any = client_class(
            emulation=profile,
            cookie_provider=self._jar,
            default_headers=False,
            no_proxy=not proxy,
        )
        self.cookies = WreqCookies(self._jar, module)
        _warn_platform_caveat(requested)
        self.proxies: dict[str, str] = {"http": proxy, "https": proxy} if proxy else {}
        self.timeout = 30.0

    # ── transport ────────────────────────────────────────────────────────────
    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        client = self._client
        if client is None:
            raise ProtocolBackendError("protocol_http_backend_closed")
        options, timeout = self._options(method, url, kwargs)
        try:
            with client.request(_method(self._module, method), url, **options) as raw:
                return _translate_response(self, raw, timeout)
        except self._module.TimeoutError as exc:
            from requests import exceptions

            raise exceptions.Timeout(str(exc)) from exc
        except self._module.RequestError as exc:
            from requests import exceptions

            raise exceptions.ConnectionError(str(exc)) from exc

    def _options(self, method: str, url: str, kwargs: dict[str, Any]) -> tuple[dict[str, Any], float]:
        module = self._module
        options: dict[str, Any] = {}
        if "impersonate" in kwargs:
            requested = str(kwargs.pop("impersonate") or "").strip().lower()
            if requested and requested != self.impersonate.lower():
                raise ProtocolBackendError(
                    f"protocol_http_backend_impersonate_mismatch:{requested}!={self.impersonate}"
                )
        if "headers" in kwargs:
            raw_headers = kwargs.pop("headers")
            if isinstance(raw_headers, Mapping):
                # curl_cffi uses ``headers["Content-Type"] = None`` to suppress libcurl's
                # default header on body-less POST; wreq sends no default headers so
                # dropping None-valued entries preserves the exact wire intent without
                # tripping wreq's strict string typing.
                options["headers"] = {str(k): str(v) for k, v in raw_headers.items() if v is not None}
            elif raw_headers is not None:
                options["headers"] = raw_headers
        for key in ("json", "cookies"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
        if "params" in kwargs:
            options["query"] = kwargs.pop("params")
        if "data" in kwargs:
            data = kwargs.pop("data")
            options["form" if isinstance(data, (Mapping, list, tuple)) else "body"] = data
        timeout = self.timeout
        if "timeout" in kwargs:
            # A garbage timeout must not become a transport-level surprise; the
            # session default is the only value this lane ever intends.
            try:
                timeout = float(kwargs.pop("timeout"))
            except (TypeError, ValueError):
                timeout = self.timeout
        options["timeout"] = _timedelta(timeout)
        allow_redirects = kwargs.pop("allow_redirects", True)
        options["redirect"] = module.redirect.Policy.limited(30) if allow_redirects else module.redirect.Policy.none()
        proxies = kwargs.pop("proxies", None) or self.proxies
        proxy = proxies.get(_scheme(url)) or proxies.get("all")
        if proxy:
            options["proxy"] = module.Proxy.all(proxy)
        if kwargs:
            raise TypeError(f"wreq backend 不支持请求参数: {', '.join(sorted(kwargs))}")
        return options, timeout

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        client, self._client = getattr(self, "_client", None), None
        if client is not None:
            client.__exit__(None, None, None)


def _warn_platform_caveat(impersonate: str) -> None:
    """Say once that wreq's preset and our profile table disagree about the OS.

    Measured 2026-11-19 on ``wreq`` 0.13.0: ``Profile.Firefox144`` is a **macOS**
    capture -- its default ``User-Agent`` is ``Macintosh; Intel Mac OS X 10.15`` --
    while our ``firefox144`` profile claims Windows. That is not a contradiction
    in the request (this lane sends its own UA and client hints, and Firefox's
    ClientHello does not vary by OS), but it *is* worth one log line: the run is
    no longer "the same browser" as a curl_cffi run of the same profile name, and
    an A/B that ignores that would attribute the difference to the wrong thing.
    """
    from .auth_headers import _platform_class

    if _platform_class(impersonate) != "windows":
        return
    key = ("wreq-platform", impersonate)
    if key in _WARNED:
        return
    _WARNED.add(key)
    _LOGGER.warning(
        "[transport] wreq preset %s is a macOS capture; our profile claims Windows. "
        "TLS/H2 come from the preset, UA/hints from the profile -- see "
        "docs/current/protocol-registration.md",
        impersonate,
    )


_WARNED: set[tuple[str, str]] = set()


def _scheme(url: str) -> str:
    return url.split(":", 1)[0].lower() if "://" in url else "https"


def _method(module: Any, method: str) -> Any:
    try:
        return getattr(module.Method, str(method).upper())
    except AttributeError as exc:
        raise TypeError(f"wreq backend 不支持方法: {method}") from exc


def _timedelta(seconds: float) -> Any:
    return timedelta(seconds=seconds)


def _translate_response(session: WreqSession, raw: Any, timeout: float) -> Any:
    """``wreq`` response -> ``requests.Response`` with the fields this lane reads."""
    from requests import Response
    from requests.structures import CaseInsensitiveDict
    from requests.utils import get_encoding_from_headers

    response = Response()
    response.status_code = raw.status.as_int()
    response.reason = responses.get(response.status_code, "")
    response.url = str(raw.url)
    response.headers = CaseInsensitiveDict(
        (bytes(key).decode("ascii"), bytes(value).decode("latin-1")) for key, value in raw.headers
    )
    response._content = bytes(raw.bytes())
    response._content_consumed = True
    response.encoding = get_encoding_from_headers(response.headers)
    response.cookies = session.cookies.jar
    response.elapsed = _timedelta(0.0)
    return response


__all__ = [
    "BACKENDS",
    "BACKEND_ENV",
    "BACKEND_KEY",
    "DEFAULT_BACKEND",
    "MIN_PYTHON",
    "ProtocolBackendError",
    "WreqCookies",
    "WreqSession",
    "capabilities",
    "configured_backend",
    "new_session",
    "supported_impersonations",
    "wreq_module",
    "wreq_profile",
    "wreq_profile_map",
]
