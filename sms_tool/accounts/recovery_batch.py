"""测活批量引擎（refresh_local_quota_statuses 及其闭包）——候选1 god file 拆解。

从 ``account_recovery.py``（1530 行）拆出的批处理引擎：线程池、heavy-lane
信号量、每账号/整批截止线、快照持久化与清理、IPC 批事件。恢复策略
（``relogin_codex_account`` 等）仍归 ``account_recovery``。

🔴 引擎对策略层的引用**必须留在函数体内**。这**不是**为了断环：
``account_recovery`` 自 2026-09-22 起已不再反指本模块（PEP 562 兼容再导出
``__getattr__`` 已删，SCC 消失），所以模块级导入在运行期是安全的。
留函数内的理由是**保住 patch 面**：``tests/accounts/test_account_recovery.py:1308``
等用例 patch 的是 ``sms_tool.accounts.account_recovery.CFG``，只有调用期才去
解析 ``account_recovery`` 的模块属性，才读得到被 patch 的值。提升到模块级会让
这些 patch **静默失效**（转而使用真实配置，不报错、不失败）。
同理 ``_clear_promotion_marker_after_probe`` 里的 ``except TypeError`` 分支是
为兼容只接受单参的旧注入替身，不是死代码。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeGuard

from .account_inspection import DEFAULT_INSPECT_SKEW_SECONDS, inspect_accounts_with_runner
from ..registration.retry_guard import RegistrationRetryGuard

logger = logging.getLogger(__name__)


# Probe verdicts from a scan pass that a quota refresh must not re-probe.
# Anything else (unknown / timeout / empty / 检测失败) is transport-shaped and
# gets a fresh probe because the network may have recovered since the scan.
_FRESH_PROBE_DEFINITIVE_STATUSES = {"active", "token_invalid", "account_deactivated"}

# runtime/account_liveness_batches grew without bound (one snapshot per run,
# several per operator day). Keep the newest files -- the WPF panel reads the
# newest snapshot, older ones are crash-history only.
_LIVENESS_SNAPSHOT_KEEP = 20


def _prune_liveness_snapshots(directory, keep: int = _LIVENESS_SNAPSHOT_KEEP, exclude: str = "") -> int:
    """Delete oldest liveness batch snapshots beyond ``keep``. Returns removed count."""
    try:
        files = [p for p in Path(directory).glob("*.json") if p.name != exclude]
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _probe_is_fresh_definitive(probe: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    if not isinstance(probe, dict) or not probe:
        return False
    return str(probe.get("status") or "").strip().lower() in _FRESH_PROBE_DEFINITIVE_STATUSES


def _relogin_proxy_for_account(
    account: dict[str, Any], *, proxy: str | None, relogin_pool: list[str], index: int
) -> str | None:
    if proxy or len(relogin_pool) <= 1 or (account.get("identity_context") or {}).get("proxy_affinity"):
        return proxy
    return relogin_pool[index % len(relogin_pool)]


def _heavy_lane_slots(max_workers: int) -> int:
    """Capacity of a heavy lane (browser fallback / 401 relogin).

    不变量：**只要 ``max_workers >= 2``，heavy lane 必须严格小于 worker 池**。
    heavy lane 的作用就是给便宜的 HTTP probe 留出余量；一旦它等于池大小，
    慢恢复就能占满所有 worker，semaphore 形同虚设。

    旧公式 ``max(1, min(max_workers, max(2, max_workers // 2)))`` 里的
    ``max(2, ...)`` 下限会在小并发下**反向压过** ``max_workers``：
    ``max_workers=2``（配置默认，见 ``account_health_queue.py:121``）算出来是 2，
    与池相等 —— 默认配置下隔离从来没生效过。

    ``max_workers=1`` 时无法隔离（一个 worker 不可能同时干重活和轻活），返回 1。
    外层的 ``max(1, ...)`` 已经兜住了这个下界，下面的显式分支是**冗余的**
    （等价变异验证时删掉它测试仍全绿）。保留是因为它把"1 个 worker 时不做隔离"
    这个决策写成了代码，也防未来有人改外层下界时静默退化成 0。
    """
    if max_workers <= 1:
        return 1
    return max(1, min(max_workers - 1, max_workers // 2))


def refresh_local_quota_statuses(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 30,
    relogin_on_401: bool = False,
    relogin_timeout: int = 300,
    relogin_mode: str = "auto",
    batch_timeout: int = 900,
    account_timeout: int = 360,
    fresh_probes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # 策略层与既有助手在调用期经 account_recovery 解析：测试对
    # account_recovery.* 的 patch 才能生效（切分不改变 patch 面）。
    # 🔴 不要提升到模块级 —— 理由见本模块 docstring。
    from .account_recovery import (
        CFG,
        _TRANSIENT_RELOGIN_MODES,
        _clear_relogin_failure,
        _has_relogin_material,
        _is_token_revoked_drop,
        _health_status_code,
        _item_is_account_deactivated,
        _local_quota_accounts,
        _looks_account_deactivated,
        _persist_token_revoked_drop,
        _probe_is_token_invalid,
        _record_relogin_failure,
        _relogin_cooldown_active,
        _relogin_failure_quota_status,
        _timed_out_health_result,
        account_identity,
        browser_fetch_for_account,
        is_permanently_deactivated,
        mailbox_relogin_allowed,
        mark_quota_status,
        probe_account_liveness,
        proxy_pool_for,
        relogin_codex_account,
        runtime_file,
    )

    if relogin_on_401:
        _refresh_mailbox_quarantine_state()
    accounts = _local_quota_accounts(emails)
    relogin_pool = proxy_pool_for(CFG, "relogin") if (relogin_on_401 and not proxy) else []
    run_id = uuid.uuid4().hex
    _emit_account_batch_event(run_id, "batch_started", "running", total=len(accounts), detail="账号测活开始")
    # Liveness probe is a light GET; heavy 401 relogins and browser fallbacks use narrower semaphores.
    max_workers = max(1, min(int(workers or 1), 16, len(accounts) or 1))
    heavy_lane_slots = _heavy_lane_slots(max_workers)
    browser_slots = threading.BoundedSemaphore(heavy_lane_slots)
    relogin_slots = threading.BoundedSemaphore(heavy_lane_slots)
    batch_deadline = time.monotonic() + max(30, int(batch_timeout or 900))
    # OTP recovery gets its own budget bounded by batch_deadline rather than the single HTTP probe budget.
    relogin_budget = max(30, int(relogin_timeout or timeout or 300))
    ordered: list[dict[str, Any] | None] = [None] * len(accounts)
    snapshot_path = runtime_file(CFG, "account_liveness_batches") / f"{run_id}.json"

    def persist_snapshot(*, terminal: bool = False) -> None:
        _write_liveness_snapshot(snapshot_path, run_id, len(accounts), ordered, terminal=terminal)

    def run(index: int, account: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        email = str(account.get("email") or "").strip()
        account_deadline = min(batch_deadline, time.monotonic() + max(30, int(account_timeout or 360)))
        try:
            if time.monotonic() >= account_deadline:
                return index, _timed_out_health_result(email, "account_timeout_before_probe")
            probe_timeout = max(5, min(int(timeout or 30), int(account_deadline - time.monotonic())))
            probe = _probe_account_with_browser_fallback(
                account,
                email=email,
                proxy=proxy,
                probe_timeout=probe_timeout,
                account_deadline=account_deadline,
                fresh_probes=fresh_probes,
                browser_slots=browser_slots,
                is_permanently_deactivated=is_permanently_deactivated,
                is_token_revoked_drop=_is_token_revoked_drop,
                account_identity=account_identity,
                browser_fetch_for_account=browser_fetch_for_account,
                probe_account_liveness=probe_account_liveness,
                pool_offset=index,
            )
            initial_status = str(probe.get("quota_status") or probe.get("status") or "unknown")
            liveness_401 = _probe_is_token_invalid(probe)
            relogin_eligible = bool(relogin_on_401 and liveness_401 and email and not probe.get("terminal"))
            if email and relogin_on_401 and liveness_401:
                mark_quota_status(email, initial_status, quota_result=probe)
            relogin, probe, relogin_attempted, mailbox_auth_invalid, effective_deadline = _run_optional_401_relogin(
                account,
                email=email,
                probe=probe,
                relogin_eligible=relogin_eligible,
                proxy=_relogin_proxy_for_account(account, proxy=proxy, relogin_pool=relogin_pool, index=index),
                relogin_mode=relogin_mode,
                account_deadline=account_deadline,
                batch_deadline=batch_deadline,
                relogin_budget=relogin_budget,
                relogin_slots=relogin_slots,
                cfg=CFG,
                mailbox_relogin_allowed=mailbox_relogin_allowed,
                relogin_cooldown_active=_relogin_cooldown_active,
                relogin_codex_account=relogin_codex_account,
                clear_relogin_failure=_clear_relogin_failure,
                record_relogin_failure=_record_relogin_failure,
            )
            probe = _apply_post_probe_drop_and_timeout(
                account,
                probe=probe,
                relogin=relogin,
                liveness_401=liveness_401,
                effective_deadline=effective_deadline,
                transient_relogin_modes=_TRANSIENT_RELOGIN_MODES,
                looks_account_deactivated=_looks_account_deactivated,
                has_relogin_material=_has_relogin_material,
                persist_token_revoked_drop=_persist_token_revoked_drop,
                probe_is_token_invalid=_probe_is_token_invalid,
            )
            result = _finalize_account_probe_result(
                email=email,
                probe=probe,
                relogin=relogin,
                liveness_401=liveness_401,
                relogin_attempted=relogin_attempted,
                mailbox_auth_invalid=mailbox_auth_invalid,
                mark_quota_status=mark_quota_status,
                relogin_failure_quota_status=_relogin_failure_quota_status,
                health_status_code=_health_status_code,
            )
        except Exception as exc:
            result = _build_probe_exception_result(email, exc)
        _emit_account_batch_event(
            run_id,
            "account_completed",
            "completed" if result.get("probe_ok") else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(result.get("quota_status") or "检测完成"),
        )
        return index, result

    _execute_liveness_pool(
        accounts=accounts,
        ordered=ordered,
        max_workers=max_workers,
        batch_timeout=batch_timeout,
        run_fn=run,
        persist_snapshot_fn=persist_snapshot,
        timed_out_health_result_fn=_timed_out_health_result,
    )
    results = [item for item in ordered if item is not None]
    persist_snapshot(terminal=True)
    _prune_liveness_snapshots(snapshot_path.parent, exclude=snapshot_path.name)
    return _build_liveness_batch_summary(
        results=results,
        accounts=accounts,
        run_id=run_id,
        snapshot_path=snapshot_path,
        item_is_account_deactivated=_item_is_account_deactivated,
        probe_is_token_invalid=_probe_is_token_invalid,
        looks_account_deactivated=_looks_account_deactivated,
    )


def _write_liveness_snapshot(
    snapshot_path: Path,
    run_id: str,
    total_accounts: int,
    ordered: list[dict[str, Any] | None],
    *,
    terminal: bool = False,
) -> None:
    rows = [item for item in ordered if item is not None]
    timed_out_rows = [item for item in rows if item.get("timed_out")]
    payload = {
        "run_id": run_id,
        "total": total_accounts,
        "completed": max(0, len(rows) - len(timed_out_rows)),
        "unfinished": len(timed_out_rows) + max(0, total_accounts - len(rows)),
        "partial": bool(timed_out_rows or len(rows) < total_accounts),
        "terminal": bool(terminal),
        "updated_at": int(time.time()),
        "results": rows,
    }
    try:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp.replace(snapshot_path)
    except OSError as exc:
        logger.warning("liveness snapshot persist failed run_id=%s: %s", run_id, exc)


def _probe_account_with_browser_fallback(
    account: dict[str, Any],
    *,
    email: str,
    proxy: str | None,
    probe_timeout: int,
    account_deadline: float,
    fresh_probes: dict[str, dict[str, Any]] | None,
    browser_slots: threading.BoundedSemaphore,
    is_permanently_deactivated: Any,
    is_token_revoked_drop: Any,
    account_identity: Any,
    browser_fetch_for_account: Any,
    probe_account_liveness: Any,
    pool_offset: int = 0,
) -> dict[str, Any]:
    if is_permanently_deactivated(account):
        return {
            "ok": False,
            "mode": "local",
            "status": "account_deactivated",
            "quota_status": "account_deactivated",
            "error": "account_deactivated",
            "terminal": True,
        }
    if is_token_revoked_drop(account):
        return {
            "ok": False,
            "mode": "local",
            "status": "token_invalid",
            "quota_status": "401失效",
            "error": "token_revoked_unrecoverable",
            "terminal": True,
        }
    fresh = (fresh_probes or {}).get(str(email).strip().lower())
    if _probe_is_fresh_definitive(fresh):
        probe = {**fresh, "probe_source": "scan_reuse"}
    else:
        probe = _probe_liveness_with_retries(
            account, proxy=proxy, timeout=probe_timeout, browser_fetch=None, pool_offset=pool_offset
        )
    if not isinstance(probe, dict):
        probe = {
            "ok": False,
            "status": "unknown",
            "quota_status": "检测失败",
            "error": "invalid_probe_result",
        }
    browser_identity = account_identity(account).get("browser_identity") or {}
    if browser_identity and _needs_browser_fallback(probe):
        remaining = max(0.0, account_deadline - time.monotonic())
        if remaining > 0.0 and browser_slots.acquire(timeout=remaining):
            try:
                with browser_fetch_for_account(account, proxy=proxy, timeout=probe_timeout) as browser_fetch:
                    if browser_fetch is not None:
                        browser_probe = probe_account_liveness(
                            account,
                            proxy=proxy,
                            timeout=probe_timeout,
                            browser_fetch=browser_fetch,
                        )
                        if browser_probe.get("ok") or int(browser_probe.get("status_code") or 0) != 0:
                            probe = browser_probe
                    else:
                        probe = {**probe, "browser_fallback": "unavailable"}
            finally:
                browser_slots.release()
        else:
            probe = {**probe, "browser_fallback": "concurrency_limited"}
    return probe


def _run_optional_401_relogin(
    account: dict[str, Any],
    *,
    email: str,
    probe: dict[str, Any],
    relogin_eligible: bool,
    proxy: str | None,
    relogin_mode: str,
    account_deadline: float,
    batch_deadline: float,
    relogin_budget: int,
    relogin_slots: threading.BoundedSemaphore,
    cfg: Any,
    mailbox_relogin_allowed: Any,
    relogin_cooldown_active: Any,
    relogin_codex_account: Any,
    clear_relogin_failure: Any,
    record_relogin_failure: Any,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool, float]:
    effective_deadline = account_deadline
    relogin_attempted = False
    mailbox_auth_invalid = False
    relogin: dict[str, Any] = {}
    if relogin_eligible and not mailbox_relogin_allowed(email):
        relogin = {"ok": False, "mode": "disabled", "error": "mailbox_pool_repair_required"}
    elif relogin_eligible and relogin_cooldown_active(account):
        relogin = {"ok": False, "mode": "cooldown", "error": "relogin_cooldown"}
    elif relogin_eligible and time.monotonic() < account_deadline:
        queue_remaining = max(0.0, batch_deadline - time.monotonic())
        if queue_remaining > 0.0 and relogin_slots.acquire(timeout=queue_remaining):
            try:
                relogin_attempted = True
                relogin_deadline = min(batch_deadline, time.monotonic() + relogin_budget)
                effective_deadline = relogin_deadline
                relogin = relogin_codex_account(
                    account,
                    proxy=proxy,
                    timeout=max(30, int(relogin_deadline - time.monotonic())),
                    mode=relogin_mode,
                )
                if relogin.get("ok"):
                    probe = dict(relogin.get("probe") or {})
                    if email:
                        try:
                            _clear_promotion_marker_after_probe(email)
                        except Exception:
                            pass
                        try:
                            clear_relogin_failure(email)
                            if probe.get("ok"):
                                RegistrationRetryGuard(cfg).record(email, success=True)
                        except Exception:
                            pass
                elif relogin:
                    record_relogin_failure(email, relogin)
                mailbox_auth_invalid = str(relogin.get("error") or "") == "mailbox_auth_invalid"
            finally:
                relogin_slots.release()
        else:
            relogin = {"ok": False, "mode": "concurrency_limited", "error": "relogin_concurrency_limited"}
    return relogin, probe, relogin_attempted, mailbox_auth_invalid, effective_deadline


def _apply_post_probe_drop_and_timeout(
    account: dict[str, Any],
    *,
    probe: dict[str, Any],
    relogin: dict[str, Any],
    liveness_401: bool,
    effective_deadline: float,
    transient_relogin_modes: Any,
    looks_account_deactivated: Any,
    has_relogin_material: Any,
    persist_token_revoked_drop: Any,
    probe_is_token_invalid: Any,
) -> dict[str, Any]:
    if (
        liveness_401
        and not probe.get("ok")
        and not relogin.get("ok")
        and not relogin.get("terminal")
        and not looks_account_deactivated(relogin)
        and str(relogin.get("mode") or "") not in transient_relogin_modes
        and not has_relogin_material(account)
    ):
        try:
            if persist_token_revoked_drop(account):
                probe = {**probe, "dropped": "token_revoked"}
        except Exception:
            pass
    if (
        time.monotonic() >= effective_deadline
        and not probe.get("ok")
        and not probe_is_token_invalid(probe)
        and not probe.get("terminal")
        and str(probe.get("status") or "") != "account_deactivated"
    ):
        probe = {**probe, "status": "timeout", "error": probe.get("error") or "account_timeout"}
    return probe


def _build_probe_exception_result(email: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "email": email,
        "quota_status": "检测失败",
        "probe": {"ok": False, "error": str(exc)[:200]},
        "probe_ok": False,
        "persisted": False,
        "health_status": "probe_failed",
        "liveness_401": False,
        "relogin_attempted": False,
        "mailbox_auth_invalid": False,
    }


def _execute_liveness_pool(
    *,
    accounts: list[dict[str, Any]],
    ordered: list[dict[str, Any] | None],
    max_workers: int,
    batch_timeout: int,
    run_fn: Any,
    persist_snapshot_fn: Any,
    timed_out_health_result_fn: Any,
) -> None:
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = [executor.submit(run_fn, index, account) for index, account in enumerate(accounts)]
    try:
        for future in as_completed(futures, timeout=max(30, int(batch_timeout or 900))):
            index, result = future.result()
            ordered[index] = result
            persist_snapshot_fn()
    except TimeoutError:
        for index, future in enumerate(futures):
            if ordered[index] is None:
                future.cancel()
                ordered[index] = timed_out_health_result_fn(
                    str(accounts[index].get("email") or "").strip(), "batch_timeout"
                )
        persist_snapshot_fn()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _finalize_account_probe_result(
    *,
    email: str,
    probe: dict[str, Any],
    relogin: dict[str, Any] | None,
    liveness_401: bool,
    relogin_attempted: bool,
    mailbox_auth_invalid: bool,
    mark_quota_status: Any,
    relogin_failure_quota_status: Any,
    health_status_code: Any,
) -> dict[str, Any]:
    status = str(probe.get("quota_status") or probe.get("status") or "未知")
    if relogin and not relogin.get("ok"):
        status = relogin_failure_quota_status(relogin)
    persisted = mark_quota_status(email, status, quota_result=probe) if email else False
    if email and isinstance(probe, dict) and probe.get("ok"):
        _clear_promotion_marker_after_probe(email)
    probe_ok = bool(probe.get("ok"))
    return {
        "ok": probe_ok,
        "email": email,
        "quota_status": status,
        "probe": probe,
        **({"relogin": relogin} if relogin else {}),
        "probe_ok": probe_ok,
        "persisted": bool(persisted),
        "health_status": health_status_code(probe, relogin),
        "liveness_401": bool(liveness_401),
        "relogin_attempted": bool(relogin_attempted),
        "mailbox_auth_invalid": bool(mailbox_auth_invalid),
    }


def _build_liveness_batch_summary(
    *,
    results: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    run_id: str,
    snapshot_path: Path,
    item_is_account_deactivated: Any,
    probe_is_token_invalid: Any,
    looks_account_deactivated: Any,
) -> dict[str, Any]:
    success = sum(1 for item in results if item.get("probe_ok"))
    persisted = sum(1 for item in results if item.get("persisted"))
    account_deactivated = sum(1 for item in results if item_is_account_deactivated(item))
    at_invalid = sum(1 for item in results if item.get("liveness_401"))
    probe_failed = sum(
        1
        for item in results
        if not item.get("probe_ok")
        and not item_is_account_deactivated(item)
        and not probe_is_token_invalid(item.get("probe"))
    )
    relogin_results = [relogin for item in results if isinstance((relogin := item.get("relogin")), dict)]
    relogin_success = sum(1 for item in relogin_results if item.get("ok"))
    relogin_deactivated = sum(1 for item in relogin_results if looks_account_deactivated(item))
    mailbox_auth_invalid = sum(1 for item in results if item.get("mailbox_auth_invalid"))
    relogin_attempted = sum(1 for item in results if item.get("relogin_attempted"))
    timed_out = sum(1 for item in results if item.get("timed_out"))
    _emit_account_batch_event(
        run_id,
        "batch_completed",
        "completed",
        total=len(results),
        detail="正常 {ok}/{total}，AT失效 {at_invalid}，掉号 {deactivated}，超时 {timed_out}".format(
            ok=success,
            total=len(results),
            at_invalid=at_invalid,
            deactivated=account_deactivated,
            timed_out=timed_out,
        ),
    )
    return {
        "ok": success == len(results) and len(results) == len(accounts),
        "mode": "local",
        "total": len(results),
        "completed": len(results) - timed_out,
        "success": success,
        "failed": len(results) - success,
        "persisted": persisted,
        "persist_failed": len(results) - persisted,
        "at_invalid": at_invalid,
        "account_deactivated": account_deactivated,
        "probe_failed": probe_failed,
        "relogin_attempted": relogin_attempted,
        "relogin_success": relogin_success,
        "relogin_failed": len(relogin_results) - relogin_success,
        "relogin_account_deactivated": relogin_deactivated,
        "liveness_401": at_invalid,
        "mailbox_auth_invalid": mailbox_auth_invalid,
        "results": results,
        "timed_out": sum(
            1
            for item in results
            if item.get("timed_out") or item.get("health_status") in {"batch_timeout", "account_timeout"}
        ),
        "batch_timed_out": sum(1 for item in results if item.get("health_status") == "batch_timeout"),
        "account_timed_out": sum(1 for item in results if item.get("health_status") == "account_timeout"),
        "partial": len(results) < len(accounts) or any(item.get("timed_out") for item in results),
        "unfinished": timed_out + max(0, len(accounts) - len(results)),
        "snapshot_path": str(snapshot_path),
    }


def _probe_liveness_with_retries(
    account: dict[str, Any],
    *,
    proxy: str | None,
    timeout: int,
    browser_fetch: Any = None,
    pool_offset: int = 0,
) -> dict[str, Any]:
    """Retry transport-only failures against the configured liveness pool."""
    from .account_recovery import (
        CFG,
        is_transient_transport_error,
        probe_account_liveness,
        proxy_pool_for,
    )

    has_affinity = bool((account.get("identity_context") or {}).get("proxy_affinity"))
    configured = proxy_pool_for(CFG, "liveness")
    # An explicit command proxy is authoritative.  Do not silently replace it
    # with a configured pool or discard it because the account has affinity.
    if proxy:
        candidates = [proxy] + [item for item in configured[:3] if item != proxy]
        source = "explicit"
    elif has_affinity:
        candidates = [None]
        source = "registration_affinity"
    else:
        if len(configured) > 1 and pool_offset:
            start = int(pool_offset) % len(configured)
            rotated = configured[start:] + configured[:start]
        else:
            rotated = configured
        candidates = rotated[:3] if rotated else [None]
        source = "operation_pool" if rotated else "default"
    last: dict[str, Any] = {}
    for candidate in candidates:
        effective_proxy = candidate
        last = probe_account_liveness(account, proxy=effective_proxy, timeout=timeout, browser_fetch=browser_fetch)
        if isinstance(last, dict):
            last.setdefault("proxy_source", source)
        if int(last.get("status_code") or 0) != 0 or last.get("ok"):
            return last
        if not is_transient_transport_error(last.get("error")):
            return last
    return last


def _needs_browser_fallback(probe: dict[str, Any]) -> bool:
    """Return true only for auth/challenge failures a browser can address."""
    if not isinstance(probe, dict) or probe.get("ok"):
        return False
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {0, 401, 403} or str(probe.get("status") or "").strip().lower() in {"unknown", "token_invalid"}:
        return True
    error = str(probe.get("error") or "").lower()
    return any(marker in error for marker in ("cloudflare", "challenge", "cf-", "invalid_probe_result"))


def _clear_promotion_marker_after_probe(email: str) -> None:
    """Clear a stale promotion 401 while remaining compatible with test seams."""
    from .account_recovery import clear_stale_promotion_at_marker

    try:
        clear_stale_promotion_at_marker(email, verified_at=int(time.time()))
    except TypeError:
        # Older injected test doubles only accept the original email argument.
        try:
            clear_stale_promotion_at_marker(email)
        except Exception:
            pass
    except Exception:
        pass


def _refresh_mailbox_quarantine_state() -> None:
    """Prune records for credentials removed or replaced in the repaired pool."""
    try:
        from ..mailbox.router import load_mailbox_pool

        load_mailbox_pool()
    except Exception:
        pass


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
        from ..desktop.ipc import emit_event

        emit_event(
            {
                "domain": "account_scan",
                "run_id": run_id,
                "account_ref": account_ref,
                "stage": stage,
                "status": status,
                "total": int(total or 0),
                "detail": detail,
            }
        )
    except Exception:
        pass


def inspect_accounts(
    emails: list[str] | None = None,
    *,
    workers: int = 4,
    proxy: str | None = None,
    proxy_pool: str | list[str] | None = None,
    timeout: int = 30,
    skew_seconds: int = DEFAULT_INSPECT_SKEW_SECONDS,
    relogin_on_401: bool = True,
    relogin_timeout: int | None = None,
    relogin_mode: str = "auto",
    batch_timeout: int | None = None,
    account_timeout: int | None = None,
    refresh_promotion: bool = True,
) -> dict[str, Any]:
    """Lightweight offline-triage account inspection backed by ``refresh_local_quota_statuses``."""
    return inspect_accounts_with_runner(
        emails=emails,
        refresh_quota_fn=refresh_local_quota_statuses,
        workers=workers,
        proxy=proxy,
        proxy_pool=proxy_pool,
        timeout=timeout,
        skew_seconds=skew_seconds,
        relogin_on_401=relogin_on_401,
        relogin_timeout=relogin_timeout,
        relogin_mode=relogin_mode,
        batch_timeout=batch_timeout,
        account_timeout=account_timeout,
        refresh_promotion=refresh_promotion,
    )
