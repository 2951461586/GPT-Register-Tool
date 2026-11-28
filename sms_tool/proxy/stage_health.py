"""Process-shared stage-proxy health, pair scoring, and geo-probe cache.

Extracted from ``sms_tool/paypal/proxy.py`` so neutral payment route planning
(``sms_tool/payment/routing.py``) can read and rank stage-proxy health without
reaching into the PayPal-specific adapter package. ``sms_tool/paypal/proxy.py``
re-exports every symbol here (including the shared ``_PAYPAL_PROXY_STATE_CACHE``
dictionary) so existing callers and test fixtures stay 100% compatible.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from ..kernel.cross_process_gate import cross_process_write_lock
from ..kernel.native_loader import load_native_module
from .entry import (
    normalize_proxy_url,
    redact_proxy_url as _canon_redact_proxy_url,
    strip_session_id,
)

_NATIVE_STAGE_PROXY_KEY = load_native_module(
    "stage-proxy-key-rs", "stage_proxy_key_rs", "RUST_ANYTHING_UNIT_STAGE_PROXY_KEY"
)
_NATIVE_PROXY_KEY = getattr(_NATIVE_STAGE_PROXY_KEY, "proxy_key", None) if _NATIVE_STAGE_PROXY_KEY is not None else None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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


def proxy_key(proxy: str) -> str:
    if _NATIVE_PROXY_KEY is not None and (proxy is None or isinstance(proxy, str)):
        return _NATIVE_PROXY_KEY(proxy)
    value = strip_session_id(proxy)
    if not value:
        return "direct"
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:24]


class StageProxyState:
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
                except (OSError, ValueError, TypeError):
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
            with cross_process_write_lock(self.path.with_name(f"{self.path.name}.lock")):
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
            record["label"] = _canon_redact_proxy_url(proxy, empty_placeholder="DIRECT")
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

    def record_probe(self, proxy: str, result: ProxyProbeResult) -> None:
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

    def cached_probe(self, proxy: str, expected_country: str = "", stage: str = "proxy") -> ProxyProbeResult | None:
        """Return a still-fresh cached geo probe for ``proxy``, or ``None``."""
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
        if record.get("ok") and expected and cached_country and cached_country != expected:
            return None
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
            record = (
                self._load()
                .setdefault("pairs", {})
                .setdefault(
                    key,
                    {
                        "checkout": proxy_key(checkout_proxy),
                        "provider": proxy_key(provider_proxy),
                    },
                )
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


PayPalProxyState = StageProxyState

_PAYPAL_PROXY_STATE_CACHE: dict[tuple[Any, ...], StageProxyState] = {}
_CACHE_LOCK = threading.Lock()


def _proxy_health_cfg(paypal_cfg: dict) -> dict:
    _raw_value: object = paypal_cfg.get("proxy_health")
    value: dict = _raw_value if isinstance(_raw_value, dict) else {}
    return value or {}


def stage_proxy_state(paypal_cfg: dict, *, project_root: Path | None = None) -> StageProxyState:
    health = _proxy_health_cfg(paypal_cfg)
    state_file = str(health.get("state_file") or "runtime/paypal_proxy_state.json").strip()
    state_path = Path(state_file).expanduser()
    if not state_path.is_absolute():
        state_path = Path(project_root or PROJECT_ROOT) / state_path
    key = (
        str(state_path),
        bool(health.get("enabled", True)),
        int(health.get("fail_skip_after", 2) or 2),
        int(health.get("fail_cooldown_seconds", 180) or 0),
        int(health.get("zero_cache_ttl_seconds", 1800) or 0),
        int(health.get("probe_cache_ttl_seconds", 600) or 0),
    )
    with _CACHE_LOCK:
        state = _PAYPAL_PROXY_STATE_CACHE.get(key)
        if state is None:
            state = StageProxyState(
                state_path,
                enabled=key[1],
                fail_skip_after=key[2],
                fail_cooldown_seconds=key[3],
                zero_cache_ttl_seconds=key[4],
                probe_cache_ttl_seconds=key[5],
            )
            _PAYPAL_PROXY_STATE_CACHE[key] = state
    return state


def proxy_state_from_config(cfg: Any, *, project_root: Path | None = None) -> StageProxyState:
    """Return the process-shared proxy-health state for a full config mapping."""
    paypal_cfg = cfg.get("paypal") if isinstance(cfg, dict) else None
    if not isinstance(paypal_cfg, dict):
        try:
            paypal_cfg = dict(cfg.get("paypal") or {}) if hasattr(cfg, "get") else {}
        except (TypeError, ValueError, AttributeError):
            paypal_cfg = {}
    return stage_proxy_state(paypal_cfg if isinstance(paypal_cfg, dict) else {}, project_root=project_root)
