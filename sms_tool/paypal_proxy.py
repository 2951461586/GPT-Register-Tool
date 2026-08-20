from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests

from .phone_proxy import normalize_proxy_url as _normalize_proxy_url, redact_proxy_url as _canon_redact_proxy_url, redact_proxy_text as _canon_redact_proxy_text


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


@dataclass
class ProxyProbeResult:
    ok: bool
    stage: str
    expected_country: str = ""
    ip: str = ""
    country_code: str = ""
    country: str = ""
    region: str = ""
    error: str = ""
    scheme: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_proxy_url(proxy: str) -> str:
    return _normalize_proxy_url(proxy)


def redact_proxy_url(proxy: str) -> str:
    """Canonical (phone_proxy); preserved here for historical importers."""
    return _canon_redact_proxy_url(proxy, empty_placeholder="DIRECT")


def _redact_proxy_auth_text(value: Any) -> str:
    """Redact inline proxy auth embedded in free-form log / error text (uses phone_proxy)."""
    return _canon_redact_proxy_text(value)


def _rebuild_proxy_url(parsed: Any, username: str, password: str) -> str:
    from .proxy_entry import rebuild_proxy_credentials

    return rebuild_proxy_credentials(parsed, username, password)


def rotate_proxy_session(proxy: str, country: str = "") -> str:
    """Rotate provider session credentials (and optionally the exit country).

    Thin wrapper over the single-authority ``proxy_entry.rotate_session``.
    """
    from .proxy_entry import rotate_session

    return rotate_session(normalize_proxy_url(proxy), country)


def retarget_proxy_country(proxy: str, country: str = "") -> str:
    """Change only the exit country while preserving the existing sticky ID.

    Thin wrapper over the single-authority ``proxy_entry.retarget_region``.
    """
    from .proxy_entry import retarget_region

    return retarget_region(normalize_proxy_url(proxy), country)


def infer_proxy_country(proxy: str) -> str:
    """Best-effort exit-country inference from the proxy credential template.

    Thin wrapper over the single-authority ``proxy_entry.infer_region``.
    """
    from .proxy_entry import infer_region

    return infer_region(normalize_proxy_url(proxy))


def is_retryable_network_error(error: Any) -> bool:
    names = {item.__name__ for item in type(error).mro()}
    if names.intersection({"ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout", "SSLError", "ProxyError"}):
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
    return result


def _proxy_scheme_candidates(proxy: str) -> list[str]:
    """Try the declared scheme first, then compatible HTTP/SOCKS5 variants."""
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
    proxies = {"http": proxy, "https": proxy}
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
            loaded = pool_loader()
            raw_pool = list(dict.fromkeys(
                str(item or "").strip() for item in (loaded or []) if str(item or "").strip()
            ))
        except Exception:
            raw_pool = []
    candidates = list(dict.fromkeys(
        normalize_proxy_url(item)
        for item in raw_pool
    ))
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
        attempts.append({
            "proxy": redact_proxy_url(candidate),
            "ok": result.ok,
            "stage": result.stage,
            "expected_country": result.expected_country,
            "ip": result.ip,
            "country_code": result.country_code,
            "country": result.country,
            "error": result.error,
        })
        if state is not None:
            state.record_result(stage, candidate, result.ok, reason=result.error, country=result.country_code)
        if result.ok:
            selected = candidate
            if result.scheme:
                selected = result.scheme + "://" + candidate.split("://", 1)[1]
            return selected, attempts
    return "", attempts


def proxy_key(proxy: str) -> str:
    value = normalize_proxy_url(proxy)
    if not value:
        return "direct"
    try:
        parsed = urlsplit(value)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        username = re.sub(r"(?<=-sid-)[A-Za-z0-9]+(?=-t-)", "SESSION", username, count=1)
        password = re.sub(
            r"^(?P<base>.+?)-(?P<country>[A-Za-z]{2})-[A-Za-z0-9]+-(?P<ttl>\d+[smhd])$",
            r"\g<base>-\g<country>-SESSION-\g<ttl>",
            password,
            count=1,
        )
        value = _rebuild_proxy_url(parsed, username, password)
    except Exception:
        pass
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:24]


class PayPalProxyState:
    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        fail_skip_after: int = 2,
        fail_cooldown_seconds: int = 180,
        zero_cache_ttl_seconds: int = 1800,
        probe_cache_ttl_seconds: int = 600,
    ):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.fail_skip_after = max(1, int(fail_skip_after or 1))
        self.fail_cooldown_seconds = max(0, int(fail_cooldown_seconds or 0))
        self.zero_cache_ttl_seconds = max(0, int(zero_cache_ttl_seconds or 0))
        # Geo-probe results are cached per stable proxy key so a batch of many
        # accounts probes each pool proxy once instead of hammering the free IP
        # geolocation services (ip-api/ipwho/ipapi) into rate limits per cell.
        self.probe_cache_ttl_seconds = max(0, int(probe_cache_ttl_seconds or 0))
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if self._data is not None:
                return self._data
            data: dict[str, Any] = {}
            if self.path.is_file():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}
            data.setdefault("stages", {})
            data.setdefault("pairs", {})
            data.setdefault("probes", {})
            self._data = data
            return data

    def _save(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            data = self._load()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp, self.path)

    def _record(self, stage: str, proxy: str) -> dict[str, Any]:
        stages = self._load().setdefault("stages", {})
        group = stages.setdefault(str(stage or "unknown"), {})
        return group.setdefault(proxy_key(proxy), {"success": 0, "fail": 0})

    def record_result(self, stage: str, proxy: str, success: bool, reason: str = "", country: str = "") -> None:
        if not self.enabled or not proxy:
            return
        with self._lock:
            record = self._record(stage, proxy)
            now = int(time.time())
            record["country"] = str(country or "").upper()
            record["label"] = redact_proxy_url(proxy)
            if success:
                record["success"] = int(record.get("success") or 0) + 1
                record["fail"] = 0
                record["last_success"] = now
                record["last_reason"] = "success"
            else:
                record["fail"] = int(record.get("fail") or 0) + 1
                record["last_fail"] = now
                record["last_reason"] = str(reason or "failed")[:200]
            self._save()

    def record_zero_result(self, proxy: str, country: str, amount: int | None) -> None:
        if not self.enabled or not proxy or amount is None:
            return
        with self._lock:
            record = self._record("checkout", proxy)
            record["zero_ok"] = int(amount) == 0
            record["zero_amount"] = int(amount)
            record["zero_country"] = str(country or "").upper()
            record["zero_checked_at"] = int(time.time())
            self._save()

    def zero_status(self, proxy: str, country: str) -> tuple[str, int | None]:
        if not self.enabled or not proxy:
            return "", None
        record = self._load().get("stages", {}).get("checkout", {}).get(proxy_key(proxy), {})
        checked_at = int(record.get("zero_checked_at") or 0)
        if not checked_at:
            return "", None
        if self.zero_cache_ttl_seconds and int(time.time()) - checked_at > self.zero_cache_ttl_seconds:
            return "", None
        if str(record.get("zero_country") or "").upper() != str(country or "").upper():
            return "", None
        return ("ok" if record.get("zero_ok") is True else "bad"), record.get("zero_amount")

    def record_probe(self, proxy: str, result: "ProxyProbeResult") -> None:
        """Persist a geo-probe outcome so later selections can skip the network."""
        if not self.enabled or not proxy or not isinstance(result, ProxyProbeResult):
            return
        with self._lock:
            probes = self._load().setdefault("probes", {})
            probes[proxy_key(proxy)] = {
                "ok": bool(result.ok),
                "ip": str(result.ip or ""),
                "country_code": str(result.country_code or "").upper(),
                "country": str(result.country or ""),
                "region": str(result.region or ""),
                "expected_country": str(result.expected_country or "").upper(),
                "error": str(result.error or "")[:200],
                "checked_at": int(time.time()),
            }
            self._save()

    def cached_probe(self, proxy: str, expected_country: str = "", stage: str = "proxy") -> "ProxyProbeResult | None":
        """Return a still-fresh cached geo probe for ``proxy``, or ``None``.

        A cached *mismatch* is honoured too: within the TTL a wrong-country proxy
        stays skipped instead of being re-probed for every account in a batch.
        A cached verdict is only reused when it still answers the country being
        asked about, because the same proxy template is probed for different
        countries across methods and stages.
        """
        if not self.enabled or not proxy or self.probe_cache_ttl_seconds <= 0:
            return None
        record = self._load().get("probes", {}).get(proxy_key(proxy))
        if not isinstance(record, dict):
            return None
        checked_at = int(record.get("checked_at") or 0)
        if not checked_at or int(time.time()) - checked_at > self.probe_cache_ttl_seconds:
            return None
        expected = str(expected_country or "").strip().upper()
        cached_country = str(record.get("country_code") or "").upper()
        # Only trust an OK cache entry whose exit country still matches the ask.
        if record.get("ok") and expected and cached_country and cached_country != expected:
            return None
        # A recorded country mismatch was judged against a *different* expected
        # country. Once the ask matches the exit the proxy actually reached, the
        # old verdict no longer applies and replaying it would fail a working
        # proxy, so re-probe instead. Transport failures keep no country and are
        # still honoured, which is what keeps a dead proxy skipped.
        if not record.get("ok") and expected and cached_country and cached_country == expected:
            return None
        return ProxyProbeResult(
            ok=bool(record.get("ok")),
            stage=stage,
            expected_country=expected or str(record.get("expected_country") or ""),
            ip=str(record.get("ip") or ""),
            country_code=cached_country,
            country=str(record.get("country") or ""),
            region=str(record.get("region") or ""),
            error=str(record.get("error") or ("cached" if record.get("ok") else "cached_probe_failed")),
        )

    def record_pair_result(
        self,
        checkout_proxy: str,
        provider_proxy: str,
        approve_proxy: str,
        success: bool,
        reason: str = "",
    ) -> None:
        if not self.enabled or not checkout_proxy or not provider_proxy:
            return
        key = f"{proxy_key(checkout_proxy)}:{proxy_key(provider_proxy)}"
        with self._lock:
            record = self._load().setdefault("pairs", {}).setdefault(
                key,
                {
                    "checkout": proxy_key(checkout_proxy),
                    "provider": proxy_key(provider_proxy),
                },
            )
            now = int(time.time())
            if success:
                record["success"] = int(record.get("success") or 0) + 1
                record["fail"] = 0
                record["last_success"] = now
                record["approve"] = proxy_key(approve_proxy)
                record["last_reason"] = "success"
            else:
                record["fail"] = int(record.get("fail") or 0) + 1
                record["last_fail"] = now
                record["last_reason"] = str(reason or "failed")[:200]
            self._save()

    def pair_score(self, checkout_proxy: str, provider_proxy: str) -> tuple[int, int, int]:
        key = f"{proxy_key(checkout_proxy)}:{proxy_key(provider_proxy)}"
        record = self._load().get("pairs", {}).get(key, {})
        return (
            int(record.get("success") or 0),
            int(record.get("last_success") or 0),
            -int(record.get("fail") or 0),
        )

    def rank(self, stage: str, proxies: Iterable[str], *, country: str = "", checkout_proxy: str = "") -> list[str]:
        unique = list(dict.fromkeys(normalize_proxy_url(item) for item in proxies if str(item or "").strip()))
        if not self.enabled or not unique:
            return unique
        records = self._load().get("stages", {}).get(stage, {})
        now = int(time.time())
        kept: list[str] = []
        for proxy in unique:
            record = records.get(proxy_key(proxy), {})
            fail = int(record.get("fail") or 0)
            last_fail = int(record.get("last_fail") or 0)
            in_cooldown = self.fail_cooldown_seconds <= 0 or now - last_fail <= self.fail_cooldown_seconds
            if fail >= self.fail_skip_after and last_fail and in_cooldown:
                continue
            if stage == "checkout":
                zero_status, _ = self.zero_status(proxy, country)
                if zero_status == "bad":
                    continue
            kept.append(proxy)

        def score(proxy: str) -> tuple[int, ...]:
            record = records.get(proxy_key(proxy), {})
            zero_status, _ = self.zero_status(proxy, country) if stage == "checkout" else ("", None)
            pair = self.pair_score(checkout_proxy, proxy) if stage == "provider" and checkout_proxy else (0, 0, 0)
            return (
                pair[0],
                pair[1],
                1 if zero_status == "ok" else 0,
                int(record.get("success") or 0),
                int(record.get("last_success") or 0),
                -int(record.get("fail") or 0),
            )

        return sorted(kept, key=score, reverse=True)


# ─── 阶段代理配置解析 (从 gen_pp_link.py 纯搬迁, 零行为变化) ────────────────────
#
# 本块负责把 ``paypal.stage_proxies`` / ``paypal.stage_proxy_pools`` /
# ``paypal.proxy_health`` 解析成各阶段的实际代理值。``_PAYPAL_PROXY_STATE_CACHE``
# 是进程级单例缓存, ``gen_pp_link`` 通过 re-export 共享同一对象 (测试会 clear 它)。

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PAYPAL_PROXY_STATE_CACHE: dict[tuple[Any, ...], PayPalProxyState] = {}


def _stage_proxy_value(stage_proxies: dict, key: str, fallback: str = "") -> str:
    return str((stage_proxies or {}).get(key) or fallback or "").strip()


def _proxy_health_cfg(paypal_cfg: dict) -> dict:
    value = paypal_cfg.get("proxy_health") if isinstance(paypal_cfg.get("proxy_health"), dict) else {}
    return value or {}


def _paypal_proxy_state(paypal_cfg: dict) -> PayPalProxyState:
    health = _proxy_health_cfg(paypal_cfg)
    state_file = str(health.get("state_file") or "runtime/paypal_proxy_state.json").strip()
    state_path = Path(state_file).expanduser()
    if not state_path.is_absolute():
        state_path = Path(PROJECT_ROOT) / state_path
    key = (
        str(state_path),
        bool(health.get("enabled", True)),
        int(health.get("fail_skip_after", 2) or 2),
        int(health.get("fail_cooldown_seconds", 180) or 0),
        int(health.get("zero_cache_ttl_seconds", 1800) or 0),
        int(health.get("probe_cache_ttl_seconds", 600) or 0),
    )
    state = _PAYPAL_PROXY_STATE_CACHE.get(key)
    if state is None:
        state = PayPalProxyState(
            state_path,
            enabled=key[1],
            fail_skip_after=key[2],
            fail_cooldown_seconds=key[3],
            zero_cache_ttl_seconds=key[4],
            probe_cache_ttl_seconds=key[5],
        )
        _PAYPAL_PROXY_STATE_CACHE[key] = state
    return state


def proxy_state_from_config(cfg: Any) -> PayPalProxyState:
    """Return the process-shared proxy-health state for a full config mapping.

    Reads ``paypal.proxy_health`` (health thresholds + state file) so that the
    two-pool manager path, the batch runner, and the single-proxy stage path all
    share one health/geo-probe cache instead of re-probing independently.
    """
    paypal_cfg = cfg.get("paypal") if isinstance(cfg, dict) else None
    if not isinstance(paypal_cfg, dict):
        try:
            paypal_cfg = dict(cfg.get("paypal") or {}) if hasattr(cfg, "get") else {}
        except Exception:
            paypal_cfg = {}
    return _paypal_proxy_state(paypal_cfg if isinstance(paypal_cfg, dict) else {})


def _proxy_pool_values(paypal_cfg: dict, key: str) -> list[str]:
    pools = paypal_cfg.get("stage_proxy_pools") if isinstance(paypal_cfg.get("stage_proxy_pools"), dict) else {}
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


def _proxies_from_config(cfg: dict, checkout_country: str = "", target_country: str = "") -> dict:
    """Resolve payment stage proxies from the static ``paypal.stage_proxies`` config."""
    try:
        from .payment_routing import method_payment_config

        paypal_cfg = method_payment_config(cfg, "paypal")
    except (ImportError, TypeError, AttributeError):  # pragma: no cover - direct script execution
        paypal_cfg = cfg.get("paypal") or {}
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
        paypal_cfg, state, "stripe_init", stripe_init_value, country=target_country, checkout_proxy=checkout,
    )
    payment_method_value = _stage_proxy_value(stage_proxies, "payment_method") or provider
    payment_method = _rank_stage_proxy(
        paypal_cfg, state, "payment_method", payment_method_value, country=target_country, checkout_proxy=checkout,
    )
    confirm_value = _stage_proxy_value(stage_proxies, "confirm") or provider
    confirm = _rank_stage_proxy(
        paypal_cfg, state, "confirm", confirm_value, country=target_country, checkout_proxy=checkout,
    )
    approve_value = (
        _stage_proxy_value(stage_proxies, "approve")
        or confirm
        or provider
        or proxy_default
    )
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
    promotion_value = (
        _stage_proxy_value(stage_proxies, "promotion")
        or _stage_proxy_value(stage_proxies, "promotion_update")
    )
    promotion = _rank_stage_proxy(
        paypal_cfg,
        state,
        "promotion",
        promotion_value,
    )
    return {
        "checkout": checkout,
        "promotion": promotion,
        "provider": provider,
        "stripe_init": stripe_init,
        "payment_method": payment_method,
        "confirm": confirm,
        "approve": approve,
    }


def _stage_proxy_is_configured(paypal_cfg: dict, *keys: str) -> bool:
    stage_proxies = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
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
