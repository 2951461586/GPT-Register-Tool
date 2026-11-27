"""Supporting types, execution loops, guard verdicts, and skip notifications for registration batches.

Extracted from :mod:`sms_tool.registration.batch_runner` so pure pool models,
dispatch loops, sentinel readiness checks, and completion event emitters live
apart from proxy preflight and top-level batch orchestration.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait as futures_wait
from typing import Any
from urllib.parse import urlsplit

from ..geo import shared_geo_resolver
from ..kernel.circuit_breaker import BatchCircuitBreaker
from ..kernel.error_classification import classify_error
from ..kernel.failure_registry import BATCH_DROPPED_CLASSES as BATCH_DROPPED_CLASSES
from ..kernel.failure_registry import BATCH_RETRY_CLASSES as BATCH_RETRY_CLASSES
from ..kernel.native_loader import load_native_module as load_native_module
from ..kernel.sanitizer import account_reference, mask_account, sanitize_text as sanitize_text
from ..observability.diagnostics import safe_print
from ..sentinel.runner import SentinelRunnerError, check_node_runner_readiness
from .policy import registration_retry_decision
from .retry_guard import RegistrationRetryGuard

_LOGGER = logging.getLogger(__name__)


class RegistrationProxyPool(list):
    """Selected registration proxies plus sanitized preflight observations."""

    def __init__(self, values=(), *, actual_countries=None):
        super().__init__(values)
        self.actual_countries = dict(actual_countries or {})
        self.dynamic_generation: dict[str, Any] = {"used": False, "count": 0, "reason": "disabled"}


def _proxy_endpoint_group(proxy: str) -> str:
    parsed = urlsplit(str(proxy or ""))
    return f"{(parsed.hostname or '').lower()}:{int(parsed.port or 0)}"


def _registration_proxy_metadata(
    proxy: str | None,
    *,
    pool_index: int,
    expected_country: str = "",
    actual_country: str = "",
    dynamic_generation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return audit-safe proxy selection metadata without URL credentials."""
    parsed = urlsplit(str(proxy or ""))
    status = dynamic_generation if isinstance(dynamic_generation, Mapping) else None
    return {
        "pool_index": int(pool_index) if int(pool_index) >= 0 else -1,
        "expected_country": str(expected_country or "").strip().upper(),
        "actual_country": str(actual_country or "").strip().upper(),
        "scheme": str(parsed.scheme or "").strip().lower(),
        "rotation_generation": 0,
        "dynamic_generation": {
            "used": bool((status or {}).get("used")),
            "count": int((status or {}).get("count") or 0),
            "reason": str((status or {}).get("reason") or "disabled")[:60],
        },
    }


def _cached_exit_ip(proxy: str | None) -> str:
    """Measured exit IP for this egress, from the shared geo cache ONLY."""
    try:
        geo = shared_geo_resolver().cached(proxy)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return ""
    return str(getattr(geo, "ip", "") or "") if geo else ""


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
    """``local+tag@domain`` → ``local@domain``, lowercased."""
    local, sep, domain = str(email or "").strip().lower().partition("@")
    if not sep:
        return ""
    return f"{local.split('+', 1)[0]}@{domain}"


def _emit_mailboxes_skipped(skipped, *, reason: str) -> None:
    """Tell the desktop grid which rows the backend dropped before attempting."""
    try:
        from ..desktop.ipc import desktop_events_enabled, emit_event

        if not desktop_events_enabled():
            return
        preview = ", ".join(mask_account(email) for email in skipped[:3])
        suffix = f" +{len(skipped) - 3}" if len(skipped) > 3 else ""
        reason_zh = "已注册" if reason == "already_registered" else "冷却/隔离/死路"
        emit_event(
            {
                "domain": "registration",
                "operation": "registration",
                "stage": "mailboxes_skipped",
                "status": "running",
                "detail": f"后端剔除 {len(skipped)} 个（{reason_zh}）：{preview}{suffix}",
                "reason": reason,
                "skipped_count": len(skipped),
                "skipped": [mask_account(email) for email in skipped],
            }
        )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        _LOGGER.debug("emit skipped mailboxes event failed: %s", exc)


def _announce_skipped(skipped) -> None:
    preview = ", ".join(mask_account(email) for email in skipped[:5])
    suffix = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    safe_print(f"[*] Skipped {len(skipped)} mailbox(es) that already have a registered account: {preview}{suffix}")
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


def ensure_batch_sentinel_readiness(config, registration_driver: str, *, readiness_fn=None) -> str:
    """Fail locally before a protocol batch makes network requests."""
    _raw_email_cfg: object = config.get("email_registration") if isinstance(config, Mapping) else {}
    email_cfg = _raw_email_cfg if isinstance(_raw_email_cfg, Mapping) else {}
    from ..sentinel import sentinel_backend

    backend = sentinel_backend({"email_registration": email_cfg})
    if registration_driver != "protocol" or backend == "legacy":
        return backend
    from ..sentinel.client import _legacy_fallback_enabled

    try:
        (readiness_fn or check_node_runner_readiness)()
    except Exception as exc:
        allowed = {
            "sentinel_runtime_unavailable",
            "sentinel_runtime_missing",
            "sentinel_runtime_hash_mismatch",
            "sentinel_runtime_invalid",
            "sentinel_runner_node_missing",
        }
        reason = (
            str(exc)
            if isinstance(exc, SentinelRunnerError) and str(exc) in allowed
            else "sentinel_runner_readiness_failed"
        )
        if not _legacy_fallback_enabled({"email_registration": email_cfg}):
            raise SentinelRunnerError(reason) from None
        safe_print(f"[!] Sentinel startup readiness failed ({reason}); configured legacy fallback remains available.")
    return backend


def _run_batch_sequential(count: int, *, run_one_fn, notify_fn, target_event: threading.Event) -> list[dict[str, Any]]:
    """Run registrations one by one, stopping early once ``target_event`` fires."""
    out: list[dict[str, Any]] = []
    for i in range(count):
        if target_event.is_set():
            break
        _, result = run_one_fn(i)
        out.append(result)
        notify_fn(i, result)
    return out


def _run_batch_threaded(
    count: int,
    *,
    workers: int,
    run_one_fn,
    notify_fn,
    target_goal: int,
    target_event: threading.Event,
    success_count_fn,
    breaker_tripped_fn,
) -> list[dict[str, Any]]:
    """Run registrations concurrently; bound in-flight slots when ``target_goal > 0``."""
    ordered: list[Any] = [None] * count
    if target_goal <= 0:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_one_fn, i) for i in range(count)]
            for future in as_completed(futures):
                i, result = future.result()
                ordered[i] = result
                notify_fn(i, result)
        return [result for result in ordered if result is not None]

    next_index = 0
    in_flight: dict[Any, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            remaining_needed = max(0, target_goal - int(success_count_fn()))
            cap = min(workers, remaining_needed) if remaining_needed > 0 else 0
            while (
                next_index < count and len(in_flight) < cap and not target_event.is_set() and not breaker_tripped_fn()
            ):
                in_flight[executor.submit(run_one_fn, next_index)] = next_index
                next_index += 1
            if not in_flight:
                break
            done, _ = futures_wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                in_flight.pop(future, None)
                i, result = future.result()
                ordered[i] = result
                notify_fn(i, result)
    return [result for result in ordered if result is not None]


def _cancelled_batch_result(attempt: int = 0) -> dict[str, Any]:
    return {
        "success": False,
        "error": "registration_cancelled",
        "failure_class": "cancelled",
        "retryable": False,
        "dropped": False,
        "registration_state": "cancelled",
        "registration_attempts": int(attempt or 0),
    }


def _resolve_batch_workers_and_breaker(
    registration_cfg: dict[str, Any],
    *,
    registration_driver: str,
    workers: int,
    count: int,
) -> tuple[int, BatchCircuitBreaker]:
    try:
        breaker_threshold = max(1, int(registration_cfg.get("batch_circuit_breaker_threshold") or 3))
    except (TypeError, ValueError):
        breaker_threshold = 3
    breaker_enabled = registration_cfg.get("batch_circuit_breaker_enabled", True) not in (
        False,
        0,
        "0",
        "false",
        "False",
        "no",
    )
    breaker = BatchCircuitBreaker(breaker_threshold, enabled=breaker_enabled)
    resolved_workers = max(1, min(int(workers or 1), 20, int(count or 1)))
    if registration_driver != "protocol":
        raw_limit = registration_cfg.get("browser_worker_limit")
        browser_limit = 0
        if raw_limit not in (None, "", 0, "0"):
            try:
                browser_limit = max(1, min(int(raw_limit), 8))
            except (TypeError, ValueError):
                browser_limit = 0
        if browser_limit and resolved_workers > browser_limit:
            safe_print(f"[*] Browser worker limit: {resolved_workers} -> {browser_limit}")
            resolved_workers = browser_limit
    return resolved_workers, breaker


def _check_batch_account_guard_state(
    retry_guard: RegistrationRetryGuard,
    mailbox_email: str,
) -> dict[str, Any] | None:
    guard_state = retry_guard.check(mailbox_email)
    if guard_state.get("dead_end"):
        return {
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
        return {
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
        return {
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
    return None


def _evaluate_batch_attempt_verdict(
    result: dict[str, Any],
    *,
    account_index: int,
    attempt: int,
    max_attempts: int,
    retry_delay_seconds: float,
    mailbox_email: str,
    retry_guard: RegistrationRetryGuard,
    breaker: BatchCircuitBreaker,
    emit_event: Any,
    batch_id: str,
    count: int,
    cancel_requested_fn: Any,
) -> tuple[bool, dict[str, Any]]:
    if result.get("success", False):
        retry_guard.record(mailbox_email, success=True)
        breaker.record_success()
        return True, result
    result.setdefault("failure_class", classify_error(result))
    decision = registration_retry_decision(result, failure_class=result["failure_class"])
    if decision.future_batch_eligible:
        result.setdefault("dropped", False)
    elif decision.dropped:
        result.setdefault("dropped", True)
    result["future_batch_eligible"] = decision.future_batch_eligible
    result["retry_disposition"] = decision.guard_action
    if not decision.retryable or attempt >= max_attempts:
        result["retryable"] = decision.retryable
        result["error_advice"] = decision.advice
        retry_guard.record(
            mailbox_email,
            failure_class=str(result.get("failure_class") or ""),
            error=str(result.get("error") or ""),
            success=False,
        )
        if breaker.record(result["failure_class"]):
            safe_print(
                f"[!] Batch paused after {breaker.consecutive} consecutive "
                f"{result['failure_class']} failures -- remaining accounts "
                f"are parked (mailboxes not consumed)."
            )
            if emit_event is not None:
                try:
                    emit_event(
                        {
                            "domain": "registration",
                            "batch_id": batch_id,
                            "operation": "registration",
                            "stage": "batch_paused",
                            "status": "paused",
                            "failure_class": str(result.get("failure_class") or ""),
                            "consecutive": int(breaker.consecutive),
                            "total": int(count or 0),
                        }
                    )
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    _LOGGER.debug("batch_paused event emit failed: %s", exc)
        return True, result
    safe_print(
        f"[!] Retryable {result['failure_class']} failure; "
        f"retrying account {account_index + 1} with a fresh proxy session "
        f"({attempt + 1}/{max_attempts})"
    )
    if retry_delay_seconds:
        from .cancel import cancellable_sleep

        if cancellable_sleep(retry_delay_seconds, requested=cancel_requested_fn):
            return True, _cancelled_batch_result(attempt)
    return False, result


def _emit_account_completion_events(
    emit_event: Any,
    *,
    batch_id: str,
    result: dict[str, Any],
    done: int,
    count: int,
    index: int,
    on_result: Any,
) -> None:
    if emit_event is not None:
        try:
            emit_event(
                {
                    "domain": "registration",
                    "batch_id": batch_id,
                    "account_ref": account_reference(result.get("email")),
                    "operation": "registration",
                    "stage": "account_completed",
                    "status": "success" if result.get("success") else "failed",
                    "attempt": int(result.get("registration_attempts") or 0),
                }
            )
            emit_event(
                {
                    "domain": "registration",
                    "batch_id": batch_id,
                    "operation": "registration",
                    "stage": "batch_progress",
                    "status": "running" if done < int(count or 0) else "completed",
                    "completed": done,
                    "total": int(count or 0),
                    "success": bool(result.get("success")),
                    "failure_class": str(result.get("failure_class") or "")[:80],
                }
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            _LOGGER.debug("account completion event emit failed: %s", exc)
    if result.get("success"):
        safe_print(f"[*] Account {index + 1}/{int(count or 0)} {mask_account(result.get('email'))}: registered")
    if on_result is None:
        return
    try:
        on_result(index, result)
    except Exception as exc:
        safe_print(f"[!] Result callback failed for account {index + 1}: {type(exc).__name__}; batch continues.")


def _close_batch_browser_pool(registration_driver: str) -> None:
    if registration_driver == "protocol":
        return
    try:
        from ..registration_drivers.browser_flow.flow_steps import close_browser_process_pool

        close_browser_process_pool()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        _LOGGER.debug("emit skipped mailboxes event failed: %s", exc)


def _dispatch_batch_execution(
    *,
    count: int,
    workers: int,
    registration_driver: str,
    run_one_fn: Any,
    notify_fn: Any,
    target_goal: int,
    target_event: threading.Event,
    cancel_event: Any,
    rotate_proxy_fn: Any,
    current_success_count_fn: Any,
    breaker: BatchCircuitBreaker,
    prewarm_executor: ThreadPoolExecutor | None,
    emit_event: Any,
    batch_id: str,
    config: Any,
) -> list[dict[str, Any]]:
    def _emit_batch_completed(total_done: int) -> None:
        if emit_event is not None:
            emit_event(
                {
                    "domain": "registration",
                    "batch_id": batch_id,
                    "operation": "registration",
                    "stage": "batch_completed",
                    "status": "completed",
                    "total": total_done,
                }
            )

    from .cancel import cancel_scope

    results: list[dict[str, Any]] = []
    with cancel_scope():
        if workers <= 1:
            try:
                results.extend(
                    _run_batch_sequential(count, run_one_fn=run_one_fn, notify_fn=notify_fn, target_event=target_event)
                )
                _emit_batch_completed(len(results))
                return results
            finally:
                if prewarm_executor is not None:
                    prewarm_executor.shutdown(wait=True)
                _close_batch_browser_pool(registration_driver)

        from .pulse import PulseConfig, run_pulse_batch

        pulse_config = PulseConfig.from_config(config)
        if pulse_config.enabled:
            try:
                pulse_results = run_pulse_batch(
                    count,
                    run_one_fn=run_one_fn,
                    on_result=notify_fn,
                    workers=workers,
                    pulse_config=pulse_config,
                    cancel_event=cancel_event,
                    stop_event=target_event if target_goal > 0 else None,
                    on_dispatch_block=rotate_proxy_fn,
                )
                _emit_batch_completed(len(pulse_results))
                return pulse_results
            finally:
                if prewarm_executor is not None:
                    prewarm_executor.shutdown(wait=True)
                _close_batch_browser_pool(registration_driver)

        try:
            results.extend(
                _run_batch_threaded(
                    count,
                    workers=workers,
                    run_one_fn=run_one_fn,
                    notify_fn=notify_fn,
                    target_goal=target_goal,
                    target_event=target_event,
                    success_count_fn=current_success_count_fn,
                    breaker_tripped_fn=lambda: breaker.tripped,
                )
            )
            _emit_batch_completed(len(results))
            return results
        finally:
            if prewarm_executor is not None:
                prewarm_executor.shutdown(wait=True)
            _close_batch_browser_pool(registration_driver)
