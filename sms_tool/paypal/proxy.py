from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

from ..kernel.native_loader import load_native_module
from ..proxy.entry import (
    normalize_proxy_url as _normalize_proxy_url,
    redact_proxy_url as _canon_redact_proxy_url,
    redact_proxy_text as _canon_redact_proxy_text,
)
from ..proxy.stage_health import (
    _CACHE_LOCK as _CACHE_LOCK,
    _PAYPAL_PROXY_STATE_CACHE as _PAYPAL_PROXY_STATE_CACHE,
    _proxy_health_cfg as _proxy_health_cfg,
    PayPalProxyState as PayPalProxyState,
    ProxyProbeResult as ProxyProbeResult,
    StageProxyState as StageProxyState,
    proxy_key as proxy_key,
    proxy_state_from_config as _stage_proxy_state_from_config,
    stage_proxy_state as _stage_proxy_state,
)


_NETWORK_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "connection aborted",
    "connection reset",
    "connection refused",
    "remote end closed",
    "remote disconnected",
    "unexpected_eof",
    "eof occurred",
    "ssleoferror",
    "max retries exceeded",
    "proxyerror",
    "unable to connect to proxy",
    "failed to connect",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
)


def normalize_proxy_url(proxy: str) -> str:
    return _normalize_proxy_url(proxy)


def redact_proxy_url(proxy: str) -> str:
    """Canonical (``proxy/entry.py``); preserved here for historical importers."""
    return _canon_redact_proxy_url(proxy, empty_placeholder="DIRECT")


def _redact_proxy_auth_text(value: Any) -> str:
    """Redact inline proxy auth embedded in free-form log / error text (``proxy/entry.py``)."""
    return _canon_redact_proxy_text(value)


def _rebuild_proxy_url(parsed: Any, username: str, password: str) -> str:
    from ..proxy.entry import rebuild_proxy_credentials

    return rebuild_proxy_credentials(parsed, username, password)


def rotate_proxy_session(proxy: str, country: str = "") -> str:
    """Rotate provider session credentials (and optionally the exit country).

    Thin wrapper over the single-authority ``proxy_entry.rotate_session``.
    """
    from ..proxy.entry import rotate_session

    return rotate_session(normalize_proxy_url(proxy), country)


def infer_proxy_country(proxy: str) -> str:
    """Best-effort exit-country inference from the proxy credential template.

    Thin wrapper over the single-authority ``proxy_entry.infer_proxy_country``.
    The definition site moved there on 2026-10-18: exit-country inference is a
    proxy/geo concern, not a PayPal one, and the registration lane was importing
    this PayPal-named module for it.  Kept as a delegating shell so existing
    importers and ``patch("sms_tool.paypal.proxy.infer_proxy_country")`` targets
    keep working.
    """
    from ..proxy.entry import infer_proxy_country as _infer_proxy_country

    return _infer_proxy_country(proxy)


_RETRYABLE_EXC_NAMES = frozenset(
    {"ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout", "SSLError", "ProxyError"}
)
_NATIVE_PAYPAL_PROXY_PARSE = load_native_module(
    "paypal-proxy-parse-rs", "paypal_proxy_parse_rs", "RUST_ANYTHING_UNIT_PAYPAL_PROXY_PARSE"
)
_NATIVE_IS_RETRYABLE_NETWORK_ERROR = (
    getattr(_NATIVE_PAYPAL_PROXY_PARSE, "is_retryable_network_error", None)
    if _NATIVE_PAYPAL_PROXY_PARSE is not None
    else None
)
_NATIVE_PROXY_SCHEME_CANDIDATES = (
    getattr(_NATIVE_PAYPAL_PROXY_PARSE, "_proxy_scheme_candidates", None)
    if _NATIVE_PAYPAL_PROXY_PARSE is not None
    else None
)


def is_retryable_network_error(error: Any) -> bool:
    if _NATIVE_IS_RETRYABLE_NETWORK_ERROR is not None:
        if error is None or isinstance(error, str):
            return _NATIVE_IS_RETRYABLE_NETWORK_ERROR(error)
        if any(item.__name__ in _RETRYABLE_EXC_NAMES for item in type(error).mro()):
            return True
        return _NATIVE_IS_RETRYABLE_NETWORK_ERROR(str(error or ""))
    names = {item.__name__ for item in type(error).mro()}
    if names.intersection(_RETRYABLE_EXC_NAMES):
        return True
    text = str(error or "").lower()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def probe_proxy(
    proxy: str,
    expected_country: str = "",
    stage: str = "proxy",
    timeout: float = 12,
    *,
    state: "PayPalProxyState | None" = None,
) -> ProxyProbeResult:
    value = normalize_proxy_url(proxy)
    expected = str(expected_country or "").strip().upper()
    if not value:
        return ProxyProbeResult(ok=True, stage=stage, expected_country=expected, error="direct")

    if state is not None:
        cached = state.cached_probe(value, expected, stage)
        if cached is not None:
            return cached

    results: list[ProxyProbeResult] = []
    for candidate in _proxy_scheme_candidates(value):
        result = _probe_proxy_network(candidate, expected, stage, timeout)
        result.scheme = urlsplit(candidate).scheme.lower()
        results.append(result)
        if result.ok or result.error.startswith("country_mismatch:"):
            break
    result = results[-1]
    if not result.ok and len(results) > 1:
        errors = [item.error for item in results if item.error]
        result.error = "proxy_scheme_detection_failed:" + " | ".join(errors[-3:])
    if state is not None:
        state.record_probe(value, result)
    _publish_probe_geo(value, result)
    return result


def _publish_probe_geo(value: str, result: "ProxyProbeResult") -> None:
    """Hand a successful preflight measurement to the shared geo resolver.

    The preflight already knows the real egress country — it just used to be
    dropped on the floor, and the fingerprint pool then guessed the country
    again from the credential template, with nothing reconciling the two.  (P0-1)
    """
    if not getattr(result, "ok", False) or not getattr(result, "country_code", ""):
        return
    with contextlib.suppress(Exception):
        from ..geo import ProxyGeo, remember_proxy_geo

        remember_proxy_geo(
            value,
            ProxyGeo(
                ip=str(result.ip or ""),
                country=str(result.country_code or "").upper(),
                region=str(getattr(result, "region", "") or ""),
                source="probe",
            ),
        )


def _proxy_scheme_candidates(proxy: str) -> list[str]:
    """Try the declared scheme first, then compatible HTTP/SOCKS5 variants."""
    if _NATIVE_PROXY_SCHEME_CANDIDATES is not None and (proxy is None or isinstance(proxy, str)):
        return _NATIVE_PROXY_SCHEME_CANDIDATES(proxy)
    value = normalize_proxy_url(proxy)
    if not value:
        return []
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    candidates = [value]
    alternates = {
        "http": ("socks5h", "socks5"),
        "https": ("http", "socks5h", "socks5"),
        "socks5": ("socks5h", "http"),
        "socks5h": ("socks5", "http"),
    }.get(scheme, ())
    suffix = value.split("://", 1)[1]
    candidates.extend(f"{alternate}://{suffix}" for alternate in alternates)
    return list(dict.fromkeys(candidates))


def _probe_http_get_json(url: str, proxy: str, timeout: float) -> tuple[dict[str, Any], int]:
    """GET a geo-lookup URL through ``proxy`` and return ``(json, status_code)``.

    Prefers curl_cffi with a Chrome impersonation so the probe rides the same TLS
    stack the payment flow uses (curl_cffi/Playwright); a proxy that only fails
    under that stack is caught here rather than mid-checkout. Falls back to
    ``requests`` when curl_cffi is unavailable.
    """
    # curl_cffi's ``ProxySpec`` is a ``TypedDict``; this plain mapping is what
    # every call site in the repo passes and is accepted at runtime.
    proxies: Any = {"http": proxy, "https": proxy}
    try:
        from curl_cffi import requests as _curl_requests

        response = _curl_requests.get(url, proxies=proxies, timeout=timeout, impersonate="chrome124")
    except Exception:
        session = requests.Session()
        session.trust_env = False
        session.proxies = proxies
        response = session.get(url, timeout=timeout)
    status = int(getattr(response, "status_code", 0) or 0)
    if status and not (200 <= status < 400):
        raise RuntimeError(f"HTTP {status}")
    body = response.json() or {}
    return (body if isinstance(body, dict) else {}), status


def _probe_proxy_network(value: str, expected: str, stage: str, timeout: float) -> ProxyProbeResult:
    probes = (
        (
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query,regionName",
            lambda body: (
                str(body.get("query") or ""),
                str(body.get("countryCode") or "").upper(),
                str(body.get("country") or ""),
                str(body.get("regionName") or body.get("region") or ""),
                str(body.get("message") or ""),
                str(body.get("status") or "") == "success",
            ),
        ),
        (
            "https://ipwho.is/",
            lambda body: (
                str(body.get("ip") or ""),
                str(body.get("country_code") or "").upper(),
                str(body.get("country") or ""),
                str(body.get("region") or ""),
                str(body.get("message") or ""),
                bool(body.get("success", True)),
            ),
        ),
        (
            "https://ipapi.co/json/",
            lambda body: (
                str(body.get("ip") or ""),
                str(body.get("country_code") or "").upper(),
                str(body.get("country_name") or ""),
                str(body.get("region") or ""),
                str(body.get("reason") or body.get("error") or ""),
                not bool(body.get("error")),
            ),
        ),
    )
    errors: list[str] = []
    for url, parser in probes:
        try:
            body, status_code = _probe_http_get_json(url, value, timeout)
            ip, country_code, country_name, region, message, ok = parser(body)
            if not ok or not ip or not country_code:
                errors.append(message or f"HTTP {status_code}")
                continue
            if expected and country_code != expected:
                return ProxyProbeResult(
                    ok=False,
                    stage=stage,
                    expected_country=expected,
                    ip=ip,
                    country_code=country_code,
                    country=country_name,
                    region=region,
                    error=f"country_mismatch:{country_code}",
                )
            return ProxyProbeResult(
                ok=True,
                stage=stage,
                expected_country=expected,
                ip=ip,
                country_code=country_code,
                country=country_name,
                region=region,
            )
        except Exception as exc:
            errors.append(_redact_proxy_auth_text(exc)[:160])
    return ProxyProbeResult(
        ok=False,
        stage=stage,
        expected_country=expected,
        error="proxy_probe_failed:" + " | ".join(errors[-3:]),
    )


def select_proxy_from_pool(
    proxy_pool: Iterable[str],
    expected_country: str = "",
    stage: str = "payment",
    *,
    pool_loader: Any = None,
    state: "PayPalProxyState | None" = None,
    timeout: float = 12,
) -> tuple[str, list[dict[str, Any]]]:
    """Return the first healthy country-matched dynamic proxy in pool order.

    ``pool_loader`` is an optional zero-arg callable returning an iterable of
    proxy strings.  When ``proxy_pool`` is empty, it is invoked to populate the
    candidate set (e.g. ``lambda: [e.url for e in load_proxy_pool(...)]``).

    When ``state`` is supplied the candidates are first ordered by accumulated
    health (cooldown-skipped, success-ranked) and geo probes are served from the
    shared cache, so a whole batch shares one probe per proxy instead of
    re-probing per account. Without ``state`` the behaviour is unchanged: probe
    every candidate in pool order and take the first country match.
    """
    expected = str(expected_country or "").strip().upper()
    raw_pool = list(dict.fromkeys(str(item or "").strip() for item in (proxy_pool or []) if str(item or "").strip()))
    if not raw_pool and callable(pool_loader):
        try:
            loaded: Any = pool_loader()
            raw_pool = list(
                dict.fromkeys(str(item or "").strip() for item in (loaded or []) if str(item or "").strip())
            )
        except Exception:
            raw_pool = []
    candidates = list(dict.fromkeys(normalize_proxy_url(item) for item in raw_pool))
    if state is not None and candidates:
        # Prefer healthy proxies and skip ones still in their failure cooldown;
        # fall back to raw order if every candidate is cooling down.
        candidates = state.rank(stage, candidates, country=expected) or candidates
    attempts: list[dict[str, Any]] = []
    for base in candidates:
        candidate = rotate_proxy_session(base, expected)
        # Preserve the exact no-state call shape so patched probe doubles that
        # only accept (proxy, expected_country, stage, timeout) keep working.
        if state is not None:
            result = probe_proxy(candidate, expected_country=expected, stage=stage, state=state, timeout=timeout)
        else:
            result = probe_proxy(candidate, expected_country=expected, stage=stage, timeout=timeout)
        attempts.append(
            {
                "proxy": redact_proxy_url(candidate),
                "ok": result.ok,
                "stage": result.stage,
                "expected_country": result.expected_country,
                "ip": result.ip,
                "country_code": result.country_code,
                "country": result.country,
                "error": result.error,
            }
        )
        if state is not None:
            state.record_result(stage, candidate, result.ok, reason=result.error, country=result.country_code)
        if result.ok:
            selected = candidate
            if result.scheme:
                selected = result.scheme + "://" + candidate.split("://", 1)[1]
            return selected, attempts
    return "", attempts


# ─── 阶段代理配置解析 (健康与缓存状态定义已下沉至 proxy/stage_health.py) ────────
#
# 本模块位于 ``sms_tool/paypal/``，项目根要**再上两层**（``sms_tool`` 的父目录）。
# ``_PAYPAL_PROXY_STATE_CACHE`` 与 ``PayPalProxyState`` 从 ``proxy/stage_health.py``
# 共享导入，确保 ``payment/routing.py`` 与 PayPal 适配器共享同一进程级状态字典。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _stage_proxy_value(stage_proxies: dict, key: str, fallback: str = "") -> str:
    return str((stage_proxies or {}).get(key) or fallback or "").strip()


#: The seven egress stages of a PayPal checkout, in resolution order.
STAGE_PROXY_STAGES: tuple[str, ...] = (
    "checkout",
    "promotion",
    "provider",
    "stripe_init",
    "payment_method",
    "confirm",
    "approve",
)


@dataclass(frozen=True)
class StageProxies(Mapping[str, str]):
    """One proxy string per checkout egress stage.

    Why this exists
    ---------------
    The same seven strings used to travel between ``pay_link.registry``,
    ``generate_pp_link`` and ``PPLinkExtractor`` as seven independent keyword
    arguments, and the adapter kept its own copy of their names. That copy is
    how ``max_stage_retries`` came to be forwarded to a function that never
    accepted it (a ``TypeError`` waiting for the first caller that set the
    option). One immutable value removes the ordering question, makes "which
    stages exist" a single constant, and lets a caller pass the whole set
    instead of seven keywords.

    ``Mapping`` is implemented on purpose: every existing ``proxies["checkout"]``
    and ``proxies.get("stripe_init", fallback)`` call site keeps working, while
    ``StageProxies(...) == StageProxies(...)`` compares all seven strings.
    """

    checkout: str = ""
    promotion: str = ""
    provider: str = ""
    stripe_init: str = ""
    payment_method: str = ""
    confirm: str = ""
    approve: str = ""

    def __getitem__(self, stage: str) -> str:
        if stage not in STAGE_PROXY_STAGES:
            raise KeyError(stage)
        return str(getattr(self, stage) or "")

    def __iter__(self) -> Iterator[str]:
        return iter(STAGE_PROXY_STAGES)

    def __len__(self) -> int:
        return len(STAGE_PROXY_STAGES)

    def as_dict(self) -> dict[str, str]:
        return {stage: self[stage] for stage in STAGE_PROXY_STAGES}

    @classmethod
    def from_mapping(cls, values: Any = None) -> StageProxies:
        """Read ``stage -> proxy``; anything that is not a mapping reads as empty."""
        source: Mapping = values if isinstance(values, Mapping) else {}
        return cls(**{stage: str(source.get(stage) or "").strip() for stage in STAGE_PROXY_STAGES})

    @classmethod
    def from_options(cls, values: Any = None) -> StageProxies:
        """Read the ``<stage>_proxy`` option spelling used by the adapter seam."""
        source: Mapping = values if isinstance(values, Mapping) else {}
        return cls.from_mapping({stage: source.get(f"{stage}_proxy") for stage in STAGE_PROXY_STAGES})

    def overridden(self, values: Any = None) -> StageProxies:
        """Copy with every non-empty entry of ``values`` winning.

        Empty means "not supplied", which is the same rule
        ``_resolve_stage_proxy`` applies to its explicit argument -- a caller that
        passes ``checkout_proxy=""`` must not blank a configured stage.
        """
        source: Mapping = values if isinstance(values, Mapping) else {}
        merged = self.as_dict()
        for stage in STAGE_PROXY_STAGES:
            value = str(source.get(stage) or "").strip()
            if value:
                merged[stage] = value
        return StageProxies(**merged)


def _paypal_proxy_state(paypal_cfg: dict) -> PayPalProxyState:
    return _stage_proxy_state(paypal_cfg, project_root=PROJECT_ROOT)


def proxy_state_from_config(cfg: Any) -> PayPalProxyState:
    """Return the process-shared proxy-health state for a full config mapping."""
    return _stage_proxy_state_from_config(cfg, project_root=PROJECT_ROOT)


def _proxy_pool_values(paypal_cfg: dict, key: str) -> list[str]:
    _raw_pools: object = paypal_cfg.get("stage_proxy_pools")
    pools: dict = _raw_pools if isinstance(_raw_pools, dict) else {}
    raw = pools.get(key)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [normalize_proxy_url(item) for item in raw if str(item or "").strip()]


def _rank_stage_proxy(
    paypal_cfg: dict,
    state: PayPalProxyState,
    stage: str,
    configured_value: str,
    *,
    country: str = "",
    checkout_proxy: str = "",
) -> str:
    candidates = _proxy_pool_values(paypal_cfg, stage)
    if configured_value:
        candidates.append(normalize_proxy_url(configured_value))
    ranked = state.rank(stage, candidates, country=country, checkout_proxy=checkout_proxy)
    return ranked[0] if ranked else (normalize_proxy_url(configured_value) if configured_value else "")


def _proxies_from_config(cfg: dict, checkout_country: str = "", target_country: str = "") -> StageProxies:
    """Resolve payment stage proxies from the static ``paypal.stage_proxies`` config."""
    # Function-local on purpose: ``import_layer_ratchet`` forbids the paypal ->
    # payment direction (payment is the outer layer), and ``payment.routing``
    # imports ``proxy.entry``.  ``_proxies_from_config`` is called once per link,
    # so the lazy edge costs nothing.  No ``except ImportError`` fallback: it was
    # unreachable and merely hid the import's real failures.
    from ..payment.method_config import method_payment_config

    paypal_cfg = method_payment_config(cfg, "paypal")
    stage_proxies = paypal_cfg.get("stage_proxies") or {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""
    state = _paypal_proxy_state(paypal_cfg)

    checkout_value = _stage_proxy_value(stage_proxies, "checkout", proxy_default)
    checkout = _rank_stage_proxy(
        paypal_cfg,
        state,
        "checkout",
        checkout_value,
        country=checkout_country,
    )
    provider_value = (
        _stage_proxy_value(stage_proxies, "provider")
        or _stage_proxy_value(stage_proxies, "stripe_init")
        or proxy_default
    )
    provider = _rank_stage_proxy(
        paypal_cfg,
        state,
        "provider",
        provider_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    stripe_init_value = _stage_proxy_value(stage_proxies, "stripe_init") or provider
    stripe_init = _rank_stage_proxy(
        paypal_cfg,
        state,
        "stripe_init",
        stripe_init_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    payment_method_value = _stage_proxy_value(stage_proxies, "payment_method") or provider
    payment_method = _rank_stage_proxy(
        paypal_cfg,
        state,
        "payment_method",
        payment_method_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    confirm_value = _stage_proxy_value(stage_proxies, "confirm") or provider
    confirm = _rank_stage_proxy(
        paypal_cfg,
        state,
        "confirm",
        confirm_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    approve_value = _stage_proxy_value(stage_proxies, "approve") or confirm or provider or proxy_default
    approve = _rank_stage_proxy(
        paypal_cfg,
        state,
        "approve",
        approve_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    # Promotion stage is OPT-IN: only resolved from explicit config, no fallback
    # to provider/default, so leaving it unset keeps the original behaviour.
    promotion_value = _stage_proxy_value(stage_proxies, "promotion") or _stage_proxy_value(
        stage_proxies, "promotion_update"
    )
    promotion = _rank_stage_proxy(
        paypal_cfg,
        state,
        "promotion",
        promotion_value,
    )
    return StageProxies(
        checkout=checkout,
        promotion=promotion,
        provider=provider,
        stripe_init=stripe_init,
        payment_method=payment_method,
        confirm=confirm,
        approve=approve,
    )


def _stage_proxy_is_configured(paypal_cfg: dict, *keys: str) -> bool:
    _raw_stage_proxies: object = paypal_cfg.get("stage_proxies")
    stage_proxies: dict = _raw_stage_proxies if isinstance(_raw_stage_proxies, dict) else {}
    return any(str(stage_proxies.get(key) or "").strip() for key in keys)


def _resolve_stage_proxy(
    explicit_stage_proxy: Any,
    single_proxy: Any,
    configured_proxy: Any,
    configured: bool,
    single_proxy_overrides: bool,
) -> str:
    explicit_value = str(explicit_stage_proxy or "").strip()
    if explicit_value:
        return explicit_value
    single_value = str(single_proxy or "").strip()
    configured_value = str(configured_proxy or "").strip()
    if single_proxy_overrides and single_value:
        return single_value
    if configured and configured_value:
        return configured_value
    return single_value or configured_value


def configured_stage_keys(paypal_cfg: Any) -> frozenset[str]:
    """Stage names whose proxy is written literally in ``paypal.stage_proxies``.

    The gate ``_resolve_stage_proxy`` calls ``configured`` exists so a
    *ranked pool* value cannot silently outrank an explicitly supplied single
    proxy: only a stage the operator actually wrote down may win. Reading the
    raw section once produces the same answer as calling
    ``_stage_proxy_is_configured`` per stage with that stage's key aliases.
    """
    source: Mapping = paypal_cfg if isinstance(paypal_cfg, Mapping) else {}
    raw: object = source.get("stage_proxies")
    stage_proxies: Mapping = raw if isinstance(raw, Mapping) else {}
    return frozenset(key for key, value in stage_proxies.items() if str(value or "").strip())


def resolve_stage_proxies(
    configured: StageProxies,
    explicit: StageProxies,
    *,
    single_proxy: Any = None,
    provider_argument: Any = None,
    configured_keys: frozenset[str] | set[str] = frozenset(),
    single_proxy_overrides: bool = False,
) -> StageProxies:
    """Resolve every egress stage from (explicit, single, configured) values.

    Precedence per stage, unchanged from the inline block this replaced:

    1. the stage's own keyword (``checkout_proxy=...`` / ``StageProxies.checkout``);
    2. the single ``proxy=...`` when ``explicit_proxy_overrides_stage_proxies`` is on;
    3. the configured value, but only for a stage the operator wrote down;
    4. the single ``proxy`` (or, for the stages the flow derives from the
       provider, the resolved provider).

    ``promotion`` is deliberately opt-in: it never falls back to the single
    proxy, because a promotion update from the wrong region is worse than none.
    """
    single = str(single_proxy or "").strip()
    provider_single = str(provider_argument or "").strip() or single

    def _stage(stage: str, single_value: str, configured_value: str, *flag_keys: str) -> str:
        return _resolve_stage_proxy(
            explicit[stage],
            single_value,
            configured_value,
            any(key in configured_keys for key in flag_keys),
            single_proxy_overrides,
        )

    checkout = _stage("checkout", single, configured["checkout"], "checkout")
    provider = _stage("provider", single, configured["provider"], "provider", "stripe_init")
    provider_value = str(provider or "").strip()
    stripe_init = _stage("stripe_init", provider_single, configured["stripe_init"] or provider_value, "stripe_init")
    payment_method = _stage(
        "payment_method", provider_single, configured["payment_method"] or provider_value, "payment_method"
    )
    confirm = _stage("confirm", provider_single, configured["confirm"] or provider_value, "confirm")
    approve = _stage("approve", single, configured["approve"], "approve", "confirm")
    promotion = explicit["promotion"] or configured["promotion"]

    return StageProxies(
        checkout=str(checkout or "").strip(),
        promotion=str(promotion or "").strip(),
        provider=provider_value,
        stripe_init=str(stripe_init or provider_value).strip(),
        payment_method=str(payment_method or provider_value).strip(),
        confirm=str(confirm or provider_value).strip(),
        approve=str(approve or "").strip(),
    )
