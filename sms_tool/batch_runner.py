from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import uuid
import threading

from .batch_circuit_breaker import BatchCircuitBreaker
from .error_classification import classify_error
from .config import CFG
from .paypal_proxy import infer_proxy_country
from .phone_proxy import normalize_proxy_url, probe_proxy_with_scheme_detection, refresh_proxy_sid
from .sanitizer import sanitize_text
from .sanitizer import account_reference, mask_account
from .diagnostics import safe_print
from .proxy_health import ProxyHealthTracker
from .registration_retry_guard import RegistrationRetryGuard
from .registration_policy import registration_retry_decision


def _registration_proxy_candidates(proxy_pool, fallback=None):
    candidates = []
    for item in (proxy_pool or []):
        value = normalize_proxy_url(str(item or "").strip())
        if value:
            candidates.append(value)
    fallback = normalize_proxy_url(str(fallback or "").strip())
    if fallback and fallback not in candidates:
        candidates.insert(0, fallback)
    return list(dict.fromkeys(candidates))


def select_registration_proxy_pool(proxy_pool, fallback=None):
    candidates = _registration_proxy_candidates(proxy_pool, fallback)
    if len(candidates) <= 1:
        return candidates

    tracker = ProxyHealthTracker(CFG)
    candidates = tracker.rank(candidates)

    def check(base: str) -> bool:
        candidate = refresh_proxy_sid(base)
        expected = infer_proxy_country(candidate)
        checked = probe_proxy_with_scheme_detection(candidate, expected, use_cache=True)
        return bool(checked.get("ok"))

    # Serial probing made batch start-up delay grow linearly with pool size;
    # probe concurrently instead. executor.map preserves candidate order and
    # the probe cache is lock-guarded for concurrent workers.
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        outcomes = list(executor.map(check, candidates))
    healthy = []
    for base, ok in zip(candidates, outcomes):
        tracker.record(base, ok=ok, error="proxy_preflight_failed" if not ok else "")
        if ok:
            healthy.append(base)
    return tracker.rank(healthy or candidates)


def select_registration_proxy_base(proxy_pool, fallback=None):
    candidates = select_registration_proxy_pool(proxy_pool, fallback)
    return candidates[0] if candidates else str(fallback or "").strip()


def _registration_proxy_metadata(proxy: str | None, *, pool_index: int, expected_country: str = "") -> dict:
    """Return audit-safe proxy selection metadata without URL credentials."""
    from urllib.parse import urlsplit

    parsed = urlsplit(str(proxy or ""))
    return {
        "pool_index": int(pool_index) if int(pool_index) >= 0 else -1,
        "expected_country": str(expected_country or "").strip().upper(),
        "actual_country": "",
        "scheme": str(parsed.scheme or "").strip().lower(),
    }


def _unique_mailboxes(mailboxes):
    if not mailboxes:
        return []
    unique = []
    seen = set()
    for mailbox in mailboxes:
        email = str(getattr(mailbox, "email", "") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        unique.append(mailbox)
    return unique


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
):
    if run_email_func is None:
        raise ValueError("run_email_func is required")
    from .registration_drivers.base import normalize_registration_driver
    explicit_registration_driver = registration_driver is not None
    registration_driver = normalize_registration_driver(registration_driver, CFG)
    mailboxes = _unique_mailboxes(mailboxes)
    proxy_pool = [normalize_proxy_url(str(item or "").strip()) for item in (proxy_pool or [])]
    proxy_pool = list(dict.fromkeys(item for item in proxy_pool if item))
    proxy = normalize_proxy_url(str(proxy or "").strip()) or None
    if proxy and proxy not in proxy_pool:
        proxy_pool.insert(0, proxy)
    if not proxy_pool and proxy:
        proxy_pool = [proxy]
    batch_id = uuid.uuid4().hex
    try:
        from .desktop_ipc import emit_event
        emit_event({"domain": "registration", "batch_id": batch_id, "operation": "registration", "stage": "batch_started", "status": "running", "total": int(count or 0)})
    except Exception:
        emit_event = None
    original_pool = list(proxy_pool)
    proxy_pool = select_registration_proxy_pool(proxy_pool, proxy)
    pool_indices = {value: index for index, value in enumerate(original_pool)}
    proxy = proxy_pool[0] if proxy_pool else proxy
    if mailboxes and int(count or 1) > len(mailboxes):
        safe_print(f"[!] Requested {count} account(s), but only {len(mailboxes)} unique mailbox(es) are available; capping batch size.")
        count = len(mailboxes)
    results = []
    progress_lock = threading.Lock()
    completed_count = 0
    retry_guard = RegistrationRetryGuard(CFG)
    registration_cfg = CFG.get("registration") if isinstance(CFG.get("registration"), dict) else {}
    # P2-2: whole-batch stop for environment failures (proxy pool down, signup
    # page changed shape, ...). Neither the per-account retry guard nor the
    # per-proxy health tracker can see this pattern, so a dead environment used
    # to walk through the entire mailbox list before anyone noticed.
    try:
        breaker_threshold = max(1, int(registration_cfg.get("batch_circuit_breaker_threshold") or 3))
    except (TypeError, ValueError):
        breaker_threshold = 3
    breaker_enabled = registration_cfg.get("batch_circuit_breaker_enabled", True) not in (
        False, 0, "0", "false", "False", "no",
    )
    breaker = BatchCircuitBreaker(breaker_threshold, enabled=breaker_enabled)
    safe_print(f"\n{'=' * 60}")
    safe_print(f"  ChatGPT Email Batch Registration - {count} accounts")
    safe_print(f"{'=' * 60}\n")

    workers = max(1, min(int(workers or 1), 20, int(count or 1)))
    if registration_driver != "protocol":
        # Headless contexts are expensive and the auth stage is intentionally
        # serialized by the registration gate. ``browser_worker_limit`` is now
        # an optional extra cap: unset or 0 follows the requested worker count
        # (already clamped to 1..8 above); a positive value still wins.
        raw_limit = registration_cfg.get("browser_worker_limit")
        browser_limit = 0
        if raw_limit not in (None, "", 0, "0"):
            try:
                browser_limit = max(1, min(int(raw_limit), 8))
            except (TypeError, ValueError):
                browser_limit = 0
        if browser_limit and workers > browser_limit:
            safe_print(f"[*] Browser worker limit: {workers} -> {browser_limit}")
            workers = browser_limit
    max_attempts = max(1, min(int(max_attempts or 1), 3))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds or 0.0))

    email_cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    try:
        prewarm_window = max(0, min(int(email_cfg.get("sentinel_prewarm_window") or 0), workers, count))
    except (TypeError, ValueError):
        prewarm_window = 0
    from .sentinel import sentinel_backend

    if sentinel_backend({"email_registration": email_cfg}) != "legacy":
        prewarm_window = 0
    prewarm_executor = None
    prewarmed = {}
    first_attempt_proxies = {}
    if registration_driver != "protocol":
        prewarm_window = 0
    if prewarm_window:
        from .sentinel_tokens import _extract_sentinel, _sentinel_max_concurrency

        prewarm_executor = ThreadPoolExecutor(max_workers=min(prewarm_window, _sentinel_max_concurrency()))
        for index in range(prewarm_window):
            base_proxy = proxy_pool[index % len(proxy_pool)] if proxy_pool else proxy
            worker_proxy = refresh_proxy_sid(base_proxy) if base_proxy else base_proxy
            first_attempt_proxies[index] = worker_proxy
            prewarmed[index] = prewarm_executor.submit(
                _extract_sentinel, proxy=worker_proxy, force_fresh=True, persist=False,
            )

    def _prewarmed_sentinel(index):
        future = prewarmed.get(index)
        if future is None:
            return None
        try:
            return future.result()
        except Exception:
            return None

    def _run_one(i):
        from .registration_cancel import registration_cancel_requested

        def _cancelled(attempt: int = 0):
            return {
                "success": False,
                "error": "registration_cancelled",
                "failure_class": "cancelled",
                "retryable": False,
                "dropped": False,
                "registration_state": "cancelled",
                "registration_attempts": int(attempt or 0),
            }

        def _cancel_requested():
            return (cancel_event is not None and cancel_event.is_set()) or registration_cancel_requested()

        if _cancel_requested():
            return i, _cancelled()
        if breaker.tripped:
            # Parked, not attempted -- this mailbox is NOT consumed.
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
        guard_state = retry_guard.check(mailbox_email)
        if guard_state.get("deferred"):
            return i, {
                "success": False,
                "email": mailbox_email,
                "error": "registration_retry_cooldown",
                "failure_class": "auth_state",
                "retryable": True,
                "deferred": True,
                "retry_after_seconds": int(guard_state.get("remaining_seconds") or 0),
                "registration_state": "retry_pending",
            }
        # Pin each account to a stable proxy egress for its entire lifetime.
        # Previously the index shifted on every retry (proxy_pool[(i+attempt-1)
        # % n]), which rotated the egress on each retry and looked like proxy
        # churn to registrars -- a ban trigger.  Retries now keep the same
        # egress and only refresh the session id (see refresh_proxy_sid below).
        account_proxy_index = i % len(proxy_pool) if proxy_pool else 0
        for attempt in range(1, max_attempts + 1):
            if _cancel_requested():
                return i, _cancelled(attempt - 1)
            base_proxy = proxy_pool[account_proxy_index] if proxy_pool else proxy
            worker_proxy = (
                first_attempt_proxies[i]
                if attempt == 1 and i in first_attempt_proxies
                else (refresh_proxy_sid(base_proxy) if base_proxy else base_proxy)
            )
            expected_country = infer_proxy_country(worker_proxy)
            proxy_metadata = _registration_proxy_metadata(
                worker_proxy,
                pool_index=pool_indices.get(base_proxy, i % len(proxy_pool) if proxy_pool else -1),
                expected_country=expected_country,
            )
            proxy_metadata["attempt"] = attempt
            sentinel_data = _prewarmed_sentinel(i) if attempt == 1 else None
            try:
                call_kwargs = dict(
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
                if registration_driver != "protocol":
                    call_kwargs["proxy_metadata"] = proxy_metadata
                result = run_email_func(**call_kwargs)
            except Exception as e:
                # Worker exceptions may contain proxy credentials or tokens.
                # Keep operator output useful without emitting the raw exception
                # or traceback into WPF/CLI logs.
                safe_error = sanitize_text(f"{type(e).__name__}: {e}")
                safe_print(f"[!] Registration worker failed: {safe_error[:500]}")
                failure_class = classify_error(str(e))
                result = {
                    "success": False,
                    "error": safe_error,
                    "failure_class": failure_class,
                    "dropped": True if failure_class == "account" else False if failure_class in {"network", "mailbox", "auth_state"} else None,
                }
            if not isinstance(result, dict):
                result = {"success": False, "error": "invalid_registration_result", "failure_class": "unknown"}
            tracker = ProxyHealthTracker(CFG)
            tracker.record(
                worker_proxy,
                ok=bool(result.get("success")),
                error=str(result.get("error") or result.get("failure_class") or "")[:120],
            )
            result["registration_attempts"] = attempt
            result["proxy_rotation_count"] = max(0, attempt - 1)
            result["batch_id"] = batch_id
            if result.get("success", False):
                retry_guard.record(mailbox_email, success=True)
                breaker.record_success()
                return i, result
            result.setdefault("failure_class", classify_error(result))
            if result["failure_class"] in {"network", "mailbox", "auth_state", "rate_limit"}:
                result.setdefault("dropped", False)
            elif result["failure_class"] == "account":
                result.setdefault("dropped", True)
            # Only transport and auth-state failures are retried with a new
            # pool member. Rate limits and mailbox outcomes are terminal for
            # this account and must not consume another proxy.
            decision = registration_retry_decision(result, failure_class=result["failure_class"])
            if not decision.retryable or attempt >= max_attempts:
                result["retryable"] = decision.retryable
                result["error_advice"] = decision.advice
                retry_guard.record(
                    mailbox_email,
                    failure_class=result.get("failure_class"),
                    error=result.get("error"),
                    success=False,
                )
                # P2-2: only terminal per-account verdicts feed the breaker, so
                # the retries inside one account cannot inflate the streak.
                if breaker.record(result["failure_class"]):
                    safe_print(
                        f"[!] Batch paused after {breaker.consecutive} consecutive "
                        f"{result['failure_class']} failures -- remaining accounts "
                        f"are parked (mailboxes not consumed)."
                    )
                    if emit_event is not None:
                        try:
                            emit_event({
                                "domain": "registration", "batch_id": batch_id,
                                "operation": "registration", "stage": "batch_paused",
                                "status": "paused",
                                "failure_class": str(result.get("failure_class") or ""),
                                "consecutive": int(breaker.consecutive),
                                "total": int(count or 0),
                            })
                        except Exception:
                            pass
                return i, result
            safe_print(
                f"[!] Retryable {result['failure_class']} failure; "
                f"retrying account {i + 1} with a fresh proxy session "
                f"({attempt + 1}/{max_attempts})"
            )
            if retry_delay_seconds:
                from .registration_cancel import cancellable_sleep

                if cancellable_sleep(retry_delay_seconds, requested=_cancel_requested):
                    return i, _cancelled(attempt)
        return i, result

    def _notify_result(index, result):
        nonlocal completed_count
        with progress_lock:
            completed_count += 1
            done = completed_count
        if emit_event is not None:
            try:
                emit_event({
                    "domain": "registration", "batch_id": batch_id,
                    "account_ref": account_reference(result.get("email")),
                    "operation": "registration", "stage": "account_completed",
                    "status": "success" if result.get("success") else "failed",
                    "attempt": int(result.get("registration_attempts") or 0),
                })
                emit_event({
                    "domain": "registration",
                    "batch_id": batch_id,
                    "operation": "registration",
                    "stage": "batch_progress",
                    "status": "running" if done < int(count or 0) else "completed",
                    "completed": done,
                    "total": int(count or 0),
                    "success": bool(result.get("success")),
                    "failure_class": str(result.get("failure_class") or "")[:80],
                })
            except Exception:
                pass
        # One consistent per-account verdict line for the log panel; failures
        # are already reported by the persistence path with the error string.
        if result.get("success"):
            safe_print(
                f"[*] Account {index + 1}/{int(count or 0)} "
                f"{mask_account(result.get('email'))}: registered"
            )
        if on_result is None:
            return
        try:
            on_result(index, result)
        except Exception as exc:
            safe_print(
                f"[!] Result callback failed for account {index + 1}: "
                f"{type(exc).__name__}; batch continues."
            )

    def _close_browser_pool():
        if registration_driver == "protocol":
            return
        try:
            from .registration_drivers.browser_flow.flow_steps import close_browser_process_pool

            close_browser_process_pool()
        except Exception:
            pass

    # Graceful cooperative cancellation for the whole batch: the first
    # Ctrl+C sets the cancel event so workers wind down at bounded
    # checkpoints instead of dying mid-account; on exit the scope restores
    # the previous handlers and clears the flag so a cancellation can never
    # leak into a later batch in the same process.
    from .registration_cancel import cancel_scope

    with cancel_scope():
        if workers <= 1:
            try:
                for i in range(count):
                    _, result = _run_one(i)
                    results.append(result)
                    _notify_result(i, result)
                if emit_event is not None:
                    emit_event({"domain": "registration", "batch_id": batch_id, "operation": "registration", "stage": "batch_completed", "status": "completed", "total": len(results)})
                return results
            finally:
                if prewarm_executor is not None:
                    prewarm_executor.shutdown(wait=True)
                _close_browser_pool()

        # Pulse-wave scheduling: when enabled, split the batch into discrete
        # waves with IP-ban detection between waves.
        from .registration_pulse import PulseConfig, run_pulse_batch

        pulse_config = PulseConfig.from_config(CFG)
        if pulse_config.enabled:
            try:
                pulse_results = run_pulse_batch(
                    count,
                    run_one_fn=_run_one,
                    on_result=_notify_result,
                    workers=workers,
                    pulse_config=pulse_config,
                    cancel_event=cancel_event,
                )
                if emit_event is not None:
                    emit_event({"domain": "registration", "batch_id": batch_id, "operation": "registration", "stage": "batch_completed", "status": "completed", "total": len(pulse_results)})
                return pulse_results
            finally:
                # The all-at-once path shuts the prewarm pool down at the end of
                # the function; the pulse path returns early and used to leak the
                # executor threads for the rest of the process lifetime.
                if prewarm_executor is not None:
                    prewarm_executor.shutdown(wait=True)
                _close_browser_pool()

        ordered = [None] * count
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_run_one, i) for i in range(count)]
                for future in as_completed(futures):
                    i, result = future.result()
                    ordered[i] = result
                    _notify_result(i, result)
            results.extend(result for result in ordered if result is not None)
            if emit_event is not None:
                emit_event({"domain": "registration", "batch_id": batch_id, "operation": "registration", "stage": "batch_completed", "status": "completed", "total": len(results)})
            return results
        finally:
            if prewarm_executor is not None:
                prewarm_executor.shutdown(wait=True)
            _close_browser_pool()
