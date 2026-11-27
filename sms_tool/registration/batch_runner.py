import logging
from concurrent.futures import ThreadPoolExecutor
import uuid
import threading
from typing import Any

from ..accounts.account_identity import proxy_egress_key
from ..config import CFG
from ..phone.phone_proxy import normalize_proxy_url, probe_proxy_with_scheme_detection, refresh_proxy_sid
from ..proxy.entry import infer_proxy_country
from ..proxy.health import ProxyHealthTracker
from ..storage import (
    acquire_environment_lease,
    get_account_records,
    get_registration_checkpoints,
    list_account_records,
    record_environment_observations,
    release_environment_lease,
)
from .batch_helpers import (
    BATCH_DROPPED_CLASSES,
    BATCH_RETRY_CLASSES,
    BatchCircuitBreaker,
    RegistrationProxyPool,
    _alias_base_email,
    _announce_dead_end,
    _announce_skipped,
    _cached_exit_ip,
    _cancelled_batch_result,
    _check_batch_account_guard_state,
    _close_batch_browser_pool as _close_batch_browser_pool,
    _dispatch_batch_execution,
    _emit_account_completion_events,
    _emit_mailboxes_skipped as _emit_mailboxes_skipped,
    _evaluate_batch_attempt_verdict,
    _proxy_endpoint_group,
    _registration_proxy_metadata,
    _resolve_batch_workers_and_breaker,
    _run_batch_sequential as _run_batch_sequential,
    _run_batch_threaded as _run_batch_threaded,
    _unique_mailboxes,
    check_node_runner_readiness as check_node_runner_readiness,
    classify_error,
    ensure_batch_sentinel_readiness as _ensure_batch_sentinel_readiness_impl,
    load_native_module,
    safe_print,
    sanitize_text,
)
from .result import safe_proxy_audit
from .retry_guard import RegistrationRetryGuard, mailbox_registration_status


_LOGGER = logging.getLogger(__name__)

_NATIVE_BATCH_PROXY = load_native_module("batch-proxy-rs", "batch_proxy_rs", "RUST_ANYTHING_UNIT_BATCH_PROXY")
_NATIVE_INTERLEAVE_PROXY_ENDPOINTS = getattr(_NATIVE_BATCH_PROXY, "_interleave_proxy_endpoints", None)


def _interleave_proxy_endpoints(proxies) -> list[str]:
    """Keep healthy endpoints represented instead of flattening them by score."""
    if (
        _NATIVE_INTERLEAVE_PROXY_ENDPOINTS is not None
        and isinstance(proxies, list)
        and all(isinstance(p, str) for p in proxies)
    ):
        return _NATIVE_INTERLEAVE_PROXY_ENDPOINTS(proxies)
    groups: dict[str, list[str]] = {}
    for proxy in dict.fromkeys(proxies or []):
        groups.setdefault(_proxy_endpoint_group(proxy), []).append(proxy)
    if len(groups) <= 1:
        return list(dict.fromkeys(proxies or []))
    ordered = []
    pending = list(groups.values())
    while pending:
        next_round = []
        for group in pending:
            ordered.append(group.pop(0))
            if group:
                next_round.append(group)
        pending = next_round
    return ordered


def _registration_proxy_candidates(proxy_pool, fallback=None):
    candidates = []
    for item in proxy_pool or []:
        value = normalize_proxy_url(str(item or "").strip())
        if value:
            candidates.append(value)
    fallback = normalize_proxy_url(str(fallback or "").strip())
    if fallback and fallback not in candidates:
        candidates.insert(0, fallback)
    return list(dict.fromkeys(candidates))


def select_registration_proxy_pool(proxy_pool, fallback=None):
    candidates = _registration_proxy_candidates(proxy_pool, fallback)
    # Opt-in dynamic generation: rotate a provider session, screen it and require
    # the configured ASN before the batch uses it. Off by default; a generation
    # failure falls back to the static pool instead of consuming mailboxes.
    #
    # The outcome is attached to the returned pool so the per-account audit record
    # can say whether generation ran: a switch that is on but generated nothing
    # used to be indistinguishable from a switch that is off (both printed one
    # line and both fell back), which is the one thing an operator cannot see
    # from a finished batch.
    generation_status: dict[str, Any] = {"used": False, "count": 0, "reason": "disabled"}
    try:
        from ..proxy.dynamic import generate_pool_report

        outcome = generate_pool_report(CFG)
        generation_status = outcome.status.to_dict()
        if outcome:
            safe_print(f"[*] Dynamic proxy generation: {len(outcome)} screened exit(s)")
            generated_pool = RegistrationProxyPool(outcome)
            generated_pool.dynamic_generation = generation_status
            return generated_pool
        safe_print(
            "[!] Dynamic proxy generation produced no eligible exit "
            f"({generation_status.get('reason') or 'no_eligible_exit'}); using the static pool"
        )
    except Exception as exc:
        generation_status = {"used": False, "count": 0, "reason": f"error:{type(exc).__name__}"}
        safe_print(f"[!] Dynamic proxy generation unavailable ({type(exc).__name__}); using the static pool")
    selected = _select_static_registration_pool(candidates)
    selected.dynamic_generation = generation_status
    return selected


def _select_static_registration_pool(candidates: list[str]) -> RegistrationProxyPool:
    """Health-rank and preflight-probe the configured pool (the pre-existing path)."""
    if len(candidates) <= 1:
        return RegistrationProxyPool(candidates)

    tracker = ProxyHealthTracker(CFG)
    candidates = _interleave_proxy_endpoints(tracker.rank(candidates))

    def check(base: str) -> dict:
        candidate = refresh_proxy_sid(base)
        expected = infer_proxy_country(candidate)
        return probe_proxy_with_scheme_detection(candidate, expected, use_cache=True)

    # Serial probing made batch start-up delay grow linearly with pool size;
    # probe concurrently instead. executor.map preserves candidate order and
    # the probe cache is lock-guarded for concurrent workers.
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        outcomes = list(executor.map(check, candidates))
    healthy = []
    actual_countries = {}
    for base, checked in zip(candidates, outcomes):
        ok = bool(checked.get("ok"))
        tracker.record(base, ok=ok, error="proxy_preflight_failed" if not ok else "")
        if ok:
            healthy.append(base)
            actual = str(checked.get("country_code") or "").strip().upper()
            if actual:
                actual_countries[base] = actual
    ranked = tracker.rank(healthy or candidates)
    selected = _interleave_proxy_endpoints(ranked)
    # Opt-in ASN round-robin (proxy.asn_allocation.enabled, default off). It only
    # reorders what the health ranking already produced and passes the list
    # through untouched when no ASN was measured, so an existing pool's
    # behaviour is unchanged until an operator turns the ledger on.
    #
    # Imported here, not at module scope: ``scripts/import_layer_ratchet.py``
    # freezes the root -> proxy edge count and this module already carries the
    # health tracker's edge.
    from ..proxy.asn_registry import order_candidates

    selected = order_candidates(selected, config=CFG)
    return RegistrationProxyPool(
        selected,
        actual_countries={base: actual_countries[base] for base in selected if base in actual_countries},
    )


def select_registration_proxy_base(proxy_pool, fallback=None):
    candidates = select_registration_proxy_pool(proxy_pool, fallback)
    return candidates[0] if candidates else str(fallback or "").strip()


def _record_attempt_proxy_outcome(
    worker_proxy: str | None,
    result: dict[str, Any],
    failure_class: str,
    proxy_attributed: bool,
) -> None:
    """Record one attempt against the egress health journal and the ASN ledger.

    Only transport failures are evidence about the selected proxy.
    Internal/configuration/account/mailbox failures must not poison the shared
    proxy-health journal (or the ASN hit rate, which uses the same attribution
    rule). Extracted from the attempt body so the batch function does not grow
    an extra branch per bookkeeping destination.
    """
    success = bool(result.get("success"))
    if not worker_proxy or not (success or proxy_attributed):
        return
    from ..proxy.asn_registry import record_registration_outcome

    tracker = ProxyHealthTracker(CFG)
    tracker.record(
        worker_proxy,
        ok=success,
        error=str(result.get("error") or failure_class or "")[:120],
    )
    # No-op when no geo probe ever measured this exit (cached_asn_of reads the
    # shared geo cache only).
    record_registration_outcome(worker_proxy, ok=success, config=CFG)


def _hold_environment_lease(worker_proxy: str | None, *, account_ref: str, batch_id: str) -> dict:
    """Take a time-boxed lease on the egress this attempt is about to use.

    Advisory, never blocking.  A registration must not be refused because of
    bookkeeping, and with ten exits a batch of fifty has to share them -- so
    ``allow_reuse`` records the sharing (holder count + reason) rather than
    failing.  The point is that the sharing becomes visible, which it never was.

    The lease covers one ATTEMPT, not the whole account: ``refresh_proxy_sid``
    mints a new sticky session per attempt, so the exit that goes on the wire
    differs between attempts and an account-lifetime lease would be pinned to the
    wrong key from the second attempt on.
    """
    try:
        return acquire_environment_lease(
            exit_key=proxy_egress_key(worker_proxy),
            account_ref=account_ref,
            batch_id=batch_id,
            allow_reuse=True,
        )
    except Exception:
        # The ledger is an observation channel; it must never take a batch down.
        return {}


def _release_environment_lease(lease, result, worker_proxy: str | None) -> None:
    """Attach what the wire actually showed, then let the egress go.

    The profile is read from the result rather than passed in: the driver picks it
    inside the attempt (``registration_handlers`` sets ``auth_fingerprint_profile``
    from the bound fingerprint), so it is not knowable before the call.  Both
    observations are best-effort -- the request has already gone out by now, so a
    bookkeeping failure must not turn a success into a failure.
    """
    if not isinstance(lease, dict) or not lease.get("lease_id"):
        return
    _raw_payload: object = result
    payload: dict = _raw_payload if isinstance(_raw_payload, dict) else {}
    lease_id = int(lease["lease_id"])
    try:
        record_environment_observations(
            lease_id,
            exit_ip=_cached_exit_ip(worker_proxy),
            fingerprint_key=str(payload.get("auth_fingerprint_profile") or ""),
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        _LOGGER.debug("batch runner bookkeeping skipped: %s", exc)
    reason = "registered" if payload.get("success") else str(payload.get("failure_class") or "failed")
    try:
        release_environment_lease(lease_id, reason=reason[:60])
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        _LOGGER.debug("batch runner bookkeeping skipped: %s", exc)


def _registered_alias_bases():
    """Base addresses that already hold at least one account row."""
    try:
        rows = list_account_records()
    except Exception:
        # An unreadable database must not silently reorder a batch.
        return set()
    bases = set()
    for row in rows or []:
        base = _alias_base_email((row or {}).get("email"))
        if base:
            bases.add(base)
    return bases


def _drop_already_registered(mailboxes):
    """Remove mailboxes that already have a *registered* account row.

    Measured 2026-09-14 on this install: ``mailbox_tokens.txt`` held 791
    addresses and 751 of them (94.9%) were already ``status='registered'`` in
    ``accounts.sqlite3``.  Signing one of those up again cannot succeed -- the
    server answers ``user_already_exists`` -- but the signup lane only learns
    that *after* spending an email OTP on the way to ``/about-you``, so every
    such address is a burned OTP that could never have produced an account.

    Skipping is the default because it cannot lose a signup that would
    otherwise have succeeded.  Set ``registration.skip_registered_mailboxes``
    to false to attempt them anyway (for example to deliberately re-drive a
    half-finished account).

    Returns ``(kept, skipped_registered, skipped_dead_end)``.  A lookup failure
    keeps the original list: an unreadable account database must not silently
    empty a batch.

    The two skip reasons are reported separately because they need different
    operator actions.  A ``registered`` row means the account exists in *our*
    storage.  A dead end means the **server** already told us the address is
    taken while we hold no credentials for it -- which is why it is absent from
    ``accounts.sqlite3`` and why the database lookup cannot see it at all.  The
    retry guard is the only place that remembers those.

    **Alias conflicts are reordered, never skipped.**  Measured 2026-09-15: of
    that day's 305 ``user_already_exists`` answers, 292 (96%) were aliases whose
    base already held an account -- so a base conflict is a strong hint.  It is
    *not* proof: 99 aliases on this install did register successfully against an
    already-occupied base, so dropping them would lose real signups.  They move
    behind the fresh addresses instead, where a scarce worker/proxy slot is
    better spent.  Set ``registration.deprioritize_base_conflicts`` to false to
    keep the caller's original order.
    """
    items = list(mailboxes or [])
    if not items:
        return items, [], []
    _raw_registration_cfg: object = CFG.get("registration")
    registration_cfg: dict = _raw_registration_cfg if isinstance(_raw_registration_cfg, dict) else {}
    skip_registered = registration_cfg.get("skip_registered_mailboxes", True) not in (
        False,
        0,
        "0",
        "false",
        "False",
        "no",
    )
    if not skip_registered:
        return items, [], []
    deprioritize = registration_cfg.get("deprioritize_base_conflicts", True) not in (
        False,
        0,
        "0",
        "false",
        "False",
        "no",
    )

    emails = [str(getattr(mailbox, "email", "") or "").strip() for mailbox in items]
    try:
        raw = get_account_records([email for email in emails if email])
    except Exception:
        raw = {}
    records = {str(key).strip().lower(): value for key, value in (raw or {}).items()}
    try:
        retry_guard = RegistrationRetryGuard(CFG)
        blocked_states = retry_guard.blocked_email_states()
    except Exception:
        retry_guard = None
        blocked_states = {}
    try:
        checkpoints = get_registration_checkpoints([email for email in emails if email])
    except Exception:
        checkpoints = {}
    from .checkpoint import candidate_checkpoint_error

    checkpoint_errors = {
        email: error for email, checkpoint in checkpoints.items() if (error := candidate_checkpoint_error(checkpoint))
    }
    if retry_guard is not None:
        for checkpoint_email, checkpoint_error in checkpoint_errors.items():
            try:
                retry_guard.record(
                    checkpoint_email,
                    failure_class="auth_state",
                    error=checkpoint_error,
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                _LOGGER.debug("batch runner bookkeeping skipped: %s", exc)
    alias_bases = _registered_alias_bases() if deprioritize else set()
    kept, skipped, skipped_dead, deferred = [], [], [], []
    for mailbox, email in zip(items, emails):
        normalized = email.lower()
        record = records.get(normalized) or {}
        blocked_reason = blocked_states.get(normalized, "")
        status = mailbox_registration_status(record, known_partial=blocked_reason == "dead_end")
        if status == "registered":
            skipped.append(email)
        elif status == "partial_registered" or blocked_reason or normalized in checkpoint_errors:
            skipped_dead.append(email)
        else:
            base = _alias_base_email(normalized)
            # Only an *alias* can conflict with its own base: a plain address
            # that already holds a row was caught by the ``registered`` branch.
            if base and base != normalized and base in alias_bases:
                deferred.append(mailbox)
            else:
                kept.append(mailbox)
    return kept + deferred, skipped, skipped_dead


def filter_registered_mailboxes(mailboxes):
    """Drop already-registered mailboxes *and report it*, for callers that size or bill a batch."""
    kept, skipped, skipped_dead = _drop_already_registered(mailboxes)
    if skipped:
        _announce_skipped(skipped)
    if skipped_dead:
        _announce_dead_end(skipped_dead)
    return kept


def _start_batch_sentinel_prewarm(
    email_cfg: dict[str, Any],
    *,
    backend: str,
    registration_driver: str,
    workers: int,
    count: int,
    proxy_pool: Any,
    proxy: str | None,
) -> tuple[ThreadPoolExecutor | None, dict[int, Any], dict[Any, Any]]:
    try:
        prewarm_window = max(0, min(int(email_cfg.get("sentinel_prewarm_window") or 0), workers, count))
    except (TypeError, ValueError):
        prewarm_window = 0
    if backend != "legacy" or registration_driver != "protocol":
        prewarm_window = 0
    prewarm_executor = None
    prewarmed: dict[int, Any] = {}
    first_attempt_proxies: dict[Any, Any] = {}
    if prewarm_window:
        from .sentinel_tokens import extract_sentinel, sentinel_max_concurrency

        prewarm_executor = ThreadPoolExecutor(max_workers=min(prewarm_window, sentinel_max_concurrency()))
        for index in range(prewarm_window):
            base_proxy = proxy_pool[index % len(proxy_pool)] if proxy_pool else proxy
            worker_proxy = refresh_proxy_sid(base_proxy) if base_proxy else base_proxy
            first_attempt_proxies[index] = (base_proxy, worker_proxy)
            prewarmed[index] = prewarm_executor.submit(
                extract_sentinel,
                proxy=worker_proxy,
                force_fresh=True,
                persist=False,
            )
    return prewarm_executor, prewarmed, first_attempt_proxies


def _invoke_batch_email_attempt(
    *,
    account_index: int,
    attempt: int,
    account_proxy_index: int,
    account_rotation_generation: int,
    proxy_pool: Any,
    proxy: str | None,
    first_attempt_proxies: dict[Any, Any],
    pool_indices: dict[str, int],
    preflight_actual_countries: dict[str, str],
    prewarmed_sentinel_fn: Any,
    mailbox: Any,
    mailbox_email: str,
    phone_pool: Any,
    codex_oauth: bool,
    registration_mode: Any,
    browser_headless: bool | None,
    enroll_2fa: bool,
    batch_id: str,
    explicit_registration_driver: bool,
    registration_driver: str,
    run_email_func: Any,
) -> dict[str, Any]:
    base_proxy = proxy_pool[account_proxy_index] if proxy_pool else proxy
    prewarm = first_attempt_proxies.get(account_index) if attempt == 1 else None
    use_prewarm = bool(prewarm and prewarm[0] == base_proxy)
    worker_proxy = (
        prewarm[1]
        if use_prewarm and prewarm is not None
        else (refresh_proxy_sid(base_proxy) if base_proxy else base_proxy)
    )
    expected_country = infer_proxy_country(worker_proxy or "")
    proxy_metadata = _registration_proxy_metadata(
        worker_proxy,
        pool_index=pool_indices.get(base_proxy or "", account_index % len(proxy_pool) if proxy_pool else -1),
        expected_country=expected_country,
        actual_country=preflight_actual_countries.get(base_proxy or "", ""),
        dynamic_generation=getattr(proxy_pool, "dynamic_generation", None),
    )
    proxy_metadata["attempt"] = attempt
    proxy_metadata["rotation_generation"] = account_rotation_generation
    sentinel_data = prewarmed_sentinel_fn(account_index) if use_prewarm else None
    environment_lease = _hold_environment_lease(worker_proxy, account_ref=mailbox_email, batch_id=batch_id)
    try:
        call_kwargs: dict[str, Any] = dict(
            proxy=worker_proxy,
            mailbox=mailbox,
            phone_pool=phone_pool,
            codex_oauth=codex_oauth,
            sentinel_data=sentinel_data,
            registration_mode=registration_mode,
            browser_headless=browser_headless,
            enroll_2fa=enroll_2fa,
            batch_id=batch_id,
            registration_attempt=attempt,
        )
        if explicit_registration_driver or registration_driver != "protocol":
            call_kwargs["registration_driver"] = registration_driver
        call_kwargs["proxy_metadata"] = proxy_metadata
        result = run_email_func(**call_kwargs)
    except Exception as e:
        safe_error = sanitize_text(f"{type(e).__name__}: {e}")
        safe_print(f"[!] Registration worker failed: {safe_error[:500]}")
        failure_class = classify_error(str(e))
        result = {
            "success": False,
            "error": safe_error,
            "failure_class": failure_class,
            "dropped": True
            if failure_class in BATCH_DROPPED_CLASSES
            else False
            if failure_class in BATCH_RETRY_CLASSES
            else None,
        }
    if not isinstance(result, dict):
        result = {"success": False, "error": "invalid_registration_result", "failure_class": "unknown"}
    _release_environment_lease(environment_lease, result, worker_proxy)
    failure_class = str(result.get("failure_class") or "").strip().lower()
    if not failure_class and not result.get("success"):
        failure_class = classify_error(result)
        result["failure_class"] = failure_class
    proxy_attributed = bool(result.get("proxy_attributed")) or failure_class == "network"
    _record_attempt_proxy_outcome(worker_proxy, result, failure_class, proxy_attributed)
    result["registration_attempts"] = attempt
    result["proxy_rotation_count"] = account_rotation_generation
    result["proxy_session_refresh_count"] = attempt
    result.setdefault("proxy_audit", safe_proxy_audit(proxy_metadata))
    result["batch_id"] = batch_id
    return result


def _prepare_batch_proxy_pool_and_start_event(
    proxy_pool: Any,
    proxy: str | None,
    *,
    batch_id: str,
    count: int,
) -> tuple[Any, str | None, dict[str, str], dict[str, int], Any]:
    normalized_pool = [normalize_proxy_url(str(item or "").strip()) for item in (proxy_pool or [])]
    normalized_pool = list(dict.fromkeys(item for item in normalized_pool if item))
    normalized_proxy = normalize_proxy_url(str(proxy or "").strip()) or None
    if normalized_proxy and normalized_proxy not in normalized_pool:
        normalized_pool.insert(0, normalized_proxy)
    if not normalized_pool and normalized_proxy:
        normalized_pool = [normalized_proxy]
    try:
        from ..desktop.ipc import emit_event

        emit_event(
            {
                "domain": "registration",
                "batch_id": batch_id,
                "operation": "registration",
                "stage": "batch_started",
                "status": "running",
                "total": int(count or 0),
            }
        )
    except Exception:
        emit_event = None
    original_pool = list(normalized_pool)
    selected_pool = select_registration_proxy_pool(normalized_pool, normalized_proxy)
    preflight_actual_countries = dict(getattr(selected_pool, "actual_countries", {}) or {})
    pool_indices = {value: index for index, value in enumerate(original_pool)}
    resolved_proxy = selected_pool[0] if selected_pool else normalized_proxy
    return selected_pool, resolved_proxy, preflight_actual_countries, pool_indices, emit_event


def _execute_one_batch_account(
    i: int,
    *,
    count: int,
    mailboxes: Any,
    cancel_event: Any,
    breaker: BatchCircuitBreaker,
    retry_guard: RegistrationRetryGuard,
    proxy_pool: Any,
    proxy: str | None,
    proxy_pool_offset: int,
    proxy_rotation_generation: int,
    max_attempts: int,
    retry_delay_seconds: float,
    first_attempt_proxies: dict[Any, Any],
    pool_indices: dict[str, int],
    preflight_actual_countries: dict[str, str],
    prewarmed_sentinel_fn: Any,
    phone_pool: Any,
    codex_oauth: bool,
    registration_mode: Any,
    browser_headless: bool | None,
    enroll_2fa: bool,
    batch_id: str,
    explicit_registration_driver: bool,
    registration_driver: str,
    run_email_func: Any,
    emit_event: Any,
) -> tuple[int, dict[str, Any]]:
    from .cancel import registration_cancel_requested

    def _cancel_requested():
        return (cancel_event is not None and cancel_event.is_set()) or registration_cancel_requested()

    if _cancel_requested():
        return i, _cancelled_batch_result()
    if breaker.tripped:
        _parked_mailbox = mailboxes[i] if mailboxes else None
        return i, {
            "success": False,
            "email": str(getattr(_parked_mailbox, "email", "") or "").strip(),
            "error": "batch_circuit_breaker_open",
            "failure_class": "environment",
            "retryable": True,
            "dropped": False,
            "deferred": True,
            "registration_state": "batch_paused",
            "registration_attempts": 0,
            "batch_id": batch_id,
        }
    safe_print(f"\n{'#' * 40}")
    safe_print(f"  Account {i + 1}/{count}")
    safe_print(f"{'#' * 40}")
    mailbox = mailboxes[i] if mailboxes else None
    mailbox_email = str(getattr(mailbox, "email", "") or "").strip()
    guard_verdict = _check_batch_account_guard_state(retry_guard, mailbox_email)
    if guard_verdict is not None:
        return i, guard_verdict
    account_proxy_index = (i + proxy_pool_offset) % len(proxy_pool) if proxy_pool else 0
    account_rotation_generation = proxy_rotation_generation
    result: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        if _cancel_requested():
            return i, _cancelled_batch_result(attempt - 1)
        result = _invoke_batch_email_attempt(
            account_index=i,
            attempt=attempt,
            account_proxy_index=account_proxy_index,
            account_rotation_generation=account_rotation_generation,
            proxy_pool=proxy_pool,
            proxy=proxy,
            first_attempt_proxies=first_attempt_proxies,
            pool_indices=pool_indices,
            preflight_actual_countries=preflight_actual_countries,
            prewarmed_sentinel_fn=prewarmed_sentinel_fn,
            mailbox=mailbox,
            mailbox_email=mailbox_email,
            phone_pool=phone_pool,
            codex_oauth=codex_oauth,
            registration_mode=registration_mode,
            browser_headless=browser_headless,
            enroll_2fa=enroll_2fa,
            batch_id=batch_id,
            explicit_registration_driver=explicit_registration_driver,
            registration_driver=registration_driver,
            run_email_func=run_email_func,
        )
        done, final_result = _evaluate_batch_attempt_verdict(
            result,
            account_index=i,
            attempt=attempt,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            mailbox_email=mailbox_email,
            retry_guard=retry_guard,
            breaker=breaker,
            emit_event=emit_event,
            batch_id=batch_id,
            count=count,
            cancel_requested_fn=_cancel_requested,
        )
        if done:
            return i, final_result
    return i, result


def ensure_batch_sentinel_readiness(config, registration_driver: str) -> str:
    return _ensure_batch_sentinel_readiness_impl(config, registration_driver, readiness_fn=check_node_runner_readiness)


def run_batch_impl(
    count=1,
    proxy=None,
    proxy_pool=None,
    mailboxes=None,
    workers=4,
    phone_pool=None,
    codex_oauth=False,
    registration_mode=None,
    max_attempts=2,
    retry_delay_seconds=1.0,
    run_email_func=None,
    browser_headless: bool | None = None,
    enroll_2fa: bool = True,
    on_result=None,
    registration_driver: str | None = None,
    cancel_event=None,
    target_success: int | None = None,
):
    if run_email_func is None:
        raise ValueError("run_email_func is required")
    from ..registration_drivers.base import normalize_registration_driver

    explicit_registration_driver = registration_driver is not None
    registration_driver = normalize_registration_driver(registration_driver, CFG)
    _raw_email_cfg: object = CFG.get("email_registration")
    email_cfg: dict = _raw_email_cfg if isinstance(_raw_email_cfg, dict) else {}
    backend = ensure_batch_sentinel_readiness(CFG, registration_driver)
    mailboxes = _unique_mailboxes(mailboxes)
    mailboxes, skipped_registered, skipped_dead = _drop_already_registered(mailboxes)
    if skipped_registered:
        _announce_skipped(skipped_registered)
    if skipped_dead:
        _announce_dead_end(skipped_dead)
    if (skipped_registered or skipped_dead) and not mailboxes:
        safe_print(
            "[!] Every available mailbox is already registered or a known dead end; nothing to sign up. "
            "Add fresh mailboxes, or set registration.skip_registered_mailboxes=false to attempt them anyway."
        )
        return []
    batch_id = uuid.uuid4().hex
    proxy_pool, proxy, preflight_actual_countries, pool_indices, emit_event = _prepare_batch_proxy_pool_and_start_event(
        proxy_pool, proxy, batch_id=batch_id, count=count
    )
    if mailboxes and int(count or 1) > len(mailboxes):
        safe_print(
            f"[!] Requested {count} account(s), but only {len(mailboxes)} unique mailbox(es) are available; capping batch size."
        )
        count = len(mailboxes)
    progress_lock = threading.Lock()
    completed_count = success_count = 0
    target_goal = max(0, int(target_success or 0))
    target_event = threading.Event()
    retry_guard = RegistrationRetryGuard(CFG)
    _raw_registration_cfg: object = CFG.get("registration")
    registration_cfg: dict = _raw_registration_cfg if isinstance(_raw_registration_cfg, dict) else {}
    workers, breaker = _resolve_batch_workers_and_breaker(
        registration_cfg,
        registration_driver=registration_driver,
        workers=workers,
        count=count,
    )
    safe_print(f"\n{'=' * 60}")
    safe_print(f"  ChatGPT Email Batch Registration - {count} accounts")
    safe_print(f"{'=' * 60}\n")
    max_attempts = max(1, min(int(max_attempts or 1), 3))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds or 0.0))
    prewarm_executor, prewarmed, first_attempt_proxies = _start_batch_sentinel_prewarm(
        email_cfg,
        backend=backend,
        registration_driver=registration_driver,
        workers=workers,
        count=count,
        proxy_pool=proxy_pool,
        proxy=proxy,
    )
    proxy_pool_offset = 0
    proxy_rotation_generation = 0

    def _prewarmed_sentinel(index):
        future = prewarmed.get(index)
        if future is None:
            return None
        try:
            return future.result()
        except Exception:
            return None

    def _run_one(i):
        return _execute_one_batch_account(
            i,
            count=count,
            mailboxes=mailboxes,
            cancel_event=cancel_event,
            breaker=breaker,
            retry_guard=retry_guard,
            proxy_pool=proxy_pool,
            proxy=proxy,
            proxy_pool_offset=proxy_pool_offset,
            proxy_rotation_generation=proxy_rotation_generation,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            first_attempt_proxies=first_attempt_proxies,
            pool_indices=pool_indices,
            preflight_actual_countries=preflight_actual_countries,
            prewarmed_sentinel_fn=_prewarmed_sentinel,
            phone_pool=phone_pool,
            codex_oauth=codex_oauth,
            registration_mode=registration_mode,
            browser_headless=browser_headless,
            enroll_2fa=enroll_2fa,
            batch_id=batch_id,
            explicit_registration_driver=explicit_registration_driver,
            registration_driver=registration_driver,
            run_email_func=run_email_func,
            emit_event=emit_event,
        )

    def _rotate_proxy_pool_cursor() -> bool:
        nonlocal proxy_pool_offset, proxy_rotation_generation
        if len(proxy_pool) <= 1:
            return False
        proxy_pool_offset = (proxy_pool_offset + 1) % len(proxy_pool)
        proxy_rotation_generation += 1
        return True

    def _notify_result(index, result):
        nonlocal completed_count, success_count
        reached_now = False
        with progress_lock:
            completed_count += 1
            done = completed_count
            if result.get("success"):
                success_count += 1
                if target_goal > 0 and success_count >= target_goal and not target_event.is_set():
                    target_event.set()
                    reached_now = True
            current_success = success_count
        if reached_now:
            safe_print(
                f"[*] Target reached ({current_success}/{target_goal} registered); "
                "stopping before consuming remaining mailboxes."
            )
        _emit_account_completion_events(
            emit_event,
            batch_id=batch_id,
            result=result,
            done=done,
            count=count,
            index=index,
            on_result=on_result,
        )

    def _current_success_count() -> int:
        with progress_lock:
            return success_count

    return _dispatch_batch_execution(
        count=count,
        workers=workers,
        registration_driver=registration_driver,
        run_one_fn=_run_one,
        notify_fn=_notify_result,
        target_goal=target_goal,
        target_event=target_event,
        cancel_event=cancel_event,
        rotate_proxy_fn=_rotate_proxy_pool_cursor,
        current_success_count_fn=_current_success_count,
        breaker=breaker,
        prewarm_executor=prewarm_executor,
        emit_event=emit_event,
        batch_id=batch_id,
        config=CFG,
    )
