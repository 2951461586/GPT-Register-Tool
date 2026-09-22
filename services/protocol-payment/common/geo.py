"""Exit-geo and target-reachability selection for protocol-payment extractors.

Origin: blik's proxy-geo cluster, generalised for the consolidation plan
(`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`,
Q1 decision, 2026-09-19).

Why this exists
---------------
blik grew a self-contained "is this exit the right country / can it even reach
the target site" layer. ideal/twint never received it, and ``sms_tool`` already
has a more complete resolver (hint/probe precedence, TTL caches, ``ProxyGeo``).
Because this package is a separate process (Boundary Rule 10: no ``sms_tool``
import), it cannot reuse that resolver — so the blik implementation is lifted
here as the services-side authority, with the extractor's per-module state
injected rather than shared.

Injected knobs (never defaulted)
--------------------------------
``record``        per-extractor geo/target cache record (its own state file)
``save_state``    per-extractor persistence for that record
``new_session``   per-extractor session factory (honours its pre-proxy mode)
``env_bool`` / ``env_int``  per-extractor env parsing
``redact``        per-extractor redactor for error text
``log``           per-extractor logger

What is pure (shared directly): ``clean_country_code``, ``geo_lookup_urls``,
``parse_geo_country``, ``target_response_error``, ``format_expected_countries``.
What is stateful (injected): the ``lookup_*`` / ``ensure_*`` / ``precheck_*``
functions that read and write the extractor's proxy-state records.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import endpoints

__all__ = [
    "clean_country_code",
    "format_expected_countries",
    "geo_lookup_urls",
    "parse_geo_country",
    "target_probe_urls",
    "target_response_error",
    "expected_proxy_countries",
    "lookup_proxy_country",
    "lookup_proxy_targets",
    "ensure_proxy_country",
    "ensure_proxy_targets",
    "precheck_proxy_group",
]


def clean_country_code(value: str) -> str:
    return re.sub(r"[^A-Z]", "", str(value or "").upper())[:2]


def format_expected_countries(countries: set[str]) -> str:
    return ",".join(sorted(countries))


def geo_lookup_urls() -> list[tuple[str, str]]:
    return [
        ("ip-api", "http://ip-api.com/json/?fields=status,countryCode,as,message"),
        ("ipwho", "https://ipwho.is/?fields=success,country_code,connection,message"),
        ("ipapi", "https://ipapi.co/json/"),
    ]


def parse_geo_country(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    if source == "ip-api":
        if payload.get("status") != "success":
            return "", str(payload.get("message") or "")
        return clean_country_code(str(payload.get("countryCode") or "")), str(payload.get("as") or "")
    if source == "ipwho":
        if payload.get("success") is False:
            return "", str(payload.get("message") or "")
        connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
        return clean_country_code(str(payload.get("country_code") or "")), str(connection.get("asn") or "")
    if source == "ipapi":
        error = payload.get("error")
        if error:
            return "", str(payload.get("reason") or payload.get("message") or error)
        return clean_country_code(str(payload.get("country_code") or "")), str(payload.get("org") or "")
    return "", ""


def target_probe_urls(group: str) -> list[tuple[str, str]]:
    if group == "checkout":
        return [("chatgpt", f"{endpoints.CHATGPT_BASE}/")]
    return [
        ("chatgpt", f"{endpoints.CHATGPT_BASE}/"),
        ("stripe", f"{endpoints.STRIPE_API_BASE}/"),
    ]


def target_response_error(resp: Any) -> str:
    headers = getattr(resp, "headers", {}) or {}
    status_code = int(getattr(resp, "status_code", 0) or 0)
    origin = str(headers.get("x-response-origin") or headers.get("X-Response-Origin") or "").lower()
    proxy_auth = str(headers.get("proxy-authenticate") or headers.get("Proxy-Authenticate") or "").lower()
    if "proxy-server" in origin:
        return f"HTTP_{status_code}:proxy-server"
    if status_code == 407 or proxy_auth:
        return f"HTTP_{status_code}:proxy-auth"
    if status_code >= 500:
        return f"HTTP_{status_code}"
    return ""


def expected_proxy_countries(
    group: str,
    *,
    env_prefix: str,
    default_checkout_country: Callable[[], str],
    default_country: Callable[[], str],
    default_provider_countries: Callable[[], str],
) -> set[str]:
    """Resolve the expected exit-country set for a proxy group.

    ``env_prefix`` is ``IDEAL``/``TWINT``/``BLIK`` — each extractor keeps its own
    ``<PREFIX>_CHECKOUT_PROXY_COUNTRY`` / ``<PREFIX>_PROVIDER_PROXY_COUNTRIES``
    env vars, so the names are derived rather than hard-coded here.
    """
    def _expected_country() -> str:
        if group == "checkout":
            raw = os.environ.get(f"{env_prefix}_CHECKOUT_PROXY_COUNTRY", default_checkout_country())
        else:
            raw = os.environ.get(f"{env_prefix}_PROVIDER_PROXY_COUNTRY", default_country())
        return clean_country_code(raw)

    if group == "checkout":
        return {_expected_country()}
    raw = os.environ.get(
        f"{env_prefix}_PROVIDER_PROXY_COUNTRIES",
        os.environ.get(f"{env_prefix}_PROVIDER_PROXY_COUNTRY", default_provider_countries()),
    )
    countries = {clean_country_code(item) for item in re.split(r"[,;\s]+", raw) if clean_country_code(item)}
    return countries or {_expected_country()}


def lookup_proxy_country(
    group: str,
    proxy: str,
    timeout: int | None = None,
    *,
    record: dict[str, Any],
    save_state: Callable[[], None],
    new_session: Callable[..., Any],
    env_bool: Callable[[str, bool], bool],
    env_int: Callable[..., int],
    redact: Callable[[str], str],
    env_prefix: str,
) -> tuple[str, str, str]:
    now = int(time.time())
    use_pre_proxy = env_bool(f"{env_prefix}_PROXY_GEO_USE_PRE_PROXY", False)
    cached_country = clean_country_code(str(record.get("country") or ""))
    checked_at = int(record.get("country_checked_at") or 0)
    cache_matches = "country_pre_proxy" in record and bool(record.get("country_pre_proxy")) == use_pre_proxy
    cache_ttl = env_int(f"{env_prefix}_PROXY_GEO_CACHE_TTL", 3600)
    if cached_country and cache_matches and now - checked_at <= cache_ttl:
        return cached_country, str(record.get("country_as") or ""), "cache"

    last_error = ""
    request_timeout = timeout or env_int(f"{env_prefix}_PROXY_GEO_TIMEOUT", 15)
    session = new_session(proxy, use_pre_proxy=use_pre_proxy)
    for source, url in geo_lookup_urls():
        try:
            resp = session.get(url, timeout=request_timeout)
            if resp.status_code != 200:
                last_error = f"{source}:HTTP_{resp.status_code}"
                continue
            payload = resp.json() or {}
            country, asn = parse_geo_country(source, payload)
            if country:
                record["country"] = country
                record["country_as"] = asn
                record["country_source"] = source
                record["country_checked_at"] = now
                record["country_pre_proxy"] = use_pre_proxy
                save_state()
                return country, asn, source
            last_error = f"{source}:{asn or 'no_country'}"
        except Exception as exc:
            last_error = f"{source}:{str(exc)[:80]}"

    record["country"] = ""
    record["country_error"] = redact(last_error)
    record["country_checked_at"] = now
    record["country_pre_proxy"] = use_pre_proxy
    save_state()
    return "", last_error, "error"


def lookup_proxy_targets(
    group: str,
    proxy: str,
    timeout: int | None = None,
    *,
    record: dict[str, Any],
    save_state: Callable[[], None],
    new_session: Callable[..., Any],
    env_bool: Callable[[str, bool], bool],
    env_int: Callable[..., int],
    redact: Callable[[str], str],
    env_prefix: str,
    user_agent: str,
) -> tuple[bool, str]:
    now = int(time.time())
    use_pre_proxy = env_bool(f"{env_prefix}_PROXY_TARGET_USE_PRE_PROXY", True)
    checked_at = int(record.get("target_checked_at") or 0)
    cached_ok = record.get("target_ok")
    ttl = env_int(f"{env_prefix}_PROXY_TARGET_CACHE_TTL", 1800, minimum=0)
    cache_matches = "target_pre_proxy" in record and bool(record.get("target_pre_proxy")) == use_pre_proxy
    if isinstance(cached_ok, bool) and cache_matches and checked_at and (ttl <= 0 or now - checked_at <= ttl):
        return cached_ok, "cache" if cached_ok else str(record.get("target_error") or "cache_failed")

    request_timeout = timeout or env_int(
        f"{env_prefix}_PROXY_TARGET_TIMEOUT", env_int(f"{env_prefix}_PROXY_PRECHECK_TIMEOUT", 20)
    )
    session = new_session(proxy, use_pre_proxy=use_pre_proxy)
    session.headers.update({"User-Agent": user_agent, "Accept": "*/*"})
    last_error = ""
    for name, url in target_probe_urls(group):
        try:
            resp = session.get(url, timeout=request_timeout, allow_redirects=False)
            response_error = target_response_error(resp)
            if response_error:
                last_error = f"{name}:{response_error}"
                break
        except Exception as exc:
            last_error = f"{name}:{str(exc)[:120]}"
            break

    ok = not last_error
    record["target_ok"] = ok
    record["target_error"] = "" if ok else redact(last_error)
    record["target_checked_at"] = now
    record["target_pre_proxy"] = use_pre_proxy
    save_state()
    return ok, "ok" if ok else last_error


def ensure_proxy_country(
    group: str,
    proxy: str,
    *,
    lookup_country: Callable[..., tuple[str, str, str]],
    expected_countries: Callable[[str], set[str]],
    env_bool: Callable[[str, bool], bool],
    log: Callable[..., None],
    label: Callable[[str], str],
    remove_failed: Callable[[str, str, str], None],
    env_prefix: str,
) -> None:
    if not env_bool(f"{env_prefix}_PROXY_GEO_CHECK", True):
        return
    expected = expected_countries(group)
    if not expected:
        return
    country, asn, source = lookup_country(group, proxy)
    log(
        f"{group} 出口检测: {label(proxy)} country={country or 'UNKNOWN'} "
        f"expected={format_expected_countries(expected)} source={source}"
    )
    if not country:
        return
    if country not in expected:
        reason = f"{group} 代理出口国家不符: actual={country}, expected={format_expected_countries(expected)}"
        remove_failed(group, proxy, reason)
        raise RuntimeError(reason)


def ensure_proxy_targets(
    group: str,
    proxy: str,
    *,
    lookup_targets: Callable[..., tuple[bool, str]],
    env_bool: Callable[[str, bool], bool],
    log: Callable[..., None],
    label: Callable[[str], str],
    env_prefix: str,
) -> None:
    if not env_bool(f"{env_prefix}_PROXY_TARGET_CHECK", True):
        return
    ok, reason = lookup_targets(group, proxy)
    log(f"{group} 目标站检测: {label(proxy)} reachable={ok} source={reason}")
    if not ok:
        raise RuntimeError(f"{group} 代理目标站不可达: {reason}")


def precheck_proxy_group(
    group: str,
    proxies: list[str],
    *,
    lookup_country: Callable[..., tuple[str, str, str]],
    lookup_targets: Callable[..., tuple[bool, str]],
    expected_countries: Callable[[str], set[str]],
    env_bool: Callable[[str, bool], bool],
    env_int: Callable[..., int],
    log: Callable[..., None],
    record_health_failure: Callable[[str, str, str], None],
    remove_failed_proxies: Callable[[str, list[tuple[str, str]]], None],
    env_prefix: str,
) -> list[str]:
    if not env_bool(f"{env_prefix}_PROXY_PRECHECK", True):
        return proxies
    geo_enabled = env_bool(f"{env_prefix}_PROXY_GEO_CHECK", True)
    target_enabled = env_bool(f"{env_prefix}_PROXY_TARGET_CHECK", True) and env_bool(
        f"{env_prefix}_PROXY_TARGET_PRECHECK", True
    )
    if not geo_enabled and not target_enabled:
        return proxies
    expected = expected_countries(group)
    if geo_enabled and not expected:
        return proxies

    total = len(proxies)
    requested_workers = env_int(f"{env_prefix}_PROXY_PRECHECK_WORKERS", 50)
    worker_limit = env_int(f"{env_prefix}_PROXY_PRECHECK_WORKERS_MAX", 50)
    workers = min(requested_workers, worker_limit, total)
    timeout = env_int(f"{env_prefix}_PROXY_PRECHECK_TIMEOUT", 20)
    if requested_workers > workers:
        log(f"{group} 代理预筛并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        f"{group} 代理预筛开始: total={total}, "
        f"expected={format_expected_countries(expected) if geo_enabled else 'SKIP'}, "
        f"target={'on' if target_enabled else 'off'}, workers={workers}, timeout={timeout}s"
    )

    kept_set: set[str] = set()
    country_failures: list[tuple[str, str]] = []
    target_failures: list[tuple[str, str]] = []
    failed = 0
    unknown = 0

    def check(proxy: str) -> tuple[str, str, str, bool, str]:
        country = ""
        source = "skip"
        if geo_enabled:
            country, _asn, source = lookup_country(group, proxy, timeout=timeout)
        target_ok = True
        target_reason = "skip"
        if target_enabled and (not geo_enabled or country in expected):
            target_ok, target_reason = lookup_targets(group, proxy, timeout=timeout)
        return proxy, country, source, target_ok, target_reason

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check, proxy) for proxy in proxies]
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                proxy, country, source, target_ok, target_reason = future.result()
            except Exception:
                failed += 1
                continue
            country_ok = (not geo_enabled) or country in expected
            if country_ok and target_ok:
                kept_set.add(proxy)
            else:
                failed += 1
                if not country_ok and not country:
                    unknown += 1
                elif not country_ok:
                    country_failures.append(
                        (
                            proxy,
                            f"预筛出口国家不符: actual={country}, expected={format_expected_countries(expected)}, source={source}",
                        )
                    )
                else:
                    reason = f"预筛目标站不可达: {target_reason}"
                    target_failures.append((proxy, reason))
                    record_health_failure(group, proxy, reason)
            if index % 50 == 0 or index == total:
                log(f"{group} 代理预筛进度: {index}/{total}, kept={len(kept_set)}, failed={failed}, unknown={unknown}")

    remove_failed_proxies(group, country_failures)
    if not kept_set:
        failed_set = {proxy for proxy, _reason in country_failures}
        failed_set.update(proxy for proxy, _reason in target_failures)
        remaining = [proxy for proxy in proxies if proxy not in failed_set]
        if remaining:
            log(f"{group} 代理预筛无明确可用结果，仅保留 {len(remaining)} 条出口未知代理继续跑", "[WARN] ")
        return remaining
    kept = [proxy for proxy in proxies if proxy in kept_set]
    log(f"{group} 代理预筛完成: kept={len(kept)}/{total}, removed={total - len(kept)}, unknown={unknown}")
    return kept

