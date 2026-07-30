from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests


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
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_proxy_url(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    scheme = ""
    rest = value
    if "://" in value:
        scheme, rest = value.split("://", 1)
    parts = rest.split(":")
    if "@" not in rest and len(parts) == 4 and "." in parts[0] and parts[1].isdigit():
        host, port, username, password = parts
        return f"{scheme or 'http'}://{quote(username, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"
    if not scheme:
        value = f"http://{rest}"
    return value


def redact_proxy_url(proxy: str) -> str:
    value = normalize_proxy_url(proxy)
    if not value:
        return "DIRECT"
    try:
        parsed = urlsplit(value)
    except Exception:
        return "***"
    host = parsed.hostname or ""
    if not host:
        return "***"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = "***:***@" if parsed.username is not None else ""
    return urlunsplit((parsed.scheme or "http", f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def _redact_proxy_auth_text(value: Any) -> str:
    return re.sub(
        r"(?i)(https?|socks5h?)://[^\s/@]+@",
        lambda match: f"{match.group(1)}://***:***@",
        str(value or ""),
    )


def _rebuild_proxy_url(parsed: Any, username: str, password: str) -> str:
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


def _random_session_id(length: int, numeric: bool = False) -> str:
    alphabet = "0123456789" if numeric else "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    size = max(6, int(length or 8))
    return "".join(random.choice(alphabet) for _ in range(size))


def rotate_proxy_session(proxy: str, country: str = "") -> str:
    """Rotate provider session credentials without changing the proxy endpoint."""
    value = normalize_proxy_url(proxy)
    country = str(country or "").strip().upper()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username:
        return value

    changed = False
    if country:
        username, count = re.subn(r"region-[A-Za-z]{2}", f"region-{country}", username, count=1)
        changed = changed or bool(count)

    username, count = re.subn(
        r"(?<=-sid-)[A-Za-z0-9]+(?=-t-)",
        lambda match: _random_session_id(len(match.group(0))),
        username,
        count=1,
    )
    changed = changed or bool(count)

    # Kookeey gateway passwords commonly end in BASE-CC-SESSION-TTL.
    match = re.match(r"^(?P<base>.+?)-(?P<country>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])$", password)
    if match:
        password = (
            f"{match.group('base')}-{country or match.group('country').upper()}-"
            f"{_random_session_id(len(match.group('sid')), numeric=match.group('sid').isdigit())}-{match.group('ttl')}"
        )
        changed = True

    return _rebuild_proxy_url(parsed, username, password) if changed else value


def retarget_proxy_country(proxy: str, country: str = "") -> str:
    """Change only the exit country while preserving the existing sticky ID."""
    value = normalize_proxy_url(proxy)
    country = str(country or "").strip().upper()
    if not value or not country:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username:
        return value
    changed = False
    username, count = re.subn(r"region-[A-Za-z]{2}", f"region-{country}", username, count=1)
    changed = changed or bool(count)
    match = re.match(r"^(?P<base>.+?)-(?P<country>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])$", password)
    if match:
        password = f"{match.group('base')}-{country}-{match.group('sid')}-{match.group('ttl')}"
        changed = True
    return _rebuild_proxy_url(parsed, username, password) if changed else value


def infer_proxy_country(proxy: str) -> str:
    value = normalize_proxy_url(proxy)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
    except Exception:
        return ""
    match = re.search(r"region-([A-Za-z]{2})(?=$|[-_:])", username)
    if match:
        return match.group(1).upper()
    match = re.match(r"^.+?-([A-Za-z]{2})-[A-Za-z0-9]+-\d+[smhd]$", password)
    if match:
        return match.group(1).upper()
    match = re.search(r"-([A-Za-z]{2})(?:-[A-Za-z0-9]+)?$", username)
    return match.group(1).upper() if match else ""


def is_retryable_network_error(error: Any) -> bool:
    names = {item.__name__ for item in type(error).mro()}
    if names.intersection({"ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout", "SSLError", "ProxyError"}):
        return True
    text = str(error or "").lower()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def probe_proxy(proxy: str, expected_country: str = "", stage: str = "proxy", timeout: float = 12) -> ProxyProbeResult:
    value = normalize_proxy_url(proxy)
    expected = str(expected_country or "").strip().upper()
    if not value:
        return ProxyProbeResult(ok=True, stage=stage, expected_country=expected, error="direct")

    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": value, "https": value}
    probes = (
        (
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query",
            lambda body: (
                str(body.get("query") or ""),
                str(body.get("countryCode") or "").upper(),
                str(body.get("country") or ""),
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
                str(body.get("reason") or body.get("error") or ""),
                not bool(body.get("error")),
            ),
        ),
    )
    errors: list[str] = []
    for url, parser in probes:
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            ip, country_code, country_name, message, ok = parser(response.json() or {})
            if not ok or not ip or not country_code:
                errors.append(message or f"HTTP {response.status_code}")
                continue
            if expected and country_code != expected:
                return ProxyProbeResult(
                    ok=False,
                    stage=stage,
                    expected_country=expected,
                    ip=ip,
                    country_code=country_code,
                    country=country_name,
                    error=f"country_mismatch:{country_code}",
                )
            return ProxyProbeResult(
                ok=True,
                stage=stage,
                expected_country=expected,
                ip=ip,
                country_code=country_code,
                country=country_name,
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
) -> tuple[str, list[dict[str, Any]]]:
    """Return the first healthy country-matched dynamic proxy in pool order."""
    expected = str(expected_country or "").strip().upper()
    candidates = list(dict.fromkeys(
        normalize_proxy_url(item)
        for item in (proxy_pool or [])
        if str(item or "").strip()
    ))
    attempts: list[dict[str, Any]] = []
    for base in candidates:
        candidate = rotate_proxy_session(base, expected)
        result = probe_proxy(candidate, expected_country=expected, stage=stage)
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
        if result.ok:
            return candidate, attempts
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
    ):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.fail_skip_after = max(1, int(fail_skip_after or 1))
        self.fail_cooldown_seconds = max(0, int(fail_cooldown_seconds or 0))
        self.zero_cache_ttl_seconds = max(0, int(zero_cache_ttl_seconds or 0))
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
