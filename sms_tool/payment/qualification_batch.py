"""Batch orchestration for the 支付资格 (payment qualification) check.

The probe lives in :mod:`sms_tool.payment.qualification`; this module
owns everything about running it across many saved accounts — egress rotation,
result persistence, progress events, and the whole-batch guard that stops a dead
exit pool from burning the account list.

Shape matches :mod:`sms_tool.accounts.promotion_batch` on purpose: the desktop
already knows how to render an account-result batch (progress domain, per-account
terminal events, a summary dict with ``results``/``total``), and two different
shapes would mean two renderers for the same kind of thing.

Success vocabulary
------------------
``ok`` means "the probe produced an answer", not "the account is eligible". A
conclusive ``ineligible`` result is a successful check — it is the answer the
operator asked for. ``eligible`` is reported separately, exactly like
promotion's ``trial_eligible``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation only: the import itself is function-local
    from ..batch_circuit_breaker import BatchCircuitBreaker

from .qualification import (
    check_payment_qualification,
    configured_qualification_country,
    configured_qualification_method,
)
from .qualification_states import (
    QUALIFICATION_AUTH_INVALID,
    QUALIFICATION_ELIGIBLE,
    QUALIFICATION_INELIGIBLE,
    QUALIFICATION_PROBE_FAILED,
    QUALIFICATION_UNKNOWN,
)

logger = logging.getLogger(__name__)

DOMAIN = "account_payment_qualification"

#: Preferred answer for the batch preflight: the ChatGPT edge that Checkout
#: lives behind, so "reachable" means "checkout is reachable", not just "DNS
#: resolves". Same signal ``promotion_batch`` uses.
PREFLIGHT_SAMPLE = 3
PREFLIGHT_TIMEOUT_SECONDS = 6.0

#: Probe failures per account are bounded: an account walks at most this many
#: egress candidates, so a wholly dead pool cannot multiply the batch duration.
MAX_PROXY_ATTEMPTS = 3

#: Results whose error proves *no HTTP reply* came back. Only these are evidence
#: about the environment (breaker + rotation).
_TRANSPORT_MARKERS = ("transport", "timeout", "timed out", "curl: (", "connection", "tls")

#: OpenAI's risk refusal (``400 unusual activity``). It IS worth another exit --
#: measured 2026-10-27: the same account succeeded after the egress changed, and
#: the reference flow retries it the same way. It is not a "the method cannot be
#: used" verdict, so it must not be short-circuited by the HTTP-status check.
_RISK_MARKERS = ("unusual activity", "risk_blocked")


def refresh_payment_qualification(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 45,
    proxy_pool: str | list[str] | None = None,
    method: str = "",
    country: str = "",
    probe_all_methods: bool | None = None,
) -> dict[str, Any]:
    """Probe 支付资格 for saved accounts and persist the badge + state."""
    from ..storage import get_account_record, list_paypal_accounts

    accounts = _load_accounts(emails, get_account_record=get_account_record, list_accounts=list_paypal_accounts)
    selected_method = str(method or "").strip().lower().replace("-", "_") or configured_qualification_method()
    selected_country = str(country or configured_qualification_country()).strip().upper()
    run_id = uuid.uuid4().hex
    _emit(run_id, "batch_started", "running", total=len(accounts), detail=f"支付资格检测开始（{selected_method}）")
    if not accounts:
        _emit(run_id, "batch_completed", "completed", total=0, detail="没有可检测的账号")
        return _summary([], method=selected_method)

    breaker = _breaker()
    preflight = _preflight(_egress_candidates(proxy, proxy_pool))
    if not preflight["ok"]:
        _emit(
            run_id,
            "batch_aborted",
            "failed",
            total=len(accounts),
            detail=f"出口池预检失败：{preflight['checked']} 个候选均无响应，已中止以免整批空转",
        )
        return _summary([], method=selected_method, aborted=True)

    max_workers = max(1, min(_as_int(workers, 4), 16, len(accounts)))

    def run(account: dict[str, Any]) -> dict[str, Any]:
        email = str(account.get("email") or "").strip().lower()
        if breaker.tripped:
            _emit(
                run_id,
                "account_completed",
                "skipped",
                account_ref=email,
                total=len(accounts),
                detail="批次已熔断，跳过",
            )
            return _row(email, {"ok": False, "payment_qualification_state": QUALIFICATION_PROBE_FAILED}, skipped=True)
        probe = _probe_with_rotation(
            run_id,
            account,
            proxy,
            proxy_pool,
            timeout,
            selected_method,
            selected_country,
            len(accounts),
            probe_all_methods,
        )
        persisted = _persist(email, probe)
        row = _row(email, probe, persisted=persisted)
        if row["ok"] and persisted:
            breaker.record_success()
        else:
            breaker.record(_env_failure_class(probe))
        _emit(
            run_id,
            "account_completed",
            "completed" if row["ok"] and persisted else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(row.get("payment_qualification") or "检测完成"),
        )
        return row

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run, account) for account in accounts]
        for future in as_completed(futures):
            results.append(future.result())

    summary = _summary(results, method=selected_method, breaker=breaker)
    _emit(
        run_id,
        "batch_completed",
        "completed" if summary["ok"] else "failed",
        total=summary["total"],
        detail=(
            f"完成 {summary['total']} 个账号，已判定 {summary['success']}，"
            f"可用 {summary['eligible']}，无可用 {summary['ineligible']}，"
            f"AT失效 {summary['auth_invalid']}，检测失败 {summary['probe_failed']}，"
            f"保存失败 {summary['persist_failed']}"
        ),
    )
    return summary


# ── account/probe plumbing ───────────────────────────────────────────────────
def _load_accounts(emails, *, get_account_record, list_accounts) -> list[dict[str, Any]]:
    requested = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not requested:
        requested = [str(row.get("email") or "").strip().lower() for row in list_accounts()]
    requested = list(dict.fromkeys(email for email in requested if email))
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
            # The device id is also a column; ``raw_json`` normally carries it,
            # but an imported record may only have the column. The Checkout
            # Sentinel token is device-bound, so a missing id would force a fresh
            # one and mismatch the account's session.
            if not str(data.get("device_id") or "").strip():
                data["device_id"] = record.get("device_id") or ""
        accounts.append(data)
    return accounts


def _egress_candidates(proxy: str | None, proxy_pool: str | list[str] | None) -> list[str]:
    from ..proxy.routing import operation_proxy_candidates, parse_lane_proxy_pool

    candidates = operation_proxy_candidates(
        {"email": "batch-preflight"},
        operation="promotion",
        explicit=proxy,
        pool=proxy_pool if parse_lane_proxy_pool(proxy_pool) else None,
        config=_runtime_config(),
    )
    return [item.proxy for item in candidates if item.proxy]


def _probe_with_rotation(
    run_id: str,
    account: dict[str, Any],
    proxy: str | None,
    proxy_pool: str | list[str] | None,
    timeout: int,
    method: str,
    country: str,
    total: int,
    probe_all_methods: bool | None = None,
) -> dict[str, Any]:
    """Probe one account, rotating to a fresh exit only on a transport failure."""
    from ..proxy.routing import operation_proxy_candidates, parse_lane_proxy_pool

    candidates = operation_proxy_candidates(
        account,
        operation="promotion",
        explicit=proxy,
        pool=proxy_pool if parse_lane_proxy_pool(proxy_pool) else None,
        config=_runtime_config(),
    )
    ordered = [item.proxy for item in candidates] or [None]
    email = str(account.get("email") or "").strip().lower()
    probe: dict[str, Any] = {}
    for index, candidate in enumerate(ordered[:MAX_PROXY_ATTEMPTS]):
        _emit(
            run_id,
            "proxy_attempt",
            "running",
            account_ref=email,
            total=total,
            detail=f"代理 {index + 1}/{min(len(ordered), MAX_PROXY_ATTEMPTS)}",
        )
        probe = check_payment_qualification(
            account,
            proxy=candidate,
            timeout=timeout,
            method=method,
            country=country,
            probe_all_methods=probe_all_methods,
        )
        if isinstance(probe, dict):
            probe["proxy_index"] = index
            probe["proxy_attempts"] = index + 1
            probe.update(_proxy_fingerprint(candidate))
        if not _retryable_transport(probe):
            break
    return probe or {"ok": False, "payment_qualification_state": QUALIFICATION_PROBE_FAILED, "error": "no_proxy"}


def _proxy_fingerprint(proxy: str | None) -> dict[str, str]:
    """Credential-free identity for failure attribution (host + session hash)."""
    value = str(proxy or "").strip()
    if not value:
        return {"proxy_host": "", "proxy_session": "", "proxy_fingerprint": ""}
    try:
        from urllib.parse import unquote, urlsplit

        parsed = urlsplit(value if "://" in value else "http://" + value)
        host = str(parsed.hostname or "")
        port = _as_int(parsed.port, 0)
        host_label = f"{host}:{port}" if port else host
        username = unquote(parsed.username or "")
        if not username:
            return {"proxy_host": host_label, "proxy_session": "", "proxy_fingerprint": host_label}
        session = hashlib.sha256(username.encode("utf-8")).hexdigest()[:10]
        return {
            "proxy_host": host_label,
            "proxy_session": session,
            "proxy_fingerprint": f"{host_label}#sid-{session}",
        }
    except Exception:
        return {"proxy_host": "", "proxy_session": "", "proxy_fingerprint": ""}


def _retryable_transport(probe: dict[str, Any]) -> bool:
    """True when another egress is worth trying for this method result.

    Two classes qualify: a transport failure (no HTTP reply at all) and a **risk
    block** (``400 unusual activity``) — the latter is exit/device dependent, so
    it is checked *before* the HTTP-status short-circuit. Any other HTTP status
    proves the exit worked, so it must not rotate.
    """
    if not isinstance(probe, dict) or probe.get("ok"):
        return False
    error = str(probe.get("error") or "").lower()
    if any(marker in error for marker in _RISK_MARKERS):
        return True
    if _as_int(probe.get("status_code"), 0):
        return False
    return any(marker in error for marker in _TRANSPORT_MARKERS)


def _persist(email: str, probe: dict[str, Any]) -> bool:
    if not email:
        return False
    # Imported from its real home rather than through the ``storage`` star
    # re-export: a wildcard facade makes the symbol invisible to static
    # import checkers that do not follow ``__all__`` through two hops.
    from ..store.markers import mark_payment_qualification_status

    label = str(probe.get("payment_qualification") or "")
    state = str(probe.get("payment_qualification_state") or "")
    return bool(mark_payment_qualification_status(email, label, state=state, result=probe))


def _row(email: str, probe: dict[str, Any], *, persisted: bool = False, skipped: bool = False) -> dict[str, Any]:
    state = str(probe.get("payment_qualification_state") or QUALIFICATION_UNKNOWN)
    conclusive = state in {QUALIFICATION_ELIGIBLE, QUALIFICATION_INELIGIBLE}
    return {
        "email": email,
        # "ok" means "we got an answer", not "eligible" -- see the module docstring.
        "ok": conclusive and not skipped,
        "eligible": state == QUALIFICATION_ELIGIBLE,
        "payment_method": str(probe.get("payment_method") or ""),
        "payment_methods": list(probe.get("payment_methods") or []),
        "eligible_methods": list(probe.get("eligible_methods") or []),
        "country": str(probe.get("country") or ""),
        "country_source": str(probe.get("country_source") or ""),
        "payment_qualification": str(probe.get("payment_qualification") or ""),
        # The desktop result dialog reads this name (same field the account
        # payload exposes), so a batch row renders its own badge instead of
        # falling back to the liveness labeler ("测活失败 · HTTP 400").
        "payment_qualification_display": str(probe.get("payment_qualification") or ""),
        "payment_qualification_state": state,
        "proxy_fingerprint": str(probe.get("proxy_fingerprint") or ""),
        "proxy_host": str(probe.get("proxy_host") or ""),
        "proxy_attempts": _as_int(probe.get("proxy_attempts"), 0),
        "persisted": bool(persisted),
        "skipped": bool(skipped),
        "probe": probe,
    }


# ── batch guards + summary ───────────────────────────────────────────────────
def _runtime_config() -> Any:
    """Resolve the process config at call time.

    Imported here rather than at module scope: ``scripts/import_layer_ratchet.py``
    freezes ``payment -> sms_tool`` at its current count, and this module is only
    loaded by the CLI command (which has already initialised the config).
    """
    from ..config import CFG

    return CFG


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _breaker() -> "BatchCircuitBreaker":
    """Whole-batch breaker sharing the registration batch-policy keys."""
    from ..batch_circuit_breaker import BatchCircuitBreaker

    registration_cfg = _runtime_config().get("registration")
    if not isinstance(registration_cfg, dict):
        registration_cfg = {}
    enabled = registration_cfg.get("batch_circuit_breaker_enabled", True) not in (
        False,
        0,
        "0",
        "false",
        "False",
        "no",
    )
    return BatchCircuitBreaker(
        max(1, _as_int(registration_cfg.get("batch_circuit_breaker_threshold"), 3)), enabled=enabled
    )


def _preflight(candidates: list[str]) -> dict[str, Any]:
    """One-shot egress preflight so a dead pool fails in seconds, not minutes."""
    from ..proxy.edge_probe import DEAD, probe_openai_edge

    sample = candidates[:PREFLIGHT_SAMPLE]
    if not sample:
        # No configured pool is not a preflight failure: the account's own
        # affinity or a direct connection is still a legitimate path.
        return {"ok": True, "checked": 0}
    for candidate in sample:
        verdict = probe_openai_edge(candidate, timeout=PREFLIGHT_TIMEOUT_SECONDS)
        if verdict.status != DEAD:
            return {"ok": True, "checked": len(sample)}
    return {"ok": False, "checked": len(sample)}


def _env_failure_class(probe: dict[str, Any]) -> str:
    """Map one account outcome onto the **shared** breaker vocabulary.

    ``BatchCircuitBreaker`` only trips on ``ENVIRONMENT_FAILURE_CLASSES``
    (``network`` / ``auth_state``). The old values (``transport`` /
    ``unauthorized`` / ``probe``) matched none of them, so this batch's breaker
    could never open. A transport failure or a risk block is an exit-level
    problem worth another egress; an invalid AT is per-account and must not park
    the rest of the batch.
    """
    state = str(probe.get("payment_qualification_state") or "")
    error = str(probe.get("error") or "").lower()
    if state == QUALIFICATION_PROBE_FAILED:
        if any(marker in error for marker in _RISK_MARKERS) or any(marker in error for marker in _TRANSPORT_MARKERS):
            return "network"
        return "probe"
    if state == QUALIFICATION_AUTH_INVALID:
        return "account"
    return "probe"


def _summary(
    results: list[dict[str, Any]],
    *,
    method: str,
    breaker: "BatchCircuitBreaker | None" = None,
    aborted: bool = False,
) -> dict[str, Any]:
    states = [str(item.get("payment_qualification_state") or "") for item in results]
    total = len(results)
    success = sum(1 for item in results if item.get("ok"))
    persist_failed = sum(1 for item in results if not item.get("persisted") and not item.get("skipped"))
    tripped = bool(breaker.tripped) if breaker is not None else False
    return {
        "ok": bool(results) and success == total and not persist_failed and not tripped and not aborted,
        "aborted": aborted,
        "circuit_breaker_tripped": tripped,
        "payment_method": method,
        "total": total,
        "success": success,
        "failed": total - success,
        "eligible": states.count(QUALIFICATION_ELIGIBLE),
        "ineligible": states.count(QUALIFICATION_INELIGIBLE),
        "auth_invalid": states.count(QUALIFICATION_AUTH_INVALID),
        "probe_failed": states.count(QUALIFICATION_PROBE_FAILED),
        "unknown": states.count(QUALIFICATION_UNKNOWN),
        "persist_failed": persist_failed,
        "results": results,
    }


def _emit(run_id: str, stage: str, status: str, *, account_ref: str = "", total: int = 0, detail: str = "") -> None:
    try:
        from ..desktop_ipc import emit_event

        emit_event(
            {
                "domain": DOMAIN,
                "run_id": run_id,
                "account_ref": account_ref,
                "stage": stage,
                "status": status,
                "total": int(total or 0),
                "detail": detail,
            }
        )
    except Exception:
        return


__all__ = ["DOMAIN", "refresh_payment_qualification"]
