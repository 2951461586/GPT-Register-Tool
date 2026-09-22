from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import uuid
import threading
from urllib.parse import urlsplit

from .batch_circuit_breaker import BatchCircuitBreaker
from .error_classification import classify_error
from .failure_registry import BATCH_DROPPED_CLASSES, BATCH_RETRY_CLASSES
from .config import CFG
from .paypal_proxy import infer_proxy_country
from .phone_proxy import normalize_proxy_url, probe_proxy_with_scheme_detection, refresh_proxy_sid
from .sanitizer import sanitize_text
from .sanitizer import account_reference, mask_account
from .diagnostics import safe_print
from .proxy_health import ProxyHealthTracker
from .registration_retry_guard import RegistrationRetryGuard, mailbox_registration_status
from .registration_policy import registration_retry_decision
from .registration_result import safe_proxy_audit
from .storage import get_account_records, get_registration_checkpoints, list_account_records


class RegistrationProxyPool(list):
    """Selected registration proxies plus sanitized preflight observations."""

    def __init__(self, values=(), *, actual_countries=None):
        super().__init__(values)
        self.actual_countries = dict(actual_countries or {})


def _proxy_endpoint_group(proxy: str) -> str:
    parsed = urlsplit(str(proxy or ""))
    return f"{(parsed.hostname or '').lower()}:{int(parsed.port or 0)}"


def _interleave_proxy_endpoints(proxies) -> list[str]:
    """Keep healthy endpoints represented instead of flattening them by score."""
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
    return RegistrationProxyPool(
        selected,
        actual_countries={base: actual_countries[base] for base in selected if base in actual_countries},
    )


def select_registration_proxy_base(proxy_pool, fallback=None):
    candidates = select_registration_proxy_pool(proxy_pool, fallback)
    return candidates[0] if candidates else str(fallback or "").strip()


def _registration_proxy_metadata(
    proxy: str | None,
    *,
    pool_index: int,
    expected_country: str = "",
    actual_country: str = "",
) -> dict:
    """Return audit-safe proxy selection metadata without URL credentials."""
    parsed = urlsplit(str(proxy or ""))
    return {
        "pool_index": int(pool_index) if int(pool_index) >= 0 else -1,
        "expected_country": str(expected_country or "").strip().upper(),
        "actual_country": str(actual_country or "").strip().upper(),
        "scheme": str(parsed.scheme or "").strip().lower(),
        "rotation_generation": 0,
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


def _alias_base_email(email):
    """``local+tag@domain`` → ``local@domain``, lowercased.

    This install mints a second address from one mailbox by appending ``+oaiNN``,
    and OpenAI treats each alias as its own account: 119 bases on this install
    hold two *successfully registered* variants, so normalizing addresses
    everywhere would wrongly merge real accounts.  It is used here only as a
    **heuristic**, never as a skip reason.
    """
    local, sep, domain = str(email or "").strip().lower().partition("@")
    if not sep:
        return ""
    return f"{local.split('+', 1)[0]}@{domain}"


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
    registration_cfg = CFG.get("registration") if isinstance(CFG.get("registration"), dict) else {}
    skip_registered = registration_cfg.get("skip_registered_mailboxes", True) not in (
        False, 0, "0", "false", "False", "no",
    )
    if not skip_registered:
        return items, [], []
    deprioritize = registration_cfg.get("deprioritize_base_conflicts", True) not in (
        False, 0, "0", "false", "False", "no",
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
    from .registration_checkpoint import candidate_checkpoint_error

    checkpoint_errors = {
        email: error
        for email, checkpoint in checkpoints.items()
        if (error := candidate_checkpoint_error(checkpoint))
    }
    if retry_guard is not None:
        for checkpoint_email, checkpoint_error in checkpoint_errors.items():
            try:
                retry_guard.record(
                    checkpoint_email,
                    failure_class="auth_state",
                    error=checkpoint_error,
                )
            except Exception:
                pass
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


def _announce_skipped(skipped) -> None:
    preview = ", ".join(mask_account(email) for email in skipped[:5])
    suffix = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    safe_print(
        f"[*] Skipped {len(skipped)} mailbox(es) that already have a registered account: {preview}{suffix}"
    )
    _emit_mailboxes_skipped(skipped, reason="already_registered")


def _announce_dead_end(skipped) -> None:
    preview = ", ".join(mask_account(email) for email in skipped[:5])
    suffix = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    safe_print(
        f"[*] Skipped {len(skipped)} mailbox(es) the server already reports as registered, "
        f"are cooling down/quarantined, or have unrecoverable registration state: "
        f"{preview}{suffix}"
    )
    _emit_mailboxes_skipped(skipped, reason="dead_end_or_quarantined")


def _emit_mailboxes_skipped(skipped, *, reason: str) -> None:
    """Tell the desktop grid which rows the backend dropped before attempting.

    The console line above is invisible to the grid: an operator watching the
    pool sees a row stay in its pre-batch state and has to read the backend log
    to learn it was never tried.  Emitting one event with the masked list lets
    the WPF side mark those rows instead.  Passive and best-effort: a desktop
    host that predates this stage simply ignores the unknown event, and a CLI
    run has ``desktop_events_enabled()`` off so nothing is printed twice.
    """
    try:
        from .desktop_ipc import desktop_events_enabled, emit_event

        if not desktop_events_enabled():
            return
        preview = ", ".join(mask_account(email) for email in skipped[:3])
        suffix = f" +{len(skipped) - 3}" if len(skipped) > 3 else ""
        reason_zh = "已注册" if reason == "already_registered" else "冷却/隔离/死路"
        emit_event({
            "domain": "registration",
            "operation": "registration",
            "stage": "mailboxes_skipped",
            "status": "running",
            "detail": f"后端剔除 {len(skipped)} 个（{reason_zh}）：{preview}{suffix}",
            "reason": reason,
            "skipped_count": len(skipped),
            "skipped": [mask_account(email) for email in skipped],
        })
    except Exception:
        # Reporting skipped mailboxes must never break the batch that skipped
        # them -- the console line above already carried the fact.
        pass


def filter_registered_mailboxes(mailboxes):
    """Drop already-registered mailboxes *and report it*, for callers that size or bill a batch.

    ``run_batch_impl`` filters internally as a last line of defence, but by then
    the caller has already counted the mailboxes into its denominator -- and, on
    the ``--target-at200`` path, into ``purchased``/``spent``.  Filtering after
    that point reports skipped mailboxes as failures and can leave the
    replenishment loop spinning over a pool that yields nothing.

    Calling this first makes the second filter a no-op, so the line is printed
    exactly once.
    """
    kept, skipped, skipped_dead = _drop_already_registered(mailboxes)
    if skipped:
        _announce_skipped(skipped)
    if skipped_dead:
        _announce_dead_end(skipped_dead)
    return kept


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
    mailboxes, skipped_registered, skipped_dead = _drop_already_registered(mailboxes)
    if skipped_registered or skipped_dead:
        if skipped_registered:
            _announce_skipped(skipped_registered)
        if skipped_dead:
            _announce_dead_end(skipped_dead)
        if not mailboxes:
            # Return [] rather than let the loop run with no mailboxes: _run_one
            # resolves `mailbox = None` and hands that to the signup lane, which
            # reads as a confusing failure instead of "the pool was already used".
            safe_print(
                "[!] Every available mailbox is already registered or a known dead end; nothing to sign up. "
                "Add fresh mailboxes, or set registration.skip_registered_mailboxes=false to attempt them anyway."
            )
            return []
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
    preflight_actual_countries = dict(getattr(proxy_pool, "actual_countries", {}) or {})
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
    proxy_pool_offset = 0
    proxy_rotation_generation = 0
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
        nonlocal proxy_pool_offset, proxy_rotation_generation
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
        if guard_state.get("dead_end"):
            # Must be checked before ``deferred``: a dead end also reports
            # ``deferred`` (so cooldown-only callers still skip it), but its
            # verdict is terminal, not "come back later".
            return i, {
                "success": False,
                "email": mailbox_email,
                "error": "registration_dead_end",
                "failure_class": "account",
                "retryable": False,
                "dropped": True,
                "deferred": False,
                "registration_state": "dead_end",
                "dead_end_reason": str(guard_state.get("dead_end_reason") or ""),
                "future_batch_eligible": False,
                "retry_disposition": "dead_end",
            }
        if guard_state.get("quarantined"):
            return i, {
                "success": False,
                "email": mailbox_email,
                "error": "registration_otp_pending_quarantined",
                "failure_class": "mailbox",
                "retryable": False,
                "dropped": False,
                "deferred": True,
                "registration_state": "quarantined",
                "future_batch_eligible": False,
                "retry_disposition": "otp_pending_quarantine",
            }
        if guard_state.get("deferred"):
            return i, {
                "success": False,
                "email": mailbox_email,
                "error": "registration_retry_cooldown",
                "failure_class": "auth_state",
                "retryable": False,
                "deferred": True,
                "retry_after_seconds": int(guard_state.get("remaining_seconds") or 0),
                "registration_state": "retry_pending",
                "future_batch_eligible": True,
                "retry_disposition": "cooldown",
            }
        # Pin each account to a stable proxy egress for its entire lifetime.
        # Previously the index shifted on every retry (proxy_pool[(i+attempt-1)
        # % n]), which rotated the egress on each retry and looked like proxy
        # churn to registrars -- a ban trigger.  Retries now keep the same
        # egress and only refresh the session id (see refresh_proxy_sid below).
        account_proxy_index = (i + proxy_pool_offset) % len(proxy_pool) if proxy_pool else 0
        account_rotation_generation = proxy_rotation_generation
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
                actual_country=preflight_actual_countries.get(base_proxy, ""),
            )
            proxy_metadata["attempt"] = attempt
            proxy_metadata["rotation_generation"] = account_rotation_generation
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
                    "dropped": True if failure_class in BATCH_DROPPED_CLASSES else False if failure_class in BATCH_RETRY_CLASSES else None,
                }
            if not isinstance(result, dict):
                result = {"success": False, "error": "invalid_registration_result", "failure_class": "unknown"}
            # Only transport failures are evidence about the selected proxy.
            # Internal/configuration/account/mailbox failures must not poison
            # the shared proxy-health journal.
            failure_class = str(result.get("failure_class") or "").strip().lower()
            if not failure_class and not result.get("success"):
                failure_class = classify_error(result)
                result["failure_class"] = failure_class
            proxy_attributed = bool(result.get("proxy_attributed")) or failure_class == "network"
            if worker_proxy and (bool(result.get("success")) or proxy_attributed):
                tracker = ProxyHealthTracker(CFG)
                tracker.record(
                    worker_proxy,
                    ok=bool(result.get("success")),
                    error=str(result.get("error") or failure_class or "")[:120],
                )
            result["registration_attempts"] = attempt
            result["proxy_rotation_count"] = account_rotation_generation
            result["proxy_session_refresh_count"] = attempt
            result.setdefault("proxy_audit", safe_proxy_audit(proxy_metadata))
            result["batch_id"] = batch_id
            if result.get("success", False):
                retry_guard.record(mailbox_email, success=True)
                breaker.record_success()
                return i, result
            result.setdefault("failure_class", classify_error(result))
            decision = registration_retry_decision(result, failure_class=result["failure_class"])
            if decision.future_batch_eligible:
                result.setdefault("dropped", False)
            elif decision.dropped:
                result.setdefault("dropped", True)
            # Same-account immediate retry is separate from future-batch
            # eligibility. Network/auth-state may retry now; rate limits and
            # mailbox outcomes stop this account and let the guard decide when
            # or whether a later batch may reconsider the mailbox.
            result["future_batch_eligible"] = decision.future_batch_eligible
            result["retry_disposition"] = decision.guard_action
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

    def _rotate_proxy_pool_cursor() -> bool:
        """Move only future accounts to another configured pool slot."""
        nonlocal proxy_pool_offset, proxy_rotation_generation
        if len(proxy_pool) <= 1:
            return False
        proxy_pool_offset = (proxy_pool_offset + 1) % len(proxy_pool)
        proxy_rotation_generation += 1
        return True

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
                    on_dispatch_block=_rotate_proxy_pool_cursor,
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
