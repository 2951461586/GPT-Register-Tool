"""Shared proxy-state store for the protocol-payment extractors.

Batch 3 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and what is deliberately not
--------------------------------------------
The extractors each keep a lazily-loaded ``proxy_state.json`` dictionary holding
per-proxy success/failure counters, grouped by ``seed`` / ``checkout`` /
``promotion`` / ``provider`` / ``pair``.  The *mechanics* -- path resolution,
lazy load with shape defaults, atomic-ish write, stale-key pruning -- were
byte-identical across ideal / twint / blik once the provider token is
normalised away, and are provider-free.

Why this is not just "move the functions"
-----------------------------------------
The bodies reach for module-owned state: the ``_proxy_state`` global, its
``RLock``, the ``SCRIPT_DIR`` base, the ``<PREFIX>_PROXY_STATE_FILE`` env var,
the provider's own ``proxy_key`` / ``proxy_chain_key`` normalisers and its
``log`` sink.  Lifting the bodies verbatim would bind them to *this* module's
empty state -- the exact failure the first four batches avoided.  So the state
and every knob are injected through :class:`ProxyStateStore`, the same
dependency-injection shape ``proxy_selection.py`` and ``geo.py`` already use.

What is NOT here
----------------
``proxy_key`` / ``proxy_chain_key`` stay in each extractor: ``proxy_chain_key``
strips the provider's own country selector (``_PROXY_COUNTRY_SELECTOR_RE``),
which is a per-provider regex, and ``proxy_key`` is already a thin
``shared_proxy_key`` wrapper.  The seed-file cluster (``proxy_seed_file`` /
``remove_failed_proxies`` / ``unique_proxy_seeds``) and the provider error
classifiers (``record_failure_by_stage`` / ``is_*_error``) stay local too -- the
first manipulates each extractor's own seed list, the second is per-provider
vocabulary.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "ProxyStateStore",
    "checkout_zero_cache_status",
    "checkout_zero_cache_ttl",
    "is_reused_proxy_record",
    "load_proxy_state",
    "order_proxy_group",
    "prune_proxy_seed_state",
    "prune_proxy_state",
    "proxy_pair_key",
    "proxy_record",
    "proxy_remove_after_fails",
    "proxy_state_key",
    "proxy_state_path",
    "record_checkout_zero_result",
    "record_failure_by_stage",
    "record_proxy_health_failure",
    "record_proxy_pair_approve_success",
    "record_proxy_pair_result",
    "record_proxy_result",
    "save_proxy_state",
    "successful_approve_preferences",
    "successful_pair_preferences",
    "zero_cache_scheduling_enabled",
]

# The shape every extractor expects from a fresh or partial state file.  Kept
# as a factory, not a shared mutable constant: `load` returns the dict directly
# and callers mutate it in place.
_STATE_GROUPS: tuple[str, ...] = ("seed", "checkout", "promotion", "provider", "pair")

# Groups whose "prune" bookkeeping is keyed off the plain proxy identity rather
# than the country-stable chain identity.
_PRUNE_SIMPLE_GROUPS: tuple[str, ...] = ("checkout", "promotion", "provider")


def _fresh_state() -> dict[str, Any]:
    return {group: {} for group in _STATE_GROUPS}


def _coerce_int(value: Any, default: int = 0) -> int:
    """``int(value or 0)`` with a tolerant fallback.

    The original extractor bodies called ``int(record.get("x") or 0)`` directly.
    The state file is written by this code as integers, but it is also operator
    editable, and a corrupted counter used to abort a live payment run with a
    ``ValueError``.  Defaulting is strictly safer and identical for every value
    the code itself writes.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _strict_true(value: Any) -> bool:
    """Exact ``value is True`` -- ``1`` does not count, matching the original."""
    return type(value) is bool and value


def _strict_false(value: Any) -> bool:
    """Exact ``value is False`` -- ``0`` does not count, matching the original."""
    return type(value) is bool and not value


class ProxyStateStore:
    """One extractor's lazily-loaded ``proxy_state.json`` plus its lock.

    Injected knobs (never defaulted):

    ``env_prefix``
        ``IDEAL`` / ``TWINT`` / ``BLIK`` -- resolves
        ``<PREFIX>_PROXY_STATE_FILE`` and nothing else.
    ``base_dir``
        the extractor's ``SCRIPT_DIR``; the default state file is
        ``<base_dir>/proxy_state.json`` (unchanged path, so an existing state
        file keeps being read).
    ``proxy_key`` / ``proxy_chain_key``
        the extractor's own normalisers; the seed group keys off the
        country-stable chain identity, every other group off the plain key.
    ``env_bool`` / ``env_int``
        the extractor's env parsers.  The store owns the ``<PREFIX>_`` name
        construction (via ``env_prefix``), so the suffix is written once.
    ``normalize_country``
        the extractor's country normaliser -- the fallback differs per
        provider, so it is injected rather than guessed.
    ``lock``
        the extractor's existing ``_proxy_state_lock``.  It must be injected,
        not created here: the record-keeping functions acquire that lock and
        then call ``load`` / ``save``, which acquire it again.  A second lock
        would silently break that mutual exclusion.
    ``clock``
        wall-clock source for the ``last_*`` / ``zero_checked_at`` stamps;
        defaults to ``time.time`` resolved at call time so tests can freeze it.
    ``log``
        the extractor's log sink; pruning messages go here so they keep the
        extractor's own prefix and redaction.
    """

    def __init__(
        self,
        *,
        env_prefix: str,
        base_dir: Path,
        proxy_key: Callable[[str], str],
        proxy_chain_key: Callable[[str], str],
        env_bool: Callable[[str, bool], bool],
        env_int: Callable[..., int],
        normalize_country: Callable[[str], str],
        lock: threading.RLock | None = None,
        clock: Callable[[], float] | None = None,
        log: Callable[..., None] | None = None,
    ) -> None:
        self._env_prefix = str(env_prefix)
        self._base_dir = Path(base_dir)
        self._proxy_key = proxy_key
        self._proxy_chain_key = proxy_chain_key
        self._env_bool = env_bool
        self._env_int = env_int
        self._normalize_country = normalize_country
        self._clock = clock or (lambda: time.time())
        self._log = log or (lambda *_args: None)
        # Reentrant on purpose: prune acquires the lock and then calls
        # ``load`` / ``save``, which acquire it again.  A plain Lock deadlocks.
        # The extractor injects its own so record-keeping shares one lock.
        self._lock = lock if lock is not None else threading.RLock()
        self._state: dict[str, Any] | None = None

    # -- path / load / save -------------------------------------------------

    def path(self) -> Path:
        raw = os.environ.get(f"{self._env_prefix}_PROXY_STATE_FILE", "").strip()
        return Path(raw) if raw else self._base_dir / "proxy_state.json"

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._state is not None:
                return self._state
            path = self.path()
            if not path.exists():
                self._state = _fresh_state()
                return self._state
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            for group in _STATE_GROUPS:
                data.setdefault(group, {})
            self._state = data
            return self._state

    def save(self) -> None:
        with self._lock:
            if self._state is None:
                return
            path = self.path()
            path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    # -- keys / pruning -----------------------------------------------------

    def key(self, group: str, proxy: str) -> str:
        if group == "seed":
            return self._proxy_chain_key(proxy)
        return self._proxy_key(proxy)

    def prune_seed(self, proxy_seeds: list[str]) -> None:
        with self._lock:
            state = self.load()
            seed_state = state.setdefault("seed", {})
            active_keys = {self._proxy_chain_key(proxy) for proxy in proxy_seeds if self._proxy_chain_key(proxy)}
            stale_keys = [key for key in seed_state if key not in active_keys]
            for key in stale_keys:
                del seed_state[key]
            if stale_keys:
                self.save()
        if stale_keys:
            self._log(f"Seed 代理状态清理完成: {len(stale_keys)}")

    def prune(
        self,
        checkout_proxies: list[str],
        promotion_proxies: list[str],
        provider_proxies: list[str],
    ) -> None:
        removed_counts: dict[str, int] = {}
        with self._lock:
            state = self.load()
            active_keys_by_group: dict[str, set[str]] = {}
            for group, proxies in (
                ("checkout", checkout_proxies),
                ("promotion", promotion_proxies),
                ("provider", provider_proxies),
            ):
                group_state = state.get(group)
                if not isinstance(group_state, dict):
                    continue
                active_keys = {self._proxy_key(proxy) for proxy in proxies if proxy}
                active_keys_by_group[group] = active_keys
                stale_keys = [key for key in group_state if key not in active_keys]
                for key in stale_keys:
                    del group_state[key]
                if stale_keys:
                    removed_counts[group] = len(stale_keys)
            pair_state = state.get("pair")
            if isinstance(pair_state, dict):
                active_checkout = active_keys_by_group.get("checkout", set())
                active_provider = active_keys_by_group.get("provider", set())
                stale_pair_keys = [
                    key
                    for key, record in pair_state.items()
                    if not isinstance(record, dict)
                    or record.get("checkout") not in active_checkout
                    or record.get("provider") not in active_provider
                ]
                for key in stale_pair_keys:
                    del pair_state[key]
                if stale_pair_keys:
                    removed_counts["pair"] = len(stale_pair_keys)
            if removed_counts:
                self.save()
        if removed_counts:
            summary = ", ".join(f"{group}={count}" for group, count in removed_counts.items())
            self._log(f"代理状态清理完成: {summary}")

    # -- record keeping -----------------------------------------------------

    def score_enabled(self) -> bool:
        return self._env_bool(f"{self._env_prefix}_PROXY_SCORE", True)

    def record(self, group: str, proxy: str) -> dict[str, Any]:
        with self._lock:
            state = self.load()
            group_state = state.setdefault(group, {})
            key = self.key(group, proxy)
            if not key:
                return {}
            record = group_state.setdefault(key, {})
            record.setdefault("success", 0)
            record.setdefault("fail", 0)
            return record

    def record_result(self, group: str, proxy: str, success: bool, reason: str = "") -> dict[str, Any]:
        if not proxy or not self.score_enabled():
            return {}
        record = self.record(group, proxy)
        if not record:
            return {}
        now = _coerce_int(self._clock())
        if success:
            record["success"] = _coerce_int(record.get("success")) + 1
            record["fail"] = 0
            record["last_success"] = now
            record["last_reason"] = "success"
        else:
            record["fail"] = _coerce_int(record.get("fail")) + 1
            record["last_fail"] = now
            record["last_reason"] = str(reason or "failed")[:160]
        self.save()
        return record

    def remove_after_fails(self) -> int:
        return self._env_int(f"{self._env_prefix}_PROXY_REMOVE_AFTER_FAILS", 3)

    def is_reused(self, record: dict[str, Any]) -> bool:
        return _coerce_int(record.get("success")) > 0

    def record_health_failure(self, group: str, proxy: str, reason: str, remove_failed: Callable[..., Any]) -> None:
        record = self.record_result(group, proxy, False, reason)
        fail_count = _coerce_int(record.get("fail"))
        remove_after = self.remove_after_fails() if self.is_reused(record) else 1
        if fail_count >= remove_after:
            remove_failed(group, proxy, reason)

    def record_failure_by_stage(
        self,
        reason: str,
        checkout_proxy: str,
        provider_proxy: str,
        promotion_proxy: str = "",
        *,
        remove_failed: Callable[..., Any],
        is_direct_remove: Callable[[str], bool],
        is_health_failure: Callable[[str], bool],
        is_unavailable: Callable[[str], bool],
    ) -> None:
        """Dispatch a stage failure onto the ``seed`` record.

        The reason-marker strings and the dispatch order are shared protocol
        vocabulary.  Only ``is_unavailable`` is provider-specific, and
        ``remove_failed`` is the extractor's seed-file remover, so both are
        injected.
        """

        def record_seed_failure(proxy: str) -> None:
            if not proxy:
                return
            if is_direct_remove(reason):
                remove_failed("seed", proxy, reason)
                self.record_result("seed", proxy, False, reason)
            elif is_health_failure(reason):
                self.record_health_failure("seed", proxy, reason, remove_failed)
            else:
                self.record_result("seed", proxy, False, reason)

        if "checkout 阶段失败" in reason or "checkout 创建失败" in reason:
            record_seed_failure(checkout_proxy)
            return
        if is_unavailable(reason):
            return
        if "0 元优惠未生效" in reason:
            return
        if "approve blocked" in reason:
            return
        if "promotion 阶段失败" in reason or "checkout/update" in reason:
            record_seed_failure(promotion_proxy)
            return
        record_seed_failure(provider_proxy)

    # -- zero-cache window --------------------------------------------------

    def zero_cache_ttl(self) -> int:
        return self._env_int(f"{self._env_prefix}_ZERO_CACHE_TTL", 86400, minimum=0)

    def zero_scheduling_enabled(self) -> bool:
        return self._env_bool(f"{self._env_prefix}_ZERO_CACHE_SCHEDULING", False)

    def zero_cache_status(self, proxy: str, country: str) -> tuple[str, int, int]:
        if not proxy or not self._env_bool(f"{self._env_prefix}_ZERO_CACHE", True):
            return "", 0, 0
        record = self.record("seed", proxy)
        if not record:
            return "", 0, 0
        checked_at = _coerce_int(record.get("zero_checked_at"))
        if not checked_at:
            return "", 0, 0
        ttl = self.zero_cache_ttl()
        if ttl > 0 and _coerce_int(self._clock()) - checked_at > ttl:
            return "", 0, checked_at
        if self._normalize_country(str(record.get("zero_country") or country)) != self._normalize_country(country):
            return "", 0, checked_at
        amount = _coerce_int(record.get("zero_amount"))
        if _strict_true(record.get("zero_ok")):
            return "ok", amount, checked_at
        if _strict_false(record.get("zero_ok")):
            return "bad", amount, checked_at
        return "", amount, checked_at

    def record_zero_result(self, proxy: str, country: str, amount: Any) -> None:
        if not proxy or not self._env_bool(f"{self._env_prefix}_ZERO_CACHE", True):
            return
        record = self.record("seed", proxy)
        if not record:
            return
        amount = _coerce_int(amount)
        record["zero_ok"] = amount == 0
        record["zero_amount"] = amount
        record["zero_country"] = self._normalize_country(country)
        record["zero_checked_at"] = _coerce_int(self._clock())
        if amount == 0:
            record["zero_success"] = _coerce_int(record.get("zero_success")) + 1
        self.save()

    # -- checkout/provider pairs --------------------------------------------

    def pair_key(self, checkout_proxy: str, provider_proxy: str) -> str:
        checkout_key = self._proxy_key(checkout_proxy)
        provider_key = self._proxy_key(provider_proxy)
        return f"{checkout_key}:{provider_key}" if checkout_key and provider_key else ""

    def _pair_record(self, key: str, checkout_proxy: str, provider_proxy: str) -> dict[str, Any]:
        pair_state = self.load().setdefault("pair", {})
        return pair_state.setdefault(
            key,
            {"checkout": self._proxy_key(checkout_proxy), "provider": self._proxy_key(provider_proxy)},
        )

    def record_pair_result(self, checkout_proxy: str, provider_proxy: str, success: bool, reason: str = "") -> None:
        self.record_result("checkout", checkout_proxy, success, reason)
        self.record_result("provider", provider_proxy, success, reason)
        if not checkout_proxy or not provider_proxy or not self.score_enabled():
            return
        key = self.pair_key(checkout_proxy, provider_proxy)
        if not key:
            return
        with self._lock:
            record = self._pair_record(key, checkout_proxy, provider_proxy)
            now = _coerce_int(self._clock())
            if success:
                record["success"] = _coerce_int(record.get("success")) + 1
                record["fail"] = 0
                record["last_success"] = now
                record["last_reason"] = "success"
            else:
                record["fail"] = _coerce_int(record.get("fail")) + 1
                record["last_fail"] = now
                record["last_reason"] = str(reason or "failed")[:160]
            self.save()

    def record_pair_approve_success(self, checkout_proxy: str, provider_proxy: str, approve_proxy: str) -> None:
        if not checkout_proxy or not provider_proxy or not approve_proxy or not self.score_enabled():
            return
        key = self.pair_key(checkout_proxy, provider_proxy)
        approve_key = self._proxy_key(approve_proxy)
        if not key or not approve_key:
            return
        self.record_result("provider", approve_proxy, True, "approve_success")
        with self._lock:
            record = self._pair_record(key, checkout_proxy, provider_proxy)
            now = _coerce_int(self._clock())
            record["approve"] = approve_key
            record["approve_success"] = _coerce_int(record.get("approve_success")) + 1
            record["approve_last_success"] = now
            record["approve_last_reason"] = "success"
            self.save()

    def successful_approve_preferences(
        self, checkout_proxy: str, provider_proxy: str, approve_pool: list[str]
    ) -> list[str]:
        if not self.score_enabled():
            return []
        pair_state = self.load().get("pair", {})
        if not isinstance(pair_state, dict):
            return []
        record = pair_state.get(self.pair_key(checkout_proxy, provider_proxy))
        if not isinstance(record, dict):
            return []
        approve_key = str(record.get("approve") or "")
        if not approve_key:
            return []
        approve_by_key = {self._proxy_key(proxy): proxy for proxy in approve_pool}
        approve_proxy = approve_by_key.get(approve_key)
        return [approve_proxy] if approve_proxy else []

    def pair_preferences(self, checkout_proxies: list[str], provider_proxies: list[str]) -> dict[str, list[str]]:
        """Successful (checkout, provider) pairs, best-first."""
        if not self.score_enabled():
            return {}
        checkout_by_key = {self._proxy_key(proxy): proxy for proxy in checkout_proxies}
        provider_by_key = {self._proxy_key(proxy): proxy for proxy in provider_proxies}
        pair_state = self.load().get("pair", {})
        if not isinstance(pair_state, dict):
            return {}

        candidates: list[tuple[int, int, str, str]] = []
        for record in pair_state.values():
            if not isinstance(record, dict):
                continue
            success_count = _coerce_int(record.get("success"))
            if success_count <= 0:
                continue
            checkout_proxy = checkout_by_key.get(str(record.get("checkout") or ""))
            provider_proxy = provider_by_key.get(str(record.get("provider") or ""))
            if checkout_proxy and provider_proxy:
                candidates.append(
                    (success_count, _coerce_int(record.get("last_success")), checkout_proxy, provider_proxy)
                )

        candidates.sort(reverse=True)
        preferences: dict[str, list[str]] = {}
        for _success_count, _last_success, checkout_proxy, provider_proxy in candidates:
            providers = preferences.setdefault(checkout_proxy, [])
            if provider_proxy not in providers:
                providers.append(provider_proxy)
        return preferences

    # -- group ordering -----------------------------------------------------

    def order_group(self, group: str, proxies: list[str]) -> list[str]:
        """Filter + rank a proxy group by recorded success / zero-cache state.

        Ported verbatim from the extractors' ``order_proxy_group``; every knob
        (env suffix, keys, clock, log) is the store's own.  The original called
        ``env_bool("..._ZERO_CACHE_SKIP_BAD", True)`` inline inside the filter;
        it is hoisted to ``skip_zero_bad`` here so the per-proxy loop does not
        re-read the environment on every iteration (same value, same result).
        """
        if not self.score_enabled():
            return proxies
        state = self.load().get(group, {})
        skip_failed = self._env_bool(f"{self._env_prefix}_PROXY_SKIP_FAILED", True)
        fail_threshold = self._env_int(f"{self._env_prefix}_PROXY_FAIL_SKIP_AFTER", 1)
        fail_cooldown = self._env_int(f"{self._env_prefix}_PROXY_FAIL_COOLDOWN", 180, minimum=0)
        zero_ttl = self.zero_cache_ttl()
        zero_scheduling = self.zero_scheduling_enabled()
        skip_zero_bad = self._env_bool(f"{self._env_prefix}_ZERO_CACHE_SKIP_BAD", True)
        now = _coerce_int(self._clock())
        kept: list[str] = []
        cooldown_skipped = 0
        zero_skipped = 0
        zero_seen = 0
        success_seen = 0
        for proxy in proxies:
            record = state.get(self.key(group, proxy), {}) if isinstance(state, dict) else {}
            success_count = _coerce_int(record.get("success"))
            fail_count = _coerce_int(record.get("fail"))
            last_fail = _coerce_int(record.get("last_fail"))
            if success_count > 0:
                success_seen += 1
            zero_checked_at = _coerce_int(record.get("zero_checked_at"))
            zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
            if group == "checkout" and zero_scheduling and zero_cache_valid and _strict_true(record.get("zero_ok")):
                zero_seen += 1
            if (
                group == "checkout"
                and zero_scheduling
                and skip_zero_bad
                and zero_cache_valid
                and _strict_false(record.get("zero_ok"))
            ):
                zero_skipped += 1
                continue
            if skip_failed and fail_count >= fail_threshold:
                in_cooldown = fail_cooldown <= 0 or not last_fail or now - last_fail <= fail_cooldown
                if in_cooldown:
                    cooldown_skipped += 1
                    continue
            kept.append(proxy)

        if not kept and proxies:
            self._log(f"{group} 代理状态过滤后为空，已全部跳过", "[WARN] ")

        def rank(proxy: str) -> tuple[int, int, int, int, int]:
            record = state.get(self.key(group, proxy), {}) if isinstance(state, dict) else {}
            zero_checked_at = _coerce_int(record.get("zero_checked_at"))
            zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
            zero_rank = (
                1
                if group == "checkout" and zero_scheduling and zero_cache_valid and _strict_true(record.get("zero_ok"))
                else 0
            )
            return (
                zero_rank,
                _coerce_int(record.get("success")),
                _coerce_int(record.get("last_success")),
                -_coerce_int(record.get("fail")),
                -_coerce_int(record.get("last_fail")),
            )

        ordered = sorted(kept, key=rank, reverse=True)
        if cooldown_skipped or success_seen or zero_seen or zero_skipped:
            self._log(
                f"{group} 代理状态: 成功优先={success_seen}，0元命中={zero_seen}，"
                f"冷却跳过={cooldown_skipped}，0元失败跳过={zero_skipped}"
            )
        return ordered


# -- free-function surface --------------------------------------------------
#
# The extractors keep same-named module-level functions for their callers.  The
# wrappers call these with their own store, which is what keeps the parity
# report's "delegating stub" predicate (a bare ``shared_*`` call) able to
# recognise them and exclude them from the duplication count.


def proxy_state_path(store: ProxyStateStore) -> Path:
    return store.path()


def load_proxy_state(store: ProxyStateStore) -> dict[str, Any]:
    return store.load()


def save_proxy_state(store: ProxyStateStore) -> None:
    store.save()


def proxy_state_key(store: ProxyStateStore, group: str, proxy: str) -> str:
    return store.key(group, proxy)


def prune_proxy_seed_state(store: ProxyStateStore, proxy_seeds: list[str]) -> None:
    store.prune_seed(proxy_seeds)


def prune_proxy_state(
    store: ProxyStateStore,
    checkout_proxies: list[str],
    promotion_proxies: list[str],
    provider_proxies: list[str],
) -> None:
    store.prune(checkout_proxies, promotion_proxies, provider_proxies)


def proxy_record(store: ProxyStateStore, group: str, proxy: str) -> dict[str, Any]:
    return store.record(group, proxy)


def record_proxy_result(
    store: ProxyStateStore, group: str, proxy: str, success: bool, reason: str = ""
) -> dict[str, Any]:
    return store.record_result(group, proxy, success, reason)


def proxy_remove_after_fails(store: ProxyStateStore) -> int:
    return store.remove_after_fails()


def is_reused_proxy_record(store: ProxyStateStore, record: dict[str, Any]) -> bool:
    return store.is_reused(record)


def record_proxy_health_failure(
    store: ProxyStateStore,
    group: str,
    proxy: str,
    reason: str,
    remove_failed: Callable[..., Any],
) -> None:
    store.record_health_failure(group, proxy, reason, remove_failed)


def record_failure_by_stage(
    store: ProxyStateStore,
    reason: str,
    checkout_proxy: str,
    provider_proxy: str,
    promotion_proxy: str = "",
    *,
    remove_failed: Callable[..., Any],
    is_direct_remove: Callable[[str], bool],
    is_health_failure: Callable[[str], bool],
    is_unavailable: Callable[[str], bool],
) -> None:
    store.record_failure_by_stage(
        reason,
        checkout_proxy,
        provider_proxy,
        promotion_proxy,
        remove_failed=remove_failed,
        is_direct_remove=is_direct_remove,
        is_health_failure=is_health_failure,
        is_unavailable=is_unavailable,
    )


def checkout_zero_cache_ttl(store: ProxyStateStore) -> int:
    return store.zero_cache_ttl()


def zero_cache_scheduling_enabled(store: ProxyStateStore) -> bool:
    return store.zero_scheduling_enabled()


def checkout_zero_cache_status(store: ProxyStateStore, proxy: str, country: str) -> tuple[str, int, int]:
    return store.zero_cache_status(proxy, country)


def record_checkout_zero_result(store: ProxyStateStore, proxy: str, country: str, amount: Any) -> None:
    store.record_zero_result(proxy, country, amount)


def proxy_pair_key(store: ProxyStateStore, checkout_proxy: str, provider_proxy: str) -> str:
    return store.pair_key(checkout_proxy, provider_proxy)


def record_proxy_pair_result(
    store: ProxyStateStore, checkout_proxy: str, provider_proxy: str, success: bool, reason: str = ""
) -> None:
    store.record_pair_result(checkout_proxy, provider_proxy, success, reason)


def record_proxy_pair_approve_success(
    store: ProxyStateStore, checkout_proxy: str, provider_proxy: str, approve_proxy: str
) -> None:
    store.record_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)


def successful_approve_preferences(
    store: ProxyStateStore, checkout_proxy: str, provider_proxy: str, approve_pool: list[str]
) -> list[str]:
    return store.successful_approve_preferences(checkout_proxy, provider_proxy, approve_pool)


def successful_pair_preferences(
    store: ProxyStateStore, checkout_proxies: list[str], provider_proxies: list[str]
) -> dict[str, list[str]]:
    return store.pair_preferences(checkout_proxies, provider_proxies)


def order_proxy_group(store: ProxyStateStore, group: str, proxies: list[str]) -> list[str]:
    return store.order_group(group, proxies)
