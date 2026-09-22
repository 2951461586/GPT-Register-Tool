"""CLI boundary for registration pipeline commands.

The registration domain modules (``sms_tool.registration``, ``batch_runner``,
``storage``) own protocol behavior and persistence.  This module only
orchestrates ``argparse`` values into those domain calls.
``RegistrationCommandContext`` keeps the legacy CLI's replaceable hooks
explicit so tests can continue patching ``sms_tool.cli`` symbols.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import unique_emails
from ..batch_runner import filter_registered_mailboxes
from ..payment_operation import PaymentOperationConflict, PaymentOperationStore
from ..proxy_entry import parse_proxy
from ..sanitizer import mask_account, sanitize_log_text
from ..diagnostics import safe_print

logger = logging.getLogger(__name__)

# Free-text markers the WPF host matches against this process's stdout.
# They are a contract in the weakest possible form -- a bare substring, no
# version, no schema -- and the host silently stops reacting if one changes.
# Keep each value byte-identical to `BackendTextMarkers` in
# SmsWorkbench.Contracts; tests/test_backend_text_markers.py asserts that.
SAVED_SESSION_MARKER = "Saved session:"


@dataclass(frozen=True)
class RegistrationCommandContext:
    """Legacy CLI hooks required by registration command orchestration."""

    proxy_pool_values: Callable[[Any], list[str]]
    load_mailbox_pool: Callable[[Any], list[Any]]
    run_batch: Callable[..., Any]
    run_email: Callable[..., Any]
    build_session_file: Callable[[Any], dict[str, Any]]
    save_results: Callable[..., Any]
    check_registered_promotions: Callable[..., Any]
    import_registered_accounts: Callable[..., Any]
    registration_phone_pool: Callable[[Any], Any]
    upsert_account: Callable[..., Any]
    database_path: Callable[[], str]
    runtime_file: Callable[[str], Path]
    runtime_config: Mapping[str, Any]


def _preflight_host_label(candidate: Any) -> str:
    """Credential-free ``host:port`` for progress lines and per-host accounting."""
    entry = parse_proxy(candidate)
    if entry is None:
        return str(candidate or "").strip() or "-"
    return f"{entry.host}:{entry.port}"


def _preflight_limits(config: Mapping[str, Any] | None) -> tuple[int, float]:
    """``(per-host consecutive-failure cap, wall-clock budget seconds)``.

    Defaults come from the 2026-09-13 outage: a 30-candidate, three-provider pool
    where every route was dead burned 10m51s of a silent black screen before
    ``exit 2``. Both guards only ever *shorten* an all-fail run -- a single
    healthy candidate still returns immediately, so they cannot hide a working
    route that the unbounded loop would have found.
    """
    section = ((config or {}).get("registration") or {})
    if not isinstance(section, Mapping):
        section = {}

    def _int(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(section.get(name, default))))
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(section.get(name, default))))
        except (TypeError, ValueError):
            return default

    return (
        _int("preflight_max_consecutive_failures_per_host", 3, 1, 50),
        _float("preflight_budget_seconds", 180.0, 10.0, 3600.0),
    )


def preflight_registration_before_mailbox(args: Any, ctx: RegistrationCommandContext) -> dict:
    """Select a healthy auth route before a paid/disposable mailbox is claimed."""
    from ..config import validate_registration_driver_config

    candidates = ctx.proxy_pool_values(args) or [None]
    selected_proxy = next(
        (str(candidate).strip() for candidate in candidates if str(candidate or "").strip()),
        None,
    )
    # Keep this before proxy/network checks and mailbox loading.  Browser
    # credentials and cloud-provider proxy ownership are static configuration;
    # a mismatch must not consume a paid mailbox or validate an unrelated route.
    selected_driver = validate_registration_driver_config(
        ctx.runtime_config,
        getattr(args, "registration_driver", None),
        proxy=selected_proxy,
    )
    # Every remaining driver launches its browser locally, so a Sentinel/
    # ChatGPT probe from this process measures the real registration egress.
    from ..registration import registration_network_preflight

    per_host_limit, budget_seconds = _preflight_limits(ctx.runtime_config)
    total = len(candidates)
    started = time.time()
    last_error = None
    host_failures: dict[str, int] = {}
    attempted = 0
    skipped_hosts: dict[str, int] = {}
    successful_routes: list[tuple[str, dict]] = []

    safe_print(f"[*] 注册预检：{total} 个候选路由（单候选上限 {per_host_limit} 次、总预算 {budget_seconds:.0f}s）")

    # 并发探测（2026-09-18）：串行时启动开销与候选数成正比 —— 实测 10 个候选
    # 124s，而 ``select_registration_proxy_pool`` 早就在用
    # ``ThreadPoolExecutor(max_workers=8)`` 探同一个池子。单候选**内部**仍按
    # ``proxy_attempts`` 重试；这里的并发只跨候选。
    #
    # 每个主机**每轮的在飞上限**（allowance）分两档：
    #
    # * 从没探过、或有过失败记录 ⇒ ``per_host_limit``。这一档保住既有的
    #   「同一出口连续失败 N 次就不再浪费探测」守卫：坏出口最多只吃 N 个探测。
    #   因为它们是并发的，「整池死掉」从串行的 N × 单次耗时 变成**一轮**。
    # * 已经探过且记录干净 ⇒ 整个窗口。健康的出口没必要限速 —— 这一档才拿到
    #   单出口池的提速（10 候选：先 3 并发探 3 个，确认健康后 7 并发探完）。
    #
    # 逐候选的判定顺序（主机守卫 → 预算 → 探测）、进度行的
    # ``序号/总数 主机 结果（耗时）`` 形态、以及 ``successful_routes`` 的
    # **候选序**都保持不变。变的只有探测的发起顺序：并发 ⇒ 完成序不再等于候选序。
    workers = max(1, min(8, total))
    probed_hosts: set[str] = set()
    pending = list(enumerate(candidates, start=1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while pending:
            batch: list[tuple[int, Any]] = []
            deferred: list[tuple[int, Any]] = []
            in_batch_per_host: dict[str, int] = {}
            timed_out = False
            for index, candidate in pending:
                label = _preflight_host_label(candidate)
                if host_failures.get(label, 0) >= per_host_limit:
                    # One endpoint failing its first N candidates is unlikely to
                    # answer the N+1th. Skip the rest from that endpoint instead
                    # of spending the entire batch-start budget on equivalent
                    # routes.
                    skipped_hosts[label] = skipped_hosts.get(label, 0) + 1
                    continue
                elapsed_total = time.time() - started
                if elapsed_total > budget_seconds:
                    safe_print(
                        f"[!] 注册预检超时：已用 {elapsed_total:.0f}s / 预算 {budget_seconds:.0f}s，"
                        f"放弃剩余候选（已试 {attempted}/{total}）"
                    )
                    timed_out = True
                    break
                proven = label in probed_hosts and not host_failures.get(label, 0)
                allowance = workers if proven else per_host_limit
                if in_batch_per_host.get(label, 0) >= allowance or len(batch) >= workers:
                    deferred.append((index, candidate))
                    continue
                in_batch_per_host[label] = in_batch_per_host.get(label, 0) + 1
                batch.append((index, candidate))
            if not batch:
                break
            attempted += len(batch)
            batch_started = time.time()
            futures = {
                executor.submit(
                    registration_network_preflight, candidate, proxy_attempts=2
                ): (index, candidate, _preflight_host_label(candidate))
                for index, candidate in batch
            }
            batch_failures: dict[str, int] = {}
            batch_ok_hosts: set[str] = set()
            probes: dict[int, tuple[str, dict]] = {}
            for future in as_completed(futures):
                index, candidate, label = futures[future]
                elapsed = time.time() - batch_started
                try:
                    result = future.result()
                except Exception as exc:
                    last_error = exc
                    batch_failures[label] = batch_failures.get(label, 0) + 1
                    safe_print(
                        f"[!] 注册预检 {index}/{total} {label} 失败 "
                        f"({elapsed:.1f}s)：{sanitize_log_text(exc)[:160]}"
                    )
                    continue
                batch_ok_hosts.add(label)
                safe_print(f"[*] 注册预检 {index}/{total} {label} 可用（{elapsed:.1f}s）")
                probes[index] = (str(result.get("proxy") or candidate or "").strip(), result)
            # 主机计数按**整批**结算，不按完成序 —— 否则「成功清零 / 失败累加」
            # 的先后会让同一个出口的计数随线程调度随机化。
            for label, failures in batch_failures.items():
                if label not in batch_ok_hosts:
                    host_failures[label] = host_failures.get(label, 0) + failures
            for label in batch_ok_hosts:
                host_failures[label] = 0
            probed_hosts.update(label for _index, _candidate, label in futures.values())
            # ``successful_routes`` 必须保持候选序：调用方取 ``[0]`` 当
            # ``args.proxy``，并发下完成序是随机的。
            successful_routes.extend(probes[index] for index in sorted(probes))
            pending = [] if timed_out else deferred

    if skipped_hosts:
        summary = ", ".join(f"{host}×{count}" for host, count in sorted(skipped_hosts.items()))
        safe_print(f"[!] 注册预检：以下出口主机连续失败已达上限，剩余候选已跳过：{summary}")
    if successful_routes:
        healthy = list(dict.fromkeys(route for route, _result in successful_routes if route))
        args.proxy_pool = "\n".join(healthy)
        args.proxy = healthy[0] if healthy else None
        safe_print(
            f"[*] 注册预检完成：{len(successful_routes)}/{attempted} 条实测可用，"
            "批次只使用通过 OpenAI 边界检查的路由"
        )
        return successful_routes[0][1]
    raise RuntimeError(
        "registration_preflight_failed:no_healthy_route:"
        + (type(last_error).__name__ if last_error is not None else "unknown")
    )


def registration_phone_pool(args: Any):
    """Create the configured phone pool for registration flows that require SMS."""
    if getattr(args, "no_phone_reuse", False) or getattr(args, "registration_at_only", False):
        return None

    from ..phone_reuse import create_phone_pool, has_phone_reuse_config, print_phone_pool_status

    explicit = bool(getattr(args, "phone_reuse", False))
    auto_enable = has_phone_reuse_config()
    if not explicit and not auto_enable:
        return None

    phone_pool = create_phone_pool(
        max_reuse_count=getattr(args, "max_reuse_count", 0),
        send_cooldown_seconds=getattr(args, "phone_send_cooldown", None),
        source_override=getattr(args, "phone_source", None),
    )
    if not phone_pool.phones:
        if explicit:
            safe_print("[Error] --phone-reuse enabled but no phone numbers configured. Add phone_reuse.smsbower.api_key, SMSBOWER_API_KEY, phone_reuse.phone_pool, or paypal_auto.phone_numbers")
            raise SystemExit(2)
        return None

    if auto_enable and not explicit:
        first = phone_pool.phones[0] if phone_pool.phones else None
        source = first.provider if first else "configured"
        safe_print(f"[*] Auto-enabled phone verification ({source} mode)")
    print_phone_pool_status(phone_pool)
    return phone_pool


def check_registered_promotions(emails, workers=4, proxy=None, timeout=20, proxy_pool=None, payment_eligibility=True):
    """Probe plan/promotion for saved accounts.

    ``proxy_pool`` is threaded through so this entry point accepts the same
    health-pool rotation as ``commands/accounts.py::check_promotion``. It
    previously had no such parameter, so a pool supplied by the caller was
    silently dropped on this path.

    ``payment_eligibility`` (default on) additionally enumerates each account's
    available payment methods into ``raw_json.payment_capability``; see
    ``accounts/account_payment_eligibility.py``.
    """
    from ..accounts.account_promotion import refresh_promotion_statuses
    from ..sanitizer import sanitize

    targets = unique_emails(emails)
    if not targets:
        report = {"ok": True, "total": 0, "success": 0, "failed": 0, "trial_eligible": 0, "results": []}
        safe_print("[*] Promotion check: no saved successful account to probe.")
        logger.info("promotion check: no saved successful account to probe")
        return report

    safe_print(f"[*] Promotion check: probing {len(targets)} saved successful account(s)...")
    logger.info("promotion check: probing %d saved successful account(s)", len(targets))
    try:
        report = refresh_promotion_statuses(
            emails=targets,
            workers=max(1, int(workers or 1)),
            proxy=proxy,
            proxy_pool=proxy_pool,
            timeout=max(5, int(timeout or 20)),
            payment_eligibility=bool(payment_eligibility),
        )
    except Exception as exc:
        report = {
            "ok": False,
            "total": len(targets),
            "success": 0,
            "failed": len(targets),
            "trial_eligible": 0,
            "results": [],
            "error": str(sanitize(exc)),
        }
        safe_print(f"[!] Promotion check failed: {report['error']}")
        logger.warning("promotion check failed: %s", report["error"])
        return report

    results = report.get("results") if isinstance(report.get("results"), list) else []
    # refresh_promotion_statuses owns this count now. It used to be recounted
    # here, which left the other CLI entry point with no such key at all.
    trial_eligible = int(report.get("trial_eligible") or 0)
    report["trial_eligible"] = trial_eligible
    eligibility_ok = int(report.get("payment_eligibility_ok") or 0)
    eligibility_total = eligibility_ok + int(report.get("payment_eligibility_failed") or 0)
    safe_print(
        "[*] Promotion check: "
        f"success={int(report.get('success') or 0)}/{int(report.get('total') or 0)} "
        f"trial_eligible={trial_eligible} "
        f"payment_eligibility={eligibility_ok}/{eligibility_total}"
    )
    logger.info(
        "promotion check finished: success=%s/%s trial_eligible=%s payment_eligibility=%s/%s",
        int(report.get("success") or 0), int(report.get("total") or 0), trial_eligible,
        eligibility_ok, eligibility_total,
    )
    for item in results:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        label = str(item.get("promotion_status") or "检测失败").strip()
        eligibility = str(item.get("payment_eligibility") or "").strip()
        if eligibility:
            label = f"{label} · {eligibility}" if label else eligibility
        safe_print(f"    {mask_account(email)}: {label}")
    return report


_PERSISTENCE_KEY = "_registration_persistence"


def registration_pipeline_timing(pipeline_started, mailbox_seconds, register_started):
    now = time.time()
    return {
        "mailbox_load_seconds": round(float(mailbox_seconds or 0), 2),
        "registration_batch_seconds": round(max(0.0, now - register_started), 2),
        "total_seconds": round(max(0.0, now - pipeline_started), 2),
    }


def persist_registration_result(
    args,
    data,
    base_dir,
    ctx: RegistrationCommandContext,
    *,
    pipeline_timing=None,
):
    """Persist one completed registration result behind a cross-process durable
    idempotency boundary.

    The in-memory ``_PERSISTENCE_KEY`` marker (in ``_persist_registration_result_core``)
    keeps retries idempotent *within* a process. This wrapper adds the durable layer:
    a ``PaymentOperationStore`` record keyed by ``email|batch`` acquires a cross-process
    lock and replays the same guard used for payments, so two processes (or a restart
    mid-batch) cannot double-write the session file, upsert the row, or re-enqueue the
    health check. A successful persist is finalized (replays blocked). A
    pre-side-effect failure is finalized as retryable, so a later finalization
    pass can safely replay it without leaving a permanent ``running`` journal.
    """
    identity = (data.get("email") or data.get("phone") or "unknown") if isinstance(data, dict) else "unknown"
    batch_id = str(getattr(args, "registration_batch_id", "") or "")
    # Cheap in-process guard: this exact result object was already persisted in this
    # process (save_registration_results re-emits the same data objects). The durable
    # journal below is only for cross-process / restart dedup, so skip it here.
    marker = data.get(_PERSISTENCE_KEY) if isinstance(data, dict) else None
    if isinstance(marker, dict) and marker.get("status") == "complete":
        return marker

    store = PaymentOperationStore.from_config(ctx.runtime_config)
    try:
        op = store.begin(
            payment_method="registration",
            operation="persist_result",
            idempotency_key=f"{identity}|{batch_id}",
        )
    except PaymentOperationConflict as conflict:
        previous = conflict.record
        return {
            "status": "complete",
            "session_saved": int(previous.get("session_saved") or 0),
            "db_saved": int(previous.get("db_saved") or 0),
            "import_email": str(data.get("email") or "") if isinstance(data, dict) else "",
            "durable_conflict": True,
        }

    try:
        result = _persist_registration_result_core(args, data, base_dir, ctx, pipeline_timing=pipeline_timing)
    except Exception as exc:
        # A persistence failure (e.g. a temporary DB error) must surface as a
        # retryable terminal journal state, not propagate or leave ``running``
        # forever. The retry can re-acquire the lock from the failed record.
        op.finish({
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "error_code": f"persist_result_{type(exc).__name__.lower()}",
            "error_stage": "persist_result",
            "retryable": True,
            "side_effect_started": False,
        })
        return {
            "status": "failed",
            "session_saved": 0,
            "db_saved": 0,
            "import_email": str(data.get("email") or "") if isinstance(data, dict) else "",
            "error": str(exc)[:500],
        }

    # Finalize only when the core durably persisted the account row (db_saved == 1).
    # A logical failure (success=False) leaves db_saved 0 and must not be finalized, and
    # an upsert error leaves db_saved 0 even if the session file was written; in both cases
    # leave the durable record running so an in-process / cross-process retry can re-acquire
    # the lock and attempt the side effects again.
    committed = isinstance(result, dict) and bool(result.get("db_saved"))
    if committed:
        # Carry the real side-effect counts onto the journal record so a later
        # durable-conflict replay (e.g. save_registration_results re-persisting the
        # same account, or a cross-process resume) reports accurate totals instead of 0.
        op.record["session_saved"] = int(result.get("session_saved") or 0)
        op.record["db_saved"] = int(result.get("db_saved") or 0)
        op.finish({"ok": True, "status": "completed", "side_effect_started": True})
    else:
        # Logical registration failures are terminal for this persistence
        # attempt, but remain explicitly retryable because no side effect was
        # committed and a later finalization pass may safely replay them.
        op.finish({
            "ok": False,
            "status": "failed",
            "error": str(result.get("error") or "registration_result_not_persisted"),
            "error_code": "registration_result_not_persisted",
            "error_stage": "persist_result",
            "retryable": True,
            "side_effect_started": False,
        })
    return result


def _persist_registration_result_core(
    args,
    data,
    base_dir,
    ctx: RegistrationCommandContext,
    *,
    pipeline_timing=None,
):
    """Persist one completed registration result and make retries idempotent."""
    from ..storage import record_registration_audit

    if not isinstance(data, dict):
        return {
            "status": "complete",
            "session_saved": 0,
            "db_saved": 0,
            "import_email": "",
        }

    marker = data.get(_PERSISTENCE_KEY)
    marker = marker if isinstance(marker, dict) else {}
    if marker.get("status") == "complete":
        return marker

    batch_id = str(getattr(args, "registration_batch_id", "") or "")
    data["batch_id"] = batch_id
    if isinstance(pipeline_timing, dict):
        data["pipeline_timing"] = dict(pipeline_timing)

    marker.setdefault("session_saved", 0)
    marker.setdefault("db_saved", 0)
    marker.setdefault("import_email", "")
    data[_PERSISTENCE_KEY] = marker

    try:
        deferred_probe = (
            str(data.get("registration_state") or "").strip().lower()
            in {"at_probe_pending", "at_probe_transport_unknown"}
            and bool(str(data.get("access_token") or "").strip())
        )
        # 🔴 2026-09-15: ``partial_registered`` must be persisted too.
        #
        # The server has stated the address already exists; this run simply did
        # not get a session for it.  Skipping the upsert left ``accounts`` with
        # zero ``partial_registered`` rows while ``registration_audit`` held 330,
        # and the UI's "半注册" guards (``MainWindow.Register.cs`` /
        # ``MainWindow.Pools.cs``) read the *accounts* table -- so the guard
        # written to stop this address from being re-registered never fired, and
        # every batch re-selected it as an "unregistered mailbox" and spent
        # another email code.
        partial_registered = (
            str(data.get("registration_state") or "").strip().lower() == "partial_registered"
        )
        if not data.get("success", False) and not deferred_probe and not partial_registered:
            failed_email = data.get("email") or data.get("phone") or "unknown"
            failed_error = str(data.get("error") or "registration_failed")
            if not marker.get("failure_reported"):
                safe_print(
                    f"[!] Registration failed for {mask_account(failed_email)}: "
                    f"{failed_error[:500]}"
                )
                marker["failure_reported"] = True
            if not marker.get("failure_audited"):
                record_registration_audit(
                    data,
                    batch_id=batch_id,
                    state="terminal" if "account_deactivated" in failed_error.lower() else "failed",
                    runtime_config=ctx.runtime_config,
                )
                marker["failure_audited"] = True
            if "account_deactivated" in failed_error.lower() and not marker.get("dead_remail_recorded"):
                try:
                    from ..providers.mailbox_remail import record_dead_remail_account

                    record_dead_remail_account(data, reason="account_deactivated")
                except Exception:
                    pass
                marker["dead_remail_recorded"] = True
            if (
                failed_error == "phone_already_registered_or_login_redirect"
                and not marker.get("skip_reported")
            ):
                safe_print("    Skipped: phone number already registered, not saving to database")
                marker["skip_reported"] = True
            marker["status"] = "complete"
            marker.pop("error_type", None)
            return marker

        # Keep transport-unknown AT probes resumable. The account already has
        # an auth session, so persisting it is safer than replaying signup.
        data["registration_state"] = (
            "at_probe_pending" if deferred_probe
            else ("partial_registered" if partial_registered else "pending")
        )
        if not marker.get("pending_audited"):
            record_registration_audit(
                data,
                batch_id=batch_id,
                state="partial_registered" if partial_registered else "pending",
                runtime_config=ctx.runtime_config,
            )
            marker["pending_audited"] = True

        session_data = ctx.build_session_file(data)
        if not session_data.get("access_token"):
            if partial_registered:
                # No session exists to save, but the account row must still land:
                # that row is exactly what the UI's "半注册" guard reads.
                # Persist it with an empty ``json_path`` (the parameter default)
                # instead of returning early, and set ``db_saved`` so the caller
                # finalizes this persistence attempt as completed.
                session_data["registration_state"] = "partial_registered"
                # ``build_session_file`` does not emit ``batch_id``, and the
                # normal path sets it *after* this early return -- so without
                # this line every partial row lands unattributed.  Measured
                # 2026-09-15: all 71 ``partial_registered`` rows carried an
                # empty ``batch_id`` and could not be grouped with the run
                # that produced them.
                session_data["batch_id"] = batch_id
                marker["db_saved"] = 1 if ctx.upsert_account(session_data, json_path="") else 0
                marker["db_completed"] = True
                marker["import_email"] = str(session_data.get("email") or "")
                marker["status"] = "complete"
                marker.pop("error_type", None)
                if not marker.get("partial_reported"):
                    safe_print(
                        "[*] Partial registration recorded (address already exists): "
                        f"{mask_account(session_data.get('email') or '')}"
                    )
                    marker["partial_reported"] = True
                return marker
            if not marker.get("missing_token_reported"):
                safe_print("[!] Successful registration has no access_token; session file was not saved")
                marker["missing_token_reported"] = True
            marker["status"] = "complete"
            marker.pop("error_type", None)
            return marker

        session_data["batch_id"] = batch_id
        session_data["registration_state"] = (
            "at_probe_pending" if deferred_probe
            else ("partial_registered" if partial_registered else "active")
        )
        out_pattern = ctx.runtime_config.get("output", {}).get(
            "filename_pattern", "session_{email}_{timestamp}.json"
        )
        os.makedirs(base_dir, exist_ok=True)
        if not marker.get("session_path"):
            identifier = (session_data.get("email") or session_data.get("phone") or "unknown").replace("+", "")
            safe_identifier = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", identifier)
            fname = out_pattern.format(
                email=safe_identifier,
                phone=safe_identifier,
                timestamp=int(time.time()),
            )
            marker["session_path"] = os.path.join(base_dir, fname)
        out_path = marker["session_path"]

        if not marker.get("session_written"):
            temp_path = f"{out_path}.{os.getpid()}.tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as file_handle:
                    json.dump(session_data, file_handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, out_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            marker["session_written"] = True
            marker["session_saved"] = 1

        if not marker.get("db_completed"):
            marker["db_saved"] = 1 if ctx.upsert_account(session_data, json_path=out_path) else 0
            marker["db_completed"] = True
        if marker.get("db_saved") and not deferred_probe and not marker.get("account_health_enqueued"):
            try:
                from ..accounts.account_health_queue import enqueue_post_registration_checks

                health_jobs = enqueue_post_registration_checks(
                    session_data,
                    source="registration",
                    config=ctx.runtime_config,
                    # An explicit post-registration promotion check runs once
                    # for the whole batch below. Do not enqueue the same plan
                    # probe per account a second time.
                    include_plan=not bool(
                        getattr(args, "check_promotion_after_registration", False)
                    ),
                )
                marker["account_health_jobs"] = [
                    str(item.get("id") or "") for item in health_jobs if item.get("id")
                ]
                marker["account_health_enqueued"] = True
            except Exception as exc:
                safe_print(
                    "[!] Post-registration health queue warning: "
                    f"{type(exc).__name__}"
                )
        if not marker.get("active_audited"):
            record_registration_audit(
                data,
                batch_id=batch_id,
                state="at_probe_pending" if deferred_probe else "active",
                runtime_config=ctx.runtime_config,
            )
            marker["active_audited"] = True
        marker["import_email"] = str(session_data.get("email") or "")
        if not marker.get("saved_reported"):
            safe_print(f"[*] {SAVED_SESSION_MARKER} {out_path}")
            marker["saved_reported"] = True
        marker["status"] = "complete"
        marker.pop("error_type", None)
        return marker
    except Exception as exc:
        marker["status"] = "failed"
        marker["error_type"] = type(exc).__name__
        safe_print(
            "[!] Immediate registration persistence failed "
            f"({marker['error_type']}); it will be retried during finalization."
        )
        return marker


def save_registration_results(
    args,
    results,
    effective_count,
    base_dir,
    pipeline_started,
    mailbox_seconds,
    register_seconds,
    ctx: RegistrationCommandContext,
):
    batch_id = str(getattr(args, "registration_batch_id", "") or "")
    pipeline_seconds = time.time() - pipeline_started
    pipeline_timing = {
        "mailbox_load_seconds": round(mailbox_seconds, 2),
        "registration_batch_seconds": round(register_seconds, 2),
        "total_seconds": round(pipeline_seconds, 2),
    }
    for data in filter(None, results):
        data["pipeline_timing"] = pipeline_timing

    saved_count = 0
    db_saved_count = 0
    import_emails = []
    health_job_ids: list[str] = []
    for data in filter(None, results):
        outcome = persist_registration_result(
            args,
            data,
            base_dir,
            ctx,
            pipeline_timing=pipeline_timing,
        )
        saved_count += int(outcome.get("session_saved") or 0)
        db_saved_count += int(outcome.get("db_saved") or 0)
        if outcome.get("import_email"):
            import_emails.append(outcome["import_email"])
        health_job_ids.extend(
            str(item)
            for item in (outcome.get("account_health_jobs") or [])
            if str(item or "")
        )

    success_count = sum(1 for r in results if r and r.get("success"))
    safe_print(f"[*] SQLite index: {ctx.database_path()} ({db_saved_count} record(s) upserted)")
    safe_print(f"\n[*] Done. {success_count}/{effective_count} registered successfully, {saved_count} session file(s) saved.")
    quality = None
    if getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None):
        from ..providers.mailbox_remail import record_remail_batch_quality
        quality = record_remail_batch_quality(batch_id, results, requested=effective_count)
        safe_print(
            f"[*] ReMail quality: deactivated={quality['account_deactivated']}/"
            f"{quality['requested']} halt={quality['halt_replenishment']}"
        )

    promotion_report = None
    if getattr(args, "check_promotion_after_registration", False) and import_emails:
        promotion_report = ctx.check_registered_promotions(
            import_emails,
            workers=max(1, int(getattr(args, "workers", 4) or 4)),
            # Post-registration promotion checks use the account-health lane.
            # Passing the signup proxy here defeats that isolation and can
            # immediately re-use a contaminated registration exit.
            proxy=None,
            timeout=max(5, int(getattr(args, "refresh_timeout", 20) or 20)),
            payment_eligibility=bool(getattr(args, "payment_eligibility", True)),
        )

    if getattr(args, "import_cpa", False):
        ctx.import_registered_accounts(args, import_emails)
    return {
        "ok": success_count == effective_count,
        "batch_id": batch_id,
        "total": int(effective_count),
        "success": success_count,
        "failed": max(0, int(effective_count) - success_count),
        "session_saved": saved_count,
        "db_saved": db_saved_count,
        "quality": quality,
        "promotion": promotion_report,
        "health": {
            "queued": len(dict.fromkeys(health_job_ids)),
            "promotion_completed": promotion_report is not None,
            "promotion_ok": (
                bool(promotion_report.get("ok"))
                if isinstance(promotion_report, dict)
                else None
            ),
        },
    }


def run_target_at200(args, base_dir, ctx: RegistrationCommandContext):
    """Bounded ReMail replenishment mode for a stable AT-200 target."""
    if not (getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None)):
        safe_print("[Error] --target-at200 requires --buy-remail-mailbox or --remail-service-mode")
        raise SystemExit(2)
    target = max(1, int(args.target_at200 or 1))
    max_purchases = max(target, int(args.max_mailbox_purchases or target * 2))
    max_cost = max(0.0, float(args.max_remail_cost or 0.0))
    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"target_at200_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    original_count = args.count
    purchased = 0
    active = 0
    spent = 0.0
    rounds = []
    halted = False
    promotion_total = 0
    promotion_success = 0
    trial_eligible = 0
    started = time.time()
    phone_pool = ctx.registration_phone_pool(args)
    try:
        while active < target and purchased < max_purchases and not halted:
            quantity = min(target - active, max_purchases - purchased)
            args.count = quantity
            loaded_mailboxes = ctx.load_mailbox_pool(args)
            # Filter before ``purchased`` is incremented.  Skipped mailboxes must
            # not count towards ``max_purchases`` (nor towards ``spent``), and a
            # pool that is entirely registered has to stop the loop -- otherwise
            # every round re-loads the same list and the batch spins without
            # attempting anything.
            mailboxes = filter_registered_mailboxes(loaded_mailboxes)
            if not mailboxes:
                if loaded_mailboxes:
                    safe_print(
                        "[!] Every mailbox in the pool already has a registered account; stopping. "
                        "Add fresh mailboxes, or set registration.skip_registered_mailboxes=false "
                        "to attempt them anyway."
                    )
                break
            purchased += len(mailboxes)
            for mailbox in mailboxes:
                try:
                    spent += float(getattr(mailbox, "price", 0) or 0)
                except (TypeError, ValueError):
                    pass
            if max_cost and spent > max_cost:
                halted = True
                break
            round_started = time.time()
            def persist_completed_result(_index, result):
                persist_registration_result(
                    args,
                    result,
                    base_dir,
                    ctx,
                    pipeline_timing=registration_pipeline_timing(
                        round_started,
                        0,
                        round_started,
                    ),
                )

            results = ctx.run_batch(
                count=len(mailboxes),
                proxy=args.proxy,
                proxy_pool=ctx.proxy_pool_values(args),
                mailboxes=mailboxes,
                workers=args.workers,
                phone_pool=phone_pool,
                codex_oauth=False,
                registration_mode=args.registration_mode,
                registration_driver=getattr(args, "registration_driver", None),
                browser_headless=getattr(args, "browser_headless", None),
                enroll_2fa=not getattr(args, "no_2fa", False),
                run_email_func=ctx.run_email,
                on_result=persist_completed_result,
            )
            saved = ctx.save_results(
                args,
                results,
                effective_count=len(mailboxes),
                base_dir=base_dir,
                pipeline_started=round_started,
                mailbox_seconds=0,
                register_seconds=time.time() - round_started,
            ) or {}
            gained = int(saved.get("success") or 0)
            active += gained
            quality = saved.get("quality") if isinstance(saved.get("quality"), dict) else {}
            promotion = saved.get("promotion") if isinstance(saved.get("promotion"), dict) else {}
            promotion_total += int(promotion.get("total") or 0)
            promotion_success += int(promotion.get("success") or 0)
            trial_eligible += int(promotion.get("trial_eligible") or 0)
            halted = bool(quality.get("halt_replenishment"))
            rounds.append({
                "requested": quantity,
                "mailboxes": len(mailboxes),
                "active": gained,
                "deactivated": int(quality.get("account_deactivated") or 0),
                "promotion_total": int(promotion.get("total") or 0),
                "promotion_success": int(promotion.get("success") or 0),
                "trial_eligible": int(promotion.get("trial_eligible") or 0),
                "halted": halted,
            })
    finally:
        args.count = original_count
    report = {
        "ok": active >= target,
        "batch_id": args.registration_batch_id,
        "total": purchased,
        "success": active,
        "failed": max(0, purchased - active),
        "target_at200": target,
        "active": active,
        "purchased": purchased,
        "max_purchases": max_purchases,
        "estimated_cost": round(spent, 4),
        "max_cost": max_cost,
        "supplier_halted": halted,
        "promotion_total": promotion_total,
        "promotion_success": promotion_success,
        "trial_eligible": trial_eligible,
        "elapsed_seconds": round(time.time() - started, 2),
        "rounds": rounds,
    }
    report_path = ctx.runtime_file(f"registration_target_{args.registration_batch_id}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    from ..desktop_ipc import emit_result

    emit_result(report, enabled=bool(getattr(args, "desktop_ipc", False)))
    if not report["ok"]:
        raise SystemExit(3)
    return report
