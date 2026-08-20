"""First-class batch executor for protocol payment extraction."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .account_seed import load_account_seed
from .config import CFG
from .cross_process_gate import CrossProcessSemaphore, GateTimeoutError
from .sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text
from .paths import runtime_file
from .payment_auth import ensure_payment_access_token, public_payment_auth_result
from .payment_link_manager import (
    coerce_approve_country,
    generate_payment_link,
    normalize_payment_method,
    parse_proxy_pool,
    probe_payment_method,
)
from .payment_routing import PaymentRoutePlanner
from .payment_contracts import PaymentResult, payment_history_metadata, payment_retry_allowed
from .payment_catalog import PAYMENT_METHODS

# Minimum spacing between "running" checkpoint rewrites of the batch report
# (terminal states always persist immediately).
_CHECKPOINT_MIN_INTERVAL_SECONDS = 2.0


def load_payment_matrix(value: Any = None) -> list[dict[str, Any]]:
    """Load matrix cells from a JSON string/path or protocol_payments config."""
    raw = value
    if raw in (None, "", False):
        protocol = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), dict) else {}
        raw = protocol.get("matrix") or []
    if isinstance(raw, (str, Path)):
        explicit_path = isinstance(raw, Path)
        text = str(raw).strip()
        if not text:
            return []
        source = text
        if explicit_path or text[:1] not in {"[", "{"}:
            path = Path(text)
            try:
                is_file = path.is_file()
            except OSError as exc:
                raise ValueError(f"payment matrix path is invalid: {path}") from exc
            if not is_file:
                raise ValueError(f"payment matrix file does not exist: {path}")
            try:
                source = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise ValueError(f"payment matrix file could not be read: {path}") from exc
        try:
            raw = json.loads(source)
        except (ValueError, TypeError) as exc:
            location = f"file {path}" if explicit_path or text[:1] not in {"[", "{"} else "inline JSON"
            raise ValueError(f"invalid payment matrix JSON in {location}") from exc
    if isinstance(raw, dict):
        raw = raw.get("cells") if isinstance(raw.get("cells"), list) else [raw]
    cells = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        cell = dict(item)
        cell["name"] = str(cell.get("name") or f"cell_{index + 1}").strip()
        cell["sample_size"] = max(1, int(cell.get("sample_size") or 1))
        cells.append(cell)
    return cells


def run_payment_batch(
    emails: list[str],
    *,
    payment_method: str,
    workers: int = 1,
    batch_id: str = "",
    proxy: Any = None,
    payment_kwargs: dict[str, Any] | None = None,
    jit_refresh: bool = True,
    probe_only: bool = False,
    matrix: Any = None,
    canary: int = 0,
    retries: int = 3,
    timeout: int = 30,
    progress: Callable[[dict[str, Any]], None] | None = None,
    access_tokens: dict[str, str] | None = None,
    resume_checkpoint: bool | None = None,
) -> dict[str, Any]:
    method = normalize_payment_method(payment_method)
    # The desktop and CLI pass an explicit boolean.  Keep direct Python callers
    # that predate the flag source-compatible by treating an omitted value with
    # a caller-supplied batch ID as an explicit resume request.
    if resume_checkpoint is None:
        resume_checkpoint = bool(str(batch_id or "").strip())
    if not method:
        raise ValueError(f"unsupported payment method: {payment_method}")
    definition = PAYMENT_METHODS[method]
    if not probe_only and not definition.batch_enabled:
        raise ValueError(f"payment method is not enabled for batch execution: {method}")
    if definition.release_tier == "canary" and not probe_only and not canary:
        raise ValueError(f"payment method requires an explicit canary batch: {method}")
    selected = _unique_emails(emails)
    if canary:
        selected = selected[: max(1, int(canary))]
    started = time.time()
    batch_id = _safe_batch_id(batch_id) or f"{method}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    cells = load_payment_matrix(matrix)
    protocol_cfg = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), dict) else {}
    batch_cfg = protocol_cfg.get("batch") if isinstance(protocol_cfg.get("batch"), dict) else {}
    if not canary and not probe_only:
        paused = _active_canary_pause(batch_cfg, method)
        if paused:
            raise RuntimeError(f"payment_batch_paused_by_canary:{paused.get('reason') or 'protocol_profile_failed'}")
    method_caps = batch_cfg.get("method_workers") if isinstance(batch_cfg.get("method_workers"), dict) else {}
    default_cap = 2 if method in {"momo", "kakao"} else 4
    cap = max(1, int(method_caps.get(method) or default_cap))
    max_workers = max(1, min(int(workers or 1), cap, len(selected) or 1))
    retry_count = max(0, min(int(retries or 0), 5))
    base_kwargs = dict(payment_kwargs or {})
    method_configs = protocol_cfg.get("methods") if isinstance(protocol_cfg.get("methods"), Mapping) else {}
    canonical_method_cfg = method_configs.get(method) if isinstance(method_configs.get(method), Mapping) else {}
    legacy_method_cfg = CFG.get(method) if isinstance(CFG.get(method), Mapping) else {}
    for stage in ("checkout", "approve"):
        pool_key = f"{stage}_proxy_pool"
        # Custom transports (used by local adapters/tests) provide their own
        # routing contract; do not silently replace their explicit stage proxy
        # with the process-wide configured pool.
        if base_kwargs.get("transport") is not None:
            continue
        if parse_proxy_pool(base_kwargs.get(pool_key)) or str(base_kwargs.get(f"{stage}_proxy") or "").strip():
            # An explicit pool is authoritative.  Legacy single-proxy values
            # remain supported, but the configured two-pool contract is used
            # for ordinary batch runs when no explicit pool was supplied.
            if parse_proxy_pool(base_kwargs.get(pool_key)):
                continue
        configured_pool = (
            canonical_method_cfg.get(pool_key)
            or legacy_method_cfg.get(pool_key)
        )
        if not proxy and parse_proxy_pool(configured_pool):
            base_kwargs[pool_key] = configured_pool
            base_kwargs.pop(f"{stage}_proxy", None)
    run_signature = _batch_run_signature(
        method=method,
        probe_only=probe_only,
        jit_refresh=jit_refresh,
        matrix=cells,
        payment_kwargs=base_kwargs,
        proxy=proxy,
        retries=retry_count,
    )
    report_path = _report_path(batch_id, probe_only=probe_only)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    event_path = _event_path(batch_id, probe_only=probe_only)
    checkpoint_lock = threading.Lock()
    event_lock = threading.Lock()
    process_gate = CrossProcessSemaphore(
        f"payment-batch-{batch_id}",
        1,
        base_dir=runtime_file(CFG, "payment_batch_locks"),
    )
    try:
        process_gate.acquire(timeout=30)
    except GateTimeoutError as exc:
        raise RuntimeError("payment batch is already running") from exc
    try:
        existing = _load_checkpoint(report_path, method, run_signature) if resume_checkpoint else {}
    except Exception:
        process_gate.release()
        raise
    try:
        existing_by_ref = {
            str(row.get("account_ref") or ""): row
            for row in (existing.get("results") or [])
            if (
                isinstance(row, dict)
                and row.get("account_ref")
                and _checkpoint_row_resumable(row)
            )
        }
        ordered: list[dict[str, Any] | None] = [
            existing_by_ref.get(_account_ref(email)) for email in selected
        ]
        pending = [(index, email) for index, email in enumerate(selected) if ordered[index] is None]
        if existing and resume_checkpoint:
            _replay_events(event_path, run_signature, progress)
        else:
            _reset_event_log(event_path)
    except Exception:
        process_gate.release()
        raise
    stage_state: dict[str, dict[str, Any]] = {}

    def emit(account_ref: str, stage: str, status: str = "running", **extra: Any) -> None:
        if progress is None:
            event = None
        now_mono = time.monotonic()
        state = stage_state.setdefault(account_ref, {"started": now_mono, "stage": "", "stage_started": now_mono, "timings": {}, "last_failed_stage": ""})
        if state["stage"] and state["stage"] != stage:
            state["timings"][state["stage"]] = state["timings"].get(state["stage"], 0) + int((now_mono - state["stage_started"]) * 1000)
        if state["stage"] != stage:
            state["stage"] = stage
            state["stage_started"] = now_mono
        if status in {"failed", "error"}:
            state["last_failed_stage"] = stage
        if extra.get("account_terminal"):
            state["timings"][stage] = state["timings"].get(stage, 0) + int((now_mono - state["stage_started"]) * 1000)
            extra.setdefault("duration_ms", int((now_mono - state["started"]) * 1000))
            extra.setdefault("last_failed_stage", state["last_failed_stage"])
            extra.setdefault("stage_timings_ms", dict(state["timings"]))
        event = {
            "domain": "payment",
            "run_id": f"{batch_id}:{account_ref}",
            "operation": "probe" if probe_only else "extract",
            "batch_id": batch_id,
            "method": method,
            "account_ref": account_ref,
            "stage": stage,
            "status": status,
            **extra,
        }
        _append_event(event_path, event, run_signature, event_lock)
        if progress is not None:
            progress(event)

    def run_one(index: int, email: str) -> tuple[int, dict[str, Any]]:
        account_ref = _account_ref(email)
        emit(account_ref, "routing")
        seed_context, _ = load_account_seed(email=email)
        seed_country = _registration_country(seed_context)
        cell = _matrix_cell_for(index, cells, method, seed_country)
        kwargs = _cell_payment_kwargs(base_kwargs, cell, proxy, payment_method=method, pool_index=index)
        kwargs.pop("proxy", None)
        plan = kwargs.get("payment_route_plan")
        if plan is None:
            plan = PaymentRoutePlanner(CFG).plan(
                method,
                options=kwargs,
                default_proxy=proxy,
                pool_offset=index,
            )
            kwargs = {**kwargs, **plan.to_adapter_options(), "payment_route_plan": plan}
        checkout_route = plan.proxy_for("auth_gate") or plan.checkout_proxy or proxy
        if checkout_route:
            kwargs["checkout_proxy"] = checkout_route

        emit(account_ref, "auth_gate")
        manual_token = ""
        for token_email, token_value in (access_tokens or {}).items():
            if str(token_email).strip().lower() == email.strip().lower():
                manual_token = str(token_value or "").strip()
                break
        if manual_token:
            from .account_liveness import probe_account_liveness
            probe = probe_account_liveness({"email": email, "access_token": manual_token}, proxy=checkout_route, timeout=timeout)
            auth = {
                "ok": int(probe.get("status_code") or 0) == 200,
                "access_token": manual_token,
                "auth_context": {"email": email, "access_token": manual_token},
                "probed": True,
                "refreshed": False,
                "probe": probe,
                "error": "" if int(probe.get("status_code") or 0) == 200 else str(probe.get("error") or "token_invalid"),
            }
        else:
            auth = ensure_payment_access_token(
                email=email,
                proxy=checkout_route,
                timeout=timeout,
                relogin_on_401=jit_refresh,
                stabilization_probes=1,
            )
        auth_country = _registration_country(auth.get("auth_context"))
        registration_country = auth_country or seed_country
        expected_cell = _matrix_cell_for(index, cells, method, registration_country)
        matrix_route_mismatch = expected_cell != cell
        if matrix_route_mismatch:
            cell = expected_cell
        public_auth = public_payment_auth_result(auth)
        public_auth.pop("email", None)
        row: dict[str, Any] = {
            "index": index,
            "account_ref": _account_ref(email),
            "matrix_cell": str(cell.get("name") or "default"),
            "registration_country": registration_country,
            "auth": public_auth,
            "probed": bool(auth.get("probed")),
            "refreshed": bool(auth.get("refreshed")),
            "authenticated": bool(auth.get("ok")),
            "eligible": None,
            "capability_probed": False,
            "attempted": False,
            "ok": False,
            "decision": "",
            "error": "",
        }
        if not auth.get("ok"):
            row["decision"] = str(auth.get("error") or "jit_auth_failed")
            row["error"] = row["decision"]
            if auth.get("terminal"):
                row["retryable"] = False
            # account_deactivated 是永久终态。ensure_payment_access_token 已经把终态
            # 探测出来，但以前只写进 row["decision"] 就返回了，SQLite accounts 表
            # 不会被更新 —— 下次批量还会把这个 deactivated 账号选进来再 JIT 一次，
            # 白浪费一次 probe + 一次恢复链。这里显式落库，让后续批次过滤掉它。
            if auth.get("terminal") or row["decision"] == "account_deactivated":
                try:
                    from .account_recovery import _persist_permanent_deactivation, is_permanently_deactivated
                    seed_data, _ = load_account_seed(email=email)
                    if seed_data is not None and is_permanently_deactivated(seed_data):
                        _persist_permanent_deactivation(seed_data)
                        row["terminal_persisted"] = True
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    # 落库失败不影响批次主流程，只标记一下，避免拖垮整批。
                    row["terminal_persisted"] = False
                    row["terminal_persist_error"] = _canonical_sanitize_text(str(exc))
            row["status"] = "failed"
            emit(account_ref, "auth_gate", "failed", detail=row["decision"], account_terminal=True)
            timing = stage_state.get(account_ref, {})
            row.update({"stage_timings_ms": dict(timing.get("timings") or {}), "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000), "last_failed_stage": timing.get("last_failed_stage") or "auth_gate"})
            return index, row
        emit(account_ref, "auth_gate", "completed")
        if cell.get("matrix_mismatch") or matrix_route_mismatch:
            row["eligible"] = False
            row["decision"] = "matrix_registration_country_mismatch"
            row["error"] = row["decision"]
            row["retryable"] = False
            emit(account_ref, "validation", "failed", detail=row["decision"], account_terminal=True)
            timing = stage_state.get(account_ref, {})
            row.update({"stage_timings_ms": dict(timing.get("timings") or {}), "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000), "last_failed_stage": timing.get("last_failed_stage") or "validation"})
            return index, row
        if probe_only:
            emit(account_ref, "capability_probe")
            capability: dict[str, Any] = {}
            for probe_attempt in range(1, retry_count + 2):
                capability = probe_payment_method(
                    access_token=str(auth.get("access_token") or ""),
                    payment_method=method,
                    auth_context=auth.get("auth_context") if isinstance(auth.get("auth_context"), dict) else None,
                    proxy=checkout_route,
                    timeout=max(5, int(timeout or 30)),
                    **kwargs,
                )
                if capability.get("ok") or not _is_transient(capability) or probe_attempt > retry_count:
                    break
            public = _public_payment_result(capability)
            decision = str(public.get("decision") or public.get("error_code") or "capability_unknown")
            row.update(public)
            row.update({
                "auth": public_auth,
                "capability_probed": True,
                "attempted": False,
                "eligible": public.get("eligible") if isinstance(public.get("eligible"), bool) else None,
                "decision": decision,
                "attempts": probe_attempt,
            })
            emit(
                account_ref,
                "capability_probe",
                "completed" if row.get("ok") else "failed",
                account_terminal=True,
            )
            timing = stage_state.get(account_ref, {})
            row.update({"stage_timings_ms": dict(timing.get("timings") or {}), "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000), "last_failed_stage": timing.get("last_failed_stage")})
            return index, row
        if method == "paypal":
            emit(account_ref, "capability_probe")
            capability: dict[str, Any] = {}
            for probe_attempt in range(1, retry_count + 2):
                capability = probe_payment_method(
                    access_token=str(auth.get("access_token") or ""),
                    payment_method=method,
                    auth_context=auth.get("auth_context") if isinstance(auth.get("auth_context"), dict) else None,
                    proxy=checkout_route,
                    timeout=max(5, int(timeout or 30)),
                    **kwargs,
                )
                if capability.get("ok") or not _is_transient(capability) or probe_attempt > retry_count:
                    break
            public_capability = _public_payment_result(capability)
            probed_eligible = public_capability.get("eligible")
            if probed_eligible is not True:
                decision = str(
                    public_capability.get("decision")
                    or public_capability.get("error_code")
                    or ("trial_ineligible" if probed_eligible is False else "capability_unknown")
                )
                row.update(public_capability)
                row.update({
                    "auth": public_auth,
                    "capability_probed": True,
                    "attempted": False,
                    "eligible": probed_eligible if isinstance(probed_eligible, bool) else None,
                    "decision": decision,
                    "attempts": probe_attempt,
                    "error_stage": str(public_capability.get("error_stage") or "eligibility"),
                })
                if probed_eligible is False:
                    row["classification"] = "ineligible"
                    row["retryable"] = False
                    row["error_stage"] = "eligibility"
                emit(account_ref, "capability_probe", "failed", detail=decision, account_terminal=True)
                timing = stage_state.get(account_ref, {})
                row.update({"stage_timings_ms": dict(timing.get("timings") or {}), "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000), "last_failed_stage": timing.get("last_failed_stage") or "capability_probe"})
                return index, row
            row["capability_probed"] = True
            row["eligible"] = True
            emit(account_ref, "capability_probe", "completed")
        last: dict[str, Any] = {}
        for attempt in range(1, retry_count + 2):
            row["attempted"] = True
            emit(account_ref, "provider", attempt=attempt, max_attempts=retry_count + 1)

            def adapter_progress(event: dict[str, Any] | None = None) -> None:
                """Fold adapter-level stages into the batch event/timing stream.

                Adapters emit the detailed checkout/provider/redirect stages via
                the payment-link manager.  Sending those events directly to the
                caller used to leave the batch timing state stuck on ``provider``
                and made the desktop progress view lose the canonical fields.
                Re-emit through ``emit`` so every stage updates the same per-
                account timer and event schema.
                """
                payload = dict(event or {})
                stage = str(payload.pop("stage", "") or "provider").strip().lower()
                status = str(payload.pop("status", payload.pop("state", "running")) or "running")
                for key in (
                    "domain", "run_id", "batch_id", "account_ref", "operation", "method",
                    "attempt", "max_attempts", "account_terminal", "batch_terminal",
                    "duration_ms", "stage_timings_ms", "last_failed_stage",
                ):
                    payload.pop(key, None)
                emit(
                    account_ref,
                    stage,
                    status,
                    attempt=attempt,
                    max_attempts=retry_count + 1,
                    **payload,
                )

            last = generate_payment_link(
                access_token=str(auth.get("access_token") or ""),
                proxy=checkout_route,
                payment_method=method,
                operation_id=f"{batch_id}:{account_ref}",
                idempotency_key=f"{batch_id}:{account_ref}",
                auth_context=auth.get("auth_context") if isinstance(auth.get("auth_context"), dict) else None,
                progress=adapter_progress,
                **kwargs,
            )
            if last.get("ok") or not _is_transient(last) or attempt > retry_count:
                break
        authorization_queue: dict[str, Any] = {}
        if method == "paypal" and last.get("ok") and last.get("url"):
            try:
                from .paypal_authorization_queue import enqueue_paypal_ba_authorization

                authorization_queue = enqueue_paypal_ba_authorization(
                    email=email,
                    approval_url=str(last.get("url") or ""),
                    batch_id=batch_id,
                    account_ref=account_ref,
                    source_report=str(report_path),
                )
            except ValueError:
                # Hosted Checkout links can legitimately omit a BA token. Only
                # direct BA approval artifacts enter the follow-up queue.
                authorization_queue = {}
        public = _public_payment_result(last)
        decision = str(public.get("decision") or public.get("error_code") or ("ready" if public.get("ok") else "failed"))
        eligible = _eligible_from_result(method, public)
        row.update(public)
        if str(row.get("status") or "").lower() == "unknown":
            row["requires_reconciliation"] = True
        row.update({
            "auth": public_auth,
            "attempted": True,
            "eligible": eligible,
            "decision": decision,
            "attempts": attempt,
            "authorization_queued": bool(authorization_queue),
            "authorization_queue_id": str(authorization_queue.get("id") or ""),
            "authorization_status": str(authorization_queue.get("status") or ""),
        })
        emit(
            account_ref,
            "completed" if row.get("ok") else str(row.get("error_stage") or "failed"),
            "completed" if row.get("ok") else "failed",
            account_terminal=True,
        )
        timing = stage_state.get(account_ref, {})
        row.update({"stage_timings_ms": dict(timing.get("timings") or {}), "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000), "last_failed_stage": timing.get("last_failed_stage") or (str(row.get("error_stage") or "") if not row.get("ok") else "")})
        return index, row

    last_checkpoint_write = [0.0]

    def checkpoint(status: str, *, force: bool = False) -> dict[str, Any]:
        # Rebuilding + rewriting the full report on every future completion made
        # batch IO O(n²); running checkpoints are throttled to one write per
        # interval while terminal states always persist. A crash can lose at
        # most one interval of progress, which resume re-runs by signature.
        now = time.monotonic()
        if (
            status == "running"
            and not force
            and now - last_checkpoint_write[0] < _CHECKPOINT_MIN_INTERVAL_SECONDS
        ):
            return None
        last_checkpoint_write[0] = now
        results = [_sanitize_report_value(row) for row in ordered if row is not None]
        report = _build_report(
            batch_id=batch_id,
            method=method,
            started=started,
            workers=max_workers,
            probe_only=probe_only,
            selected_count=len(selected),
            results=results,
            cells=cells,
            report_path=report_path,
            status=status,
            resumed=len(selected) - len(pending),
            run_signature=run_signature,
            resume_checkpoint=resume_checkpoint,
        )
        with checkpoint_lock:
            _write_checkpoint(report_path, report)
        return report

    try:
        if pending:
            checkpoint("running", force=True)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(run_one, index, email): (index, email) for index, email in pending}
            for future in as_completed(futures):
                fallback_index, fallback_email = futures[future]
                try:
                    index, row = future.result()
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    index = fallback_index
                    row = {
                        "index": index,
                        "account_ref": _account_ref(fallback_email),
                        "matrix_cell": "unassigned",
                        "authenticated": False,
                        "eligible": None,
                        "attempted": False,
                        "ok": False,
                        "decision": "payment_worker_exception",
                        "error": _canonical_sanitize_text(f"{type(exc).__name__}: {exc}"),
                        "retryable": True,
                    }
                ordered[index] = row
                checkpoint("running")
        report = checkpoint("finished")
        if canary:
            report["canary_state"] = _record_canary_state(method, report)
            _write_checkpoint(report_path, report)
        return report
    finally:
        process_gate.release()


def _build_report(*, batch_id: str, method: str, started: float, workers: int,
                  probe_only: bool, selected_count: int, results: list[dict[str, Any]],
                  cells: list[dict[str, Any]], report_path: Path, status: str,
                  resumed: int, run_signature: str, resume_checkpoint: bool = False) -> dict[str, Any]:
    now = time.time()
    return {
        "ok": status == "finished" and bool(results) and all(bool(row.get("ok")) for row in results),
        "status": status,
        "batch_id": batch_id,
        "payment_method": method,
        "started_at": int(started),
        "updated_at": int(now),
        "finished_at": int(now) if status == "finished" else 0,
        "elapsed_seconds": round(now - started, 3),
        "workers": workers,
        "probe_only": bool(probe_only),
        "mode": "probe" if probe_only else "extract",
        "run_signature": run_signature,
        "resumed": resumed,
        "resume_checkpoint": bool(resume_checkpoint),
        "execution_mode": "断点恢复" if resume_checkpoint else "新执行",
        "counts": _batch_counts(results, selected_count),
        "matrix": _matrix_summary(results, cells),
        "results": results,
        "report_path": str(report_path),
    }


def _batch_counts(results: list[dict[str, Any]], requested: int) -> dict[str, int]:
    decisions = [str(row.get("decision") or "").lower() for row in results]
    terminal_states = [
        str(row.get("terminal_state") or row.get("status") or row.get("state") or "").lower()
        for row in results
    ]
    return {
        "requested": requested,
        "probed": sum(bool(row.get("probed")) for row in results),
        "refreshed": sum(bool(row.get("refreshed")) for row in results),
        "authenticated": sum(bool(row.get("authenticated")) for row in results),
        "eligible": sum(row.get("eligible") is True for row in results),
        "capability_probed": sum(bool(row.get("capability_probed")) for row in results),
        "capability_unknown": sum(
            bool(row.get("capability_probed") and str(row.get("classification") or "") == "unknown")
            for row in results
        ),
        "attempted": sum(bool(row.get("attempted")) for row in results),
        "completed": sum(bool(row.get("ok") and row.get("attempted")) for row in results),
        "trial_ineligible": sum("trial_ineligible" in value for value in decisions),
        "card_only": sum("card_only" in value or "promo_nonzero" in value for value in decisions),
        "approve_blocked": sum("approve" in value and "ready" not in value for value in decisions),
        "link_ready": sum(
            bool(row.get("ok") and (row.get("url") or row.get("url_present")))
            for row in results
        ),
        "qr_ready": sum(_is_qr_ready(row) for row in results),
        "terminal": sum(bool((row.get("auth") or {}).get("terminal")) for row in results),
        "failed": sum(not bool(row.get("ok")) for row in results),
        "cancelled": sum(state in {"cancelled", "canceled"} for state in terminal_states),
        "unknown": sum(state in {"unknown", "outcome_unknown"} for state in terminal_states),
        "timed_out": sum(state in {"timed_out", "timeout", "timeout_expired"} for state in terminal_states),
        "retryable": sum(row.get("retryable") is True for row in results),
    }


def _matrix_cell_for(index: int, cells: list[dict[str, Any]], method: str,
                     registration_country: str) -> dict[str, Any]:
    if not cells:
        return {"name": "default"}
    method_cells = [
        cell for cell in cells
        if not cell.get("payment_method")
        or normalize_payment_method(str(cell.get("payment_method") or "")) == method
    ]
    if not method_cells:
        return {"name": "unmatched", "matrix_mismatch": True}
    country = str(registration_country or "").strip().upper()
    if country:
        exact = [cell for cell in method_cells if str(cell.get("registration_country") or "").upper() == country]
        neutral = [cell for cell in method_cells if not str(cell.get("registration_country") or "").strip()]
        candidates = exact or neutral
        if not candidates:
            return {"name": "unmatched", "matrix_mismatch": True}
    else:
        candidates = method_cells
    schedule = [
        cell for cell in candidates
        for _ in range(max(1, int(cell.get("sample_size") or 1)))
    ]
    return dict(schedule[index % len(schedule)])


def _registration_country(context: Any) -> str:
    if not isinstance(context, dict):
        return ""
    return str(context.get("registration_country") or "").strip().upper()


def _matrix_summary(results: list[dict[str, Any]], cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = list(dict.fromkeys(str(cell.get("name") or "") for cell in cells)) or ["default"]
    for row in results:
        name = str(row.get("matrix_cell") or "")
        if name and name not in names:
            names.append(name)
    output = []
    for name in names:
        rows = [row for row in results if row.get("matrix_cell") == name]
        countries: dict[str, int] = {}
        for row in rows:
            country = str(row.get("registration_country") or "unknown")
            countries[country] = countries.get(country, 0) + 1
        output.append({"name": name, "registration_countries": countries, **_batch_counts(rows, len(rows))})
    return output


def _cell_payment_kwargs(
    base: dict[str, Any],
    cell: dict[str, Any],
    proxy: Any,
    *,
    payment_method: str = "",
    pool_index: int = 0,
) -> dict[str, Any]:
    """Overlay matrix values and compile the cell's reusable route plan."""
    values = dict(base)
    countries = dict(values.get("stage_proxy_countries") or {})
    mapping = {
        "checkout_country": "checkout",
        "promotion_country": "promotion",
        "provider_country": "provider",
        "approve_country": "approve",
        "redirect_country": "redirect",
    }
    for field, stage in mapping.items():
        country = str(cell.get(field) or "").strip().upper()
        if country:
            countries[stage] = country
    coerced_cell_approve, cell_approve_changed = coerce_approve_country(
        payment_method, countries.get("approve")
    )
    if cell_approve_changed:
        countries["approve"] = coerced_cell_approve
    coerced_base_approve, base_approve_changed = coerce_approve_country(
        payment_method, values.get("approve_country")
    )
    if base_approve_changed:
        values["approve_country"] = coerced_base_approve
    values["stage_proxy_countries"] = countries
    for key in ("strategy", "checkout_country", "target_country"):
        if cell.get(key) not in (None, ""):
            values[key] = cell[key]
    method = normalize_payment_method(payment_method) if str(payment_method or "").strip() else "gopay"
    if proxy:
        values["proxy"] = proxy
    elif values.get("checkout_proxy") and not parse_proxy_pool(values.get("checkout_proxy_pool")):
        values["proxy"] = values["checkout_proxy"]
    plan = PaymentRoutePlanner(CFG).plan(
        method,
        options=values,
        default_proxy=proxy,
        pool_offset=pool_index,
    )
    routed = {**values, **plan.to_adapter_options(), "payment_route_plan": plan}
    routed.pop("checkout_proxy_pool", None)
    routed.pop("approve_proxy_pool", None)
    return routed


def _eligible_from_result(method: str, result: dict[str, Any]) -> bool | None:
    if isinstance(result.get("eligible"), bool):
        return bool(result["eligible"])
    if method == "momo":
        if result.get("has_momo") is True and result.get("amount_due") == 0:
            return True
        decision = str(result.get("decision") or "")
        if decision in {"account_trial_ineligible", "card_only_full_price", "promo_nonzero", "momo_not_enabled"}:
            return False
    if method == "kakao":
        if result.get("has_kakao") is True and result.get("amount_due") == 0:
            return True
        if result.get("has_kakao") is False or str(result.get("decision") or "") in {"nonzero_offer", "kakao_not_enabled"}:
            return False
    return None


def _is_qr_ready(row: dict[str, Any]) -> bool:
    return bool(
        row.get("ok")
        and str(row.get("decision") or "") == "ready_with_qr"
        and (
            row.get("qr_path")
            or row.get("qr_path_present")
            or row.get("url_present")
            or "payment.momo.vn" in str(row.get("url") or "").lower()
        )
    )


def _is_transient(result: dict[str, Any]) -> bool:
    return payment_retry_allowed(result)


def _public_payment_result(result: dict[str, Any]) -> dict[str, Any]:
    blocked = {"access_token", "auth_context", "raw_output", "raw_output_tail", "state_history"}
    token_metadata = {
        "token_telemetry", "token_hash", "token_changed",
        "authorization_queued", "authorization_queue_id", "authorization_status",
    }
    return {
        key: value
        for key, value in dict(result or {}).items()
        if key not in blocked and ("token" not in key.lower() or key.lower() in token_metadata)
    }


def _sanitize_report_value(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    blocked = {"email", "access_token", "refresh_token", "id_token", "auth_context", "password"}
    token_metadata = {
        "token_telemetry", "token_hash", "token_changed",
        "authorization_queued", "authorization_queue_id", "authorization_status",
    }
    if lowered in blocked or "proxy" in lowered or ("token" in lowered and lowered not in token_metadata):
        return None
    if isinstance(value, dict):
        return {
            item_key: sanitized
            for item_key, item_value in value.items()
            if (sanitized := _sanitize_report_value(item_value, str(item_key))) is not None
        }
    if isinstance(value, list):
        return [_sanitize_report_value(item) for item in value]
    return _canonical_sanitize(value, key=key)


def _unique_emails(emails: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in emails or []:
        email = str(value or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            output.append(email)
    return output


def _account_ref(email: str) -> str:
    import hashlib
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _safe_batch_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:80]


def _report_path(batch_id: str, *, probe_only: bool = False) -> Path:
    suffix = ".probe" if probe_only else ".extract"
    return runtime_file(CFG, "payment_batches") / f"{batch_id}{suffix}.json"


def _event_path(batch_id: str, *, probe_only: bool = False) -> Path:
    suffix = ".probe" if probe_only else ".extract"
    return runtime_file(CFG, "payment_batches") / f"{batch_id}{suffix}.events.jsonl"


def _append_event(path: Path, event: dict[str, Any], run_signature: str, lock: threading.Lock) -> None:
    record = _canonical_sanitize({"run_signature": run_signature, "event": event})
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _replay_events(path: Path, run_signature: str, progress: Callable[[dict[str, Any]], None] | None) -> None:
    if progress is None or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
            if record.get("run_signature") != run_signature or not isinstance(record.get("event"), dict):
                continue
            progress({**record["event"], "replayed": True})
        except (ValueError, TypeError, json.JSONDecodeError):
            continue


def _reset_event_log(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _load_checkpoint(path: Path, method: str, run_signature: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("payment_method") != method
        or value.get("run_signature") != run_signature
    ):
        return {}
    return value


def _checkpoint_row_resumable(row: dict[str, Any]) -> bool:
    """Resume only completed success or an explicitly non-retryable failure."""
    contract = PaymentResult.from_mapping(row)
    if contract.outcome.requires_reconciliation or contract.outcome.side_effect_started and not contract.ok:
        return True
    if row.get("ok") is True:
        return True
    return row.get("ok") is False and row.get("retryable") is False


def _batch_run_signature(
    *,
    method: str,
    probe_only: bool,
    jit_refresh: bool,
    matrix: list[dict[str, Any]],
    payment_kwargs: dict[str, Any],
    proxy: Any,
    retries: int,
) -> str:
    payload = {
        "version": 2,
        "payment_method": method,
        "probe_only": bool(probe_only),
        "jit_refresh": bool(jit_refresh),
        "matrix": matrix,
        # Values are hashed immediately and never persisted; retaining the raw
        # material here ensures credential or route changes invalidate resume.
        "payment_kwargs": payment_kwargs,
        "proxy": proxy or "",
        "retries": int(retries),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    persisted = payment_history_metadata(report)
    temporary.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _canary_state_path() -> Path:
    return runtime_file(CFG, "payment_canary_state.json")


def _active_canary_pause(batch_cfg: dict[str, Any], method: str) -> dict[str, Any]:
    if not bool(batch_cfg.get("pause_on_canary_failure", True)):
        return {}
    path = _canary_state_path()
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    try:
        ttl = max(60, int(batch_cfg.get("canary_pause_seconds") or 21600))
    except (TypeError, ValueError):
        ttl = 21600
    if state.get("payment_method") != method:
        return {}
    if state.get("paused") and time.time() - int(state.get("updated_at") or 0) < ttl:
        return state
    return {}


def _record_canary_state(method: str, report: dict[str, Any]) -> dict[str, Any]:
    rows = report.get("results") if isinstance(report.get("results"), list) else []
    probe_only = bool(report.get("probe_only"))
    evaluated = [
        row for row in rows
        if (row.get("capability_probed") if probe_only else row.get("attempted"))
    ]
    completed = (
        sum(bool(row.get("conclusive")) for row in evaluated)
        if probe_only
        else int((report.get("counts") or {}).get("completed") or 0)
    )
    conclusive_offer = {
        "account_trial_ineligible", "card_only_full_price", "promo_nonzero", "momo_not_enabled",
        "nonzero_offer", "wrong_currency", "kakao_not_enabled", "credential_invalid", "account_deactivated",
    }
    conclusive_offer.update({"payment_method_unavailable", "nonzero_offer"})
    decisions = {str(row.get("decision") or "") for row in evaluated}
    systemic = bool(evaluated and not completed and decisions and not decisions.issubset(conclusive_offer))
    state = {
        "payment_method": method,
        "probe_only": probe_only,
        "paused": systemic,
        "reason": "protocol_profile_canary_failed" if systemic else "",
        "attempted": sum(bool(row.get("attempted")) for row in evaluated),
        "capability_probed": sum(bool(row.get("capability_probed")) for row in evaluated),
        "completed": completed,
        "decisions": sorted(decisions),
        "updated_at": int(time.time()),
    }
    path = _canary_state_path()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
