"""First-class batch executor for protocol payment extraction."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from ..store.account_seed import load_account_seed
from ..config import CFG
from ..kernel.cross_process_gate import CrossProcessSemaphore, GateTimeoutError
from .auth import ensure_payment_access_token, public_payment_auth_result
from .link_manager import (
    coerce_approve_country,
    generate_payment_link,
    normalize_payment_method,
    parse_proxy_pool,
    probe_payment_method,
)
from .routing import PaymentRoutePlanner
from .catalog import PAYMENT_METHODS, offline_method_error
from .batch_report import (
    account_ref as _account_ref,
    active_canary_pause as _active_canary_pause_impl,
    append_event as _append_event,
    batch_run_signature as _batch_run_signature,
    build_report as _build_report,
    checkpoint_row_resumable as _checkpoint_row_resumable,
    eligible_from_result as _eligible_from_result,
    event_path_for as _event_path_for,
    is_transient as _is_transient,
    load_checkpoint as _load_checkpoint,
    matrix_cell_for as _matrix_cell_for_impl,
    public_payment_result as _public_payment_result,
    record_canary_state as _record_canary_state_impl,
    registration_country as _registration_country,
    replay_events as _replay_events,
    report_path_for as _report_path_for,
    reset_event_log as _reset_event_log,
    runtime_file,
    safe_batch_id as _safe_batch_id,
    sanitize_report_value as _sanitize_report_value,
    sanitize_text as _canonical_sanitize_text,
    unique_emails as _unique_emails,
    write_checkpoint as _write_checkpoint,
)
from .batch_setup import apply_configured_stage_pools

_batch_counts = __import__("sms_tool.payment.batch_report", fromlist=["batch_counts"]).batch_counts
_is_qr_ready = __import__("sms_tool.payment.batch_report", fromlist=["is_qr_ready"]).is_qr_ready
_matrix_summary = __import__("sms_tool.payment.batch_report", fromlist=["matrix_summary"]).matrix_summary

# Minimum spacing between "running" checkpoint rewrites of the batch report
# (terminal states always persist immediately).
_CHECKPOINT_MIN_INTERVAL_SECONDS = 2.0


def load_payment_matrix(value: Any = None) -> list[dict[str, Any]]:
    """Load matrix cells from a JSON string/path or protocol_payments config."""
    raw = value
    if raw in (None, "", False):
        _protocol_raw: Any = CFG.get("protocol_payments")
        protocol: dict[str, Any] = _protocol_raw if isinstance(_protocol_raw, dict) else {}
        raw = protocol.get("matrix") or []
    if isinstance(raw, (str, Path)):
        explicit_path = isinstance(raw, Path)
        text = str(raw).strip()
        if not text:
            return []
        source = text
        path: Path | None = None
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
            location = f"file {path}" if path is not None else "inline JSON"
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
    offline_error = offline_method_error(method, probe_only=probe_only)
    if offline_error is not None:
        raise offline_error
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
    _protocol_cfg_raw: Any = CFG.get("protocol_payments")
    protocol_cfg: dict[str, Any] = _protocol_cfg_raw if isinstance(_protocol_cfg_raw, dict) else {}
    _batch_raw: Any = protocol_cfg.get("batch")
    batch_cfg: dict[str, Any] = _batch_raw if isinstance(_batch_raw, dict) else {}
    if not canary and not probe_only:
        paused = _active_canary_pause(batch_cfg, method)
        if paused:
            raise RuntimeError(f"payment_batch_paused_by_canary:{paused.get('reason') or 'protocol_profile_failed'}")
    _method_caps_raw: Any = batch_cfg.get("method_workers")
    method_caps: dict[str, Any] = _method_caps_raw if isinstance(_method_caps_raw, dict) else {}
    default_cap = 2 if method in {"momo", "kakao"} else 4
    cap = max(1, int(method_caps.get(method) or default_cap))
    max_workers = max(1, min(int(workers or 1), cap, len(selected) or 1))
    retry_count = max(0, min(int(retries or 0), 5))
    base_kwargs = dict(payment_kwargs or {})
    base_kwargs = apply_configured_stage_pools(
        base_kwargs, protocol_config=protocol_cfg, legacy_config=CFG, method=method, proxy=proxy
    )
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
            if (isinstance(row, dict) and row.get("account_ref") and _checkpoint_row_resumable(row))
        }
        ordered: list[dict[str, Any] | None] = [existing_by_ref.get(_account_ref(email)) for email in selected]
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
        now_mono = time.monotonic()
        state = stage_state.setdefault(
            account_ref,
            {"started": now_mono, "stage": "", "stage_started": now_mono, "timings": {}, "last_failed_stage": ""},
        )
        if state["stage"] and state["stage"] != stage:
            state["timings"][state["stage"]] = state["timings"].get(state["stage"], 0) + int(
                (now_mono - state["stage_started"]) * 1000
            )
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

    last_checkpoint_write = [0.0]

    def checkpoint(status: str, *, force: bool = False) -> dict[str, Any]:
        # Rebuilding + rewriting the full report on every future completion made
        # batch IO O(n²); running checkpoints are throttled to one write per
        # interval while terminal states always persist. A crash can lose at
        # most one interval of progress, which resume re-runs by signature.
        now = time.monotonic()
        if status == "running" and not force and now - last_checkpoint_write[0] < _CHECKPOINT_MIN_INTERVAL_SECONDS:
            return {}
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
            futures = {
                executor.submit(
                    _run_one_row,
                    index,
                    email,
                    method=method,
                    cells=cells,
                    base_kwargs=base_kwargs,
                    proxy=proxy,
                    timeout=timeout,
                    jit_refresh=jit_refresh,
                    probe_only=probe_only,
                    retry_count=retry_count,
                    access_tokens=access_tokens,
                    emit=emit,
                    batch_id=batch_id,
                    report_path=report_path,
                    stage_state=stage_state,
                ): (index, email)
                for index, email in pending
            }
            for future in as_completed(futures):
                fallback_index, fallback_email = futures[future]
                try:
                    index, row = future.result()
                except Exception as exc:  # noqa: BLE001 - see _worker_exception_row
                    index = fallback_index
                    row = _worker_exception_row(index, fallback_email, exc)
                ordered[index] = row
                checkpoint("running")
        report = checkpoint("finished")
        if canary:
            report["canary_state"] = _record_canary_state(method, report)
            _write_checkpoint(report_path, report)
        return report
    finally:
        # Reached only when ``acquire`` succeeded -- the timeout branch above
        # raises, so we never discard a gate another process still holds.
        # ``batch_id`` is unique per run, so after release nobody will acquire
        # this gate again and its slot file is pure litter (3,949 empty ones had
        # accumulated before this line existed).
        process_gate.release()
        process_gate.discard()


def _run_one_row(
    index: int,
    email: str,
    *,
    method: str,
    cells: list[dict[str, Any]],
    base_kwargs: dict[str, Any],
    proxy: Any,
    timeout: int,
    jit_refresh: bool,
    probe_only: bool,
    retry_count: int,
    access_tokens: dict[str, str] | None,
    emit: Callable[..., None],
    batch_id: str,
    report_path: Path,
    stage_state: dict[str, dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    """Run one account's JIT auth, capability probe and payment execution.

    Extracted verbatim from the ``run_one`` closure (2026-10-23). The parameter
    names are exactly the names the body already used, so the body is unchanged
    apart from the dedent: the closure became an explicit argument list instead
    of a hidden one. ``run_payment_batch`` keeps the orchestration (checkpoint,
    executor, report) and submits this function.
    """
    account_ref = _account_ref(email)
    emit(account_ref, "routing")
    seed_context, _ = load_account_seed(email=email)
    seed_country = _registration_country(seed_context)
    cell = _matrix_cell_for(index, cells, method, seed_country)
    # P1-4: the UPI lane takes an exclusive per-exit lease for this cell, so two
    # workers (or two processes) cannot dial the same credential at once.
    lease_owner = _lease_owner_for(method, account_ref)
    kwargs = _cell_payment_kwargs(
        base_kwargs,
        cell,
        proxy,
        payment_method=method,
        pool_index=index,
        lease_owner=lease_owner,
    )
    kwargs.pop("proxy", None)
    plan = kwargs.get("payment_route_plan")
    if plan is None:
        plan = PaymentRoutePlanner(CFG, lease_owner=lease_owner).plan(
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
        from ..accounts.account_liveness import probe_account_liveness

        probe = probe_account_liveness(
            {"email": email, "access_token": manual_token}, proxy=checkout_route, timeout=timeout
        )
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
                from ..accounts.account_recovery import _persist_permanent_deactivation, is_permanently_deactivated

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
        row.update(
            {
                "stage_timings_ms": dict(timing.get("timings") or {}),
                "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000),
                "last_failed_stage": timing.get("last_failed_stage") or "auth_gate",
            }
        )
        return index, row
    emit(account_ref, "auth_gate", "completed")
    if cell.get("matrix_mismatch") or matrix_route_mismatch:
        row["eligible"] = False
        row["decision"] = "matrix_registration_country_mismatch"
        row["error"] = row["decision"]
        row["retryable"] = False
        emit(account_ref, "validation", "failed", detail=row["decision"], account_terminal=True)
        timing = stage_state.get(account_ref, {})
        row.update(
            {
                "stage_timings_ms": dict(timing.get("timings") or {}),
                "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000),
                "last_failed_stage": timing.get("last_failed_stage") or "validation",
            }
        )
        return index, row
    if probe_only:
        emit(account_ref, "capability_probe")
        capability: dict[str, Any] = {}
        probe_attempt = 0
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
        row.update(
            {
                "auth": public_auth,
                "capability_probed": True,
                "attempted": False,
                "eligible": public.get("eligible") if isinstance(public.get("eligible"), bool) else None,
                "decision": decision,
                "attempts": probe_attempt,
            }
        )
        emit(
            account_ref,
            "capability_probe",
            "completed" if row.get("ok") else "failed",
            account_terminal=True,
        )
        timing = stage_state.get(account_ref, {})
        row.update(
            {
                "stage_timings_ms": dict(timing.get("timings") or {}),
                "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000),
                "last_failed_stage": timing.get("last_failed_stage"),
            }
        )
        return index, row
    if method == "paypal":
        emit(account_ref, "capability_probe")
        capability: dict[str, Any] = {}
        probe_attempt = 0
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
            row.update(
                {
                    "auth": public_auth,
                    "capability_probed": True,
                    "attempted": False,
                    "eligible": probed_eligible if isinstance(probed_eligible, bool) else None,
                    "decision": decision,
                    "attempts": probe_attempt,
                    "error_stage": str(public_capability.get("error_stage") or "eligibility"),
                }
            )
            if probed_eligible is False:
                row["classification"] = "ineligible"
                row["retryable"] = False
                row["error_stage"] = "eligibility"
            emit(account_ref, "capability_probe", "failed", detail=decision, account_terminal=True)
            timing = stage_state.get(account_ref, {})
            row.update(
                {
                    "stage_timings_ms": dict(timing.get("timings") or {}),
                    "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000),
                    "last_failed_stage": timing.get("last_failed_stage") or "capability_probe",
                }
            )
            return index, row
        row["capability_probed"] = True
        row["eligible"] = True
        emit(account_ref, "capability_probe", "completed")
    last: dict[str, Any] = {}
    attempt = 0
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
                "domain",
                "run_id",
                "batch_id",
                "account_ref",
                "operation",
                "method",
                "attempt",
                "max_attempts",
                "account_terminal",
                "batch_terminal",
                "duration_ms",
                "stage_timings_ms",
                "last_failed_stage",
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
    # Release whatever this cell took, whatever the verdict (a crashed cell is
    # covered by the lease TTL).
    _release_leases(lease_owner)
    authorization_queue: dict[str, Any] = {}
    if method == "paypal" and last.get("ok") and last.get("url"):
        try:
            from ..paypal.authorization_queue import enqueue_paypal_ba_authorization

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
    row.update(
        {
            "auth": public_auth,
            "attempted": True,
            "eligible": eligible,
            "decision": decision,
            "attempts": attempt,
            "authorization_queued": bool(authorization_queue),
            "authorization_queue_id": str(authorization_queue.get("id") or ""),
            "authorization_status": str(authorization_queue.get("status") or ""),
        }
    )
    emit(
        account_ref,
        "completed" if row.get("ok") else str(row.get("error_stage") or "failed"),
        "completed" if row.get("ok") else "failed",
        account_terminal=True,
    )
    timing = stage_state.get(account_ref, {})
    row.update(
        {
            "stage_timings_ms": dict(timing.get("timings") or {}),
            "total_duration_ms": int((time.monotonic() - timing.get("started", time.monotonic())) * 1000),
            "last_failed_stage": timing.get("last_failed_stage")
            or (str(row.get("error_stage") or "") if not row.get("ok") else ""),
        }
    )
    return index, row


def _worker_exception_row(index: int, email: str, exc: BaseException) -> dict[str, Any]:
    """Per-row fallback for an exception that escaped ``run_one``.

    The worker loop below is the only place a checkpoint is written, so an
    exception escaping ``future.result()`` aborts ``as_completed`` and loses the
    report for every row that already finished. The catch used to be
    ``(OSError, ValueError, TypeError, RuntimeError)``; a ``KeyError`` /
    ``AttributeError`` / ``AssertionError`` from a provider payload, or any
    custom ``Exception`` subclass, took the whole batch down with them. Catching
    ``Exception`` is what makes "the batch always writes a checkpoint" true.

    ``retryable`` still follows the shared taxonomy: a programming or
    configuration defect is not fixed by re-running the account, while
    transport/unknown failures stay retryable. ``failure_class`` records the
    branch so an operator does not have to re-derive it from the message.
    """
    # Function-local: the shared classifier is a leaf utility and this is its
    # only call site in the payment lane, so a module-level edge would grow the
    # layer ratchet for one use. The delayed-import ratchet records the reason.
    from ..kernel.error_classification import INTERNAL_EXCEPTION_TYPES, classify_error

    # ``INTERNAL_EXCEPTION_TYPES`` is the same object the registration stage
    # executors use (owner: ``kernel.error_classification``); re-running the
    # account cannot fix a code/contract defect, so such a row is not requeued.
    internal = isinstance(exc, INTERNAL_EXCEPTION_TYPES)

    failure_class = "internal" if internal else classify_error(f"{type(exc).__name__}: {exc}")
    return {
        "index": index,
        "account_ref": _account_ref(email),
        "matrix_cell": "unassigned",
        "authenticated": False,
        "eligible": None,
        "attempted": False,
        "ok": False,
        "decision": "payment_worker_exception",
        "failure_class": failure_class,
        "error": _canonical_sanitize_text(f"{type(exc).__name__}: {exc}"),
        "retryable": failure_class not in {"internal", "configuration", "cancelled"},
    }


def _matrix_cell_for(index: int, cells: list[dict[str, Any]], method: str, registration_country: str) -> dict[str, Any]:
    return _matrix_cell_for_impl(
        index,
        cells,
        method,
        registration_country,
        normalize_method=normalize_payment_method,
    )


def _lease_owner_for(method: str, account_ref: str) -> str:
    """Owner id for per-exit lease (unused after retiring local UPI extractors)."""
    return ""


def _release_leases(owner: str) -> None:
    """No-op after retiring local UPI exit leases."""
    return None


def _cell_payment_kwargs(
    base: dict[str, Any],
    cell: dict[str, Any],
    proxy: Any,
    *,
    payment_method: str = "",
    pool_index: int = 0,
    lease_owner: str = "",
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
    coerced_cell_approve, cell_approve_changed = coerce_approve_country(payment_method, countries.get("approve"))
    if cell_approve_changed:
        countries["approve"] = coerced_cell_approve
    coerced_base_approve, base_approve_changed = coerce_approve_country(payment_method, values.get("approve_country"))
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
    plan = PaymentRoutePlanner(CFG, lease_owner=lease_owner).plan(
        method,
        options=values,
        default_proxy=proxy,
        pool_offset=pool_index,
    )
    routed = {**values, **plan.to_adapter_options(), "payment_route_plan": plan}
    routed.pop("checkout_proxy_pool", None)
    routed.pop("approve_proxy_pool", None)
    return routed


def _report_path(batch_id: str, *, probe_only: bool = False) -> Path:
    return _report_path_for(CFG, batch_id, probe_only=probe_only)


def _event_path(batch_id: str, *, probe_only: bool = False) -> Path:
    return _event_path_for(CFG, batch_id, probe_only=probe_only)


def _canary_state_path() -> Path:
    return runtime_file(CFG, "payment_canary_state.json")


def _active_canary_pause(batch_cfg: dict[str, Any], method: str) -> dict[str, Any]:
    return _active_canary_pause_impl(batch_cfg, method, state_path_fn=_canary_state_path)


def _record_canary_state(method: str, report: dict[str, Any]) -> dict[str, Any]:
    return _record_canary_state_impl(method, report, state_path=_canary_state_path())
