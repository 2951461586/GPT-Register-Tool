"""Batch orchestration for the 优惠 (promotion) check.

Split out of :mod:`sms_tool.accounts.account_promotion` so that module owns only
the probe: ``check_account_promotion`` and its parse/label/code helpers. This
module owns everything about running that probe across many saved accounts --
proxy rotation, 429 throttling, the optional payment-eligibility pass, result
persistence and progress events.

The split is what keeps the probe module off the desktop read path's import
cost: ``desktop_read`` reads ``promotion_states`` and ``account_promotion``;
only the batch entry points (CLI / account-health queue) import this module, so
``account_payment_eligibility`` and the payment catalog it pulls in stay out of
a read-only desktop start.

Callers import :func:`refresh_promotion_statuses` from here.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .account_identity import account_identity
from .account_liveness import browser_fetch_for_account
from .account_payment_eligibility import (
    payment_eligibility_diagnostics,
    probe_account_payment_eligibility,
)
from .account_promotion import check_account_promotion, promotion_status_code
from ..config import CFG
from ..promotion_states import (
    PROMOTION_STATE_AUTH_INVALID,
    PROMOTION_STATE_PROBE_FAILED,
    payment_eligibility_label,
    payment_method_tokens,
    promotion_status_with_eligibility,
)
from ..proxy_routing import operation_proxy_candidates, parse_lane_proxy_pool

logger = logging.getLogger(__name__)

# Rate-limit response: worth one delayed retry. Bounds exist so a hostile or
# fat-fingered ``Retry-After`` cannot park a batch run.
PROMOTION_THROTTLE_STATUS = 429
PROMOTION_THROTTLE_DEFAULT_BACKOFF = 1.5
PROMOTION_THROTTLE_MAX_BACKOFF = 5.0


def refresh_promotion_statuses(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 20,
    proxy_pool: str | list[str] | None = None,
    payment_eligibility: bool = False,
) -> dict[str, Any]:
    """Probe plan/promotion for saved accounts and persist ``promotion_status``.

    When ``payment_eligibility`` is explicitly set, each account whose plan
    probe succeeded also gets one disposable Checkout + Stripe init
    probe that enumerates the payment methods Stripe offers it; the result is
    persisted as ``raw_json.payment_capability`` and rendered next to the
    promotion badge in the desktop 优惠状态 column.  See
    :mod:`sms_tool.accounts.account_payment_eligibility` for why it is one probe
    rather than one per method.
    """
    from ..storage import get_account_record, list_paypal_accounts, mark_promotion_status

    requested = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not requested:
        requested = [str(row.get("email") or "").strip().lower() for row in list_paypal_accounts()]
    requested = list(dict.fromkeys(e for e in requested if e))
    accounts: list[dict[str, Any]] = []
    for email in requested:
        record = get_account_record(email)
        data: dict[str, Any] = {"email": email}
        if record:
            try:
                data.update(json.loads(record.get("raw_json") or "{}"))
            except Exception:
                pass
            data.setdefault("access_token", record.get("access_token") or "")
        accounts.append(data)

    max_workers = max(1, min(int(workers or 1), 16, len(accounts) or 1))
    results: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex
    _emit_account_batch_event(run_id, "batch_started", "running", total=len(accounts), detail="账号优惠检测开始")

    def run(account: dict[str, Any]) -> dict[str, Any]:
        email = str(account.get("email") or "").strip().lower()
        used_proxy = proxy
        try:
            with browser_fetch_for_account(account, proxy=proxy, timeout=timeout) as browser_fetch:
                browser_identity = account_identity(account).get("browser_identity") or {}
                if browser_identity and browser_fetch is None:
                    probe = {
                        "ok": False,
                        "promotion_status": "检测失败",
                        "error": "browser_context_unavailable",
                    }
                else:
                    # Stateless/imported accounts have no persisted identity
                    # affinity. Rotate through the supplied health pool when a
                    # proxy-only timeout occurs so one dead exit does not make
                    # the same account fail on every run.
                    candidates = _promotion_proxy_candidates(account, proxy, proxy_pool)
                    probe = None
                    for index, candidate in enumerate(candidates):
                        probe = check_account_promotion(
                            account,
                            proxy=candidate,
                            timeout=timeout,
                            browser_fetch=browser_fetch,
                        )
                        used_proxy = candidate
                        if probe.get("ok"):
                            break
                        # A 429 is per-exit *and* short-lived: sleep first, then
                        # rotate to a fresh IP when the pool has one left, and
                        # fall back to one delayed retry on the same exit once
                        # the pool is exhausted. Total attempts are therefore
                        # bounded at len(candidates) + 1, so a sustained 429
                        # cannot multiply the batch duration.
                        backoff = _promotion_throttle_backoff(probe)
                        if backoff is not None:
                            time.sleep(backoff)
                            if index >= len(candidates) - 1:
                                probe = check_account_promotion(
                                    account,
                                    proxy=candidate,
                                    timeout=timeout,
                                    browser_fetch=browser_fetch,
                                )
                                break
                            continue
                        if not _retryable_promotion_transport(probe) or index >= len(candidates) - 1:
                            break
                    probe = probe or {
                        "ok": False,
                        "promotion_status": "检测失败",
                        "error": "no_promotion_proxy_available",
                    }
            label = str(probe.get("promotion_status") or "")
            if not str(probe.get("promotion_state") or "").strip():
                probe["promotion_state"] = promotion_status_code(probe)
            # A failed plan probe does not establish a trustworthy context for
            # Checkout. In particular, a dead AT or failed exit must not incur
            # an extra request or produce a payment-method verdict.
            eligibility: dict[str, Any] = {}
            if payment_eligibility and probe.get("ok"):
                eligibility = _probe_payment_eligibility(account, proxy=used_proxy, timeout=timeout)
            persisted = (
                mark_promotion_status(
                    email,
                    label,
                    promotion_result=probe,
                    payment_capability={} if _promotion_probe_is_unauthorized(probe) else (eligibility or None),
                )
                if email
                else False
            )
            result = {
                "email": email,
                "ok": bool(probe.get("ok")),
                "promotion_status": label,
                "promotion_state": str(probe.get("promotion_state") or ""),
                "persisted": bool(persisted),
                "probe": probe,
            }
            eligibility_label = ""
            if eligibility:
                result["payment_capability"] = eligibility
                eligibility_label = payment_eligibility_label(eligibility)
                if eligibility_label:
                    result["payment_eligibility"] = eligibility_label
            # Pre-composed so every consumer (desktop grid, detail panel, task
            # result list) renders the same string instead of each re-deriving
            # the separator rule.
            result["promotion_display"] = promotion_status_with_eligibility(label, eligibility_label)
        except Exception as exc:
            result = {"email": email, "ok": False, "promotion_status": "检测失败", "promotion_state": PROMOTION_STATE_PROBE_FAILED, "persisted": False, "probe": {"ok": False, "error": str(exc)[:200]}}
        _emit_account_batch_event(
            run_id,
            "account_completed",
            "completed" if result.get("ok") and result.get("persisted") else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(result.get("promotion_status") or "检测完成"),
        )
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run, account) for account in accounts]
        for future in as_completed(futures):
            results.append(future.result())

    success = sum(1 for item in results if item.get("ok"))
    unauthorized = sum(1 for item in results if _promotion_status_code(item) == 401)
    transport_failed = sum(1 for item in results if _promotion_failure_class(item) == "transport")
    persist_failed = sum(1 for item in results if not item.get("persisted"))
    # Owned here, not recounted by each caller. commands/registration.py used to
    # derive this itself, which left the CLI promotion path with no such key at
    # all -- two entry points, two different report shapes.
    trial_eligible = sum(
        1
        for item in results
        if isinstance(item.get("probe"), dict)
        and bool(item["probe"].get("plus_trial_eligible"))
    )
    eligibility_results = [
        item["payment_capability"]
        for item in results
        if isinstance(item.get("payment_capability"), dict)
    ]
    eligibility_ok = sum(1 for item in eligibility_results if item.get("ok"))
    # Distinct method tokens across the batch: the batch-level answer to "which
    # payment rails do these accounts actually have".
    methods_seen = sorted({token for item in eligibility_results for token in payment_method_tokens(item)})
    diagnostics = payment_eligibility_diagnostics(eligibility_results)
    _emit_account_batch_event(
        run_id,
        "batch_completed",
        "completed" if success == len(results) and not persist_failed else "failed",
        total=len(results),
        detail=(
            f"完成 {len(results)} 个账号，成功 {success}，401 {unauthorized}，"
            f"传输失败 {transport_failed}，保存失败 {persist_failed}，"
            f"支付资格 {eligibility_ok}/{len(eligibility_results)}"
        ),
    )
    return {
        "ok": bool(results) and success == len(results) and not persist_failed,
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "unauthorized": unauthorized,
        "transport_failed": transport_failed,
        "persist_failed": persist_failed,
        "trial_eligible": trial_eligible,
        "payment_eligibility_ok": eligibility_ok,
        "payment_eligibility_failed": len(eligibility_results) - eligibility_ok,
        "payment_eligibility_diagnostics": diagnostics,
        "payment_methods_seen": methods_seen,
        "results": results,
    }


def _promotion_proxy_candidates(account: dict[str, Any], proxy: str | None, proxy_pool: str | list[str] | None) -> list[str | None]:
    """Return candidates from the canonical operation-proxy decision point."""
    candidates = operation_proxy_candidates(
        account,
        operation="promotion",
        explicit=proxy,
        pool=proxy_pool if parse_lane_proxy_pool(proxy_pool) else None,
        config=CFG,
    )
    return [item.proxy for item in candidates] or [None]


def _retryable_promotion_transport(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict) or probe.get("ok"):
        return False
    if probe.get("status_code"):
        return False
    error = str(probe.get("error") or "").lower()
    return any(marker in error for marker in ("curl: (5)", "curl: (7)", "curl: (28)", "timed out", "timeout"))


def _promotion_throttle_backoff(probe: dict[str, Any] | None) -> float | None:
    """Seconds to wait before retrying a throttled probe, else ``None``.

    Only HTTP 429 is throttled. **401 is deliberately not retryable** -- the
    access token is dead, so a second attempt just burns a proxy slot and adds
    latency (``test_promotion_401_stays_in_promotion_namespace`` locks that in).
    When the endpoint sends ``Retry-After`` we honour it, clamped so a hostile
    or misconfigured value cannot stall a batch run.
    """
    if not isinstance(probe, dict) or probe.get("ok"):
        return None
    try:
        code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        return None
    if code != PROMOTION_THROTTLE_STATUS:
        return None
    try:
        seconds = float(str(probe.get("retry_after") or "").strip())
    except (TypeError, ValueError):
        return PROMOTION_THROTTLE_DEFAULT_BACKOFF
    if seconds < 0:
        return PROMOTION_THROTTLE_DEFAULT_BACKOFF
    return min(seconds, PROMOTION_THROTTLE_MAX_BACKOFF)


def _promotion_status_code(item: dict[str, Any]) -> int:
    try:
        return int((item.get("probe") or {}).get("status_code") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _promotion_probe_is_unauthorized(probe: Any) -> bool:
    """True when the promotion probe proved the access token is dead."""
    if not isinstance(probe, dict):
        return False
    if int(probe.get("status_code") or 0) == 401:
        return True
    return str(probe.get("promotion_state") or "").strip() == PROMOTION_STATE_AUTH_INVALID


def _probe_payment_eligibility(
    account: dict[str, Any],
    *,
    proxy: str | None,
    timeout: int,
) -> dict[str, Any]:
    """Enumerate the account's payment methods, never raising into the batch."""
    try:
        return probe_account_payment_eligibility(
            account,
            proxy=proxy,
            timeout=max(5, int(timeout or 45)),
        )
    except Exception:  # noqa: BLE001 - eligibility is best-effort
        logger.debug("payment eligibility probe failed")
        return {
            "ok": False,
            "error": "eligibility_probe_exception",
            "error_code": "eligibility_probe_exception",
            "error_stage": "payment_eligibility",
            "retryable": True,
        }


def _promotion_failure_class(item: dict[str, Any]) -> str:
    if item.get("ok"):
        return "ok"
    code = _promotion_status_code(item)
    if code == 401:
        return "unauthorized"
    probe = item.get("probe") if isinstance(item.get("probe"), dict) else {}
    if not code and _retryable_promotion_transport(probe):
        return "transport"
    return "probe"


def _emit_account_batch_event(
    run_id: str,
    stage: str,
    status: str,
    *,
    account_ref: str = "",
    total: int = 0,
    detail: str = "",
) -> None:
    try:
        from ..desktop_ipc import emit_event

        emit_event({
            "domain": "account_promotion",
            "run_id": run_id,
            "account_ref": account_ref,
            "stage": stage,
            "status": status,
            "total": int(total or 0),
            "detail": detail,
        })
    except Exception:
        pass


__all__ = ["refresh_promotion_statuses"]
