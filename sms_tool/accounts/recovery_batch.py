"""测活批量引擎（refresh_local_quota_statuses 及其闭包）——候选1 god file 拆解。

从 ``account_recovery.py``（1530 行）拆出的批处理引擎：线程池、heavy-lane
信号量、每账号/整批截止线、快照持久化与清理、IPC 批事件。恢复策略
（``relogin_codex_account`` 等）仍归 ``account_recovery``——引擎通过**函数内
延迟导入**引用策略层，避免导入期循环；``account_recovery`` 用 PEP 562
``__getattr__`` 兼容再导出本模块的公共名。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def __getattr__(name: str):
    """引擎体对策略层与既有助手的引用在调用期经 account_recovery 解析。

    (a) recovery_batch → account_recovery 是调用期依赖，导入期零循环；
    (b) 测试通过 ``patch.object(account_recovery, "probe_account_liveness", …)``
        打桩的协作者继续保持生效——切分不改变 patch 面。
    """
    import importlib

    module = importlib.import_module(".account_recovery", __package__)
    return getattr(module, name)
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


def _probe_is_fresh_definitive(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict) or not probe:
        return False
    return str(probe.get("status") or "").strip().lower() in _FRESH_PROBE_DEFINITIVE_STATUSES


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
    # 策略层与既有助手在调用期经 account_recovery 解析：导入期零循环，
    # 且测试对 account_recovery.* 的 patch 继续生效（切分不改变 patch 面）。
    from .account_recovery import (
        CFG,
        _TRANSIENT_RELOGIN_MODES,
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
        _safe_relogin_result,
        _timed_out_health_result,
        account_identity,
        browser_fetch_for_account,
        clear_stale_promotion_at_marker,
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
    run_id = uuid.uuid4().hex
    _emit_account_batch_event(
        run_id,
        "batch_started",
        "running",
        total=len(accounts),
        detail="账号测活开始",
    )
    # The liveness probe is a single light GET, so a modestly higher ceiling keeps
    # a full-pool scan responsive; heavy 401 relogins only run for invalid tokens.
    max_workers = max(1, min(int(workers or 1), 16, len(accounts) or 1))
    # Heavy lanes (browser fallback, 401 relogin) stay narrower than the probe
    # pool so a slow recovery cannot starve cheap probes -- but their capacity
    # follows max_workers. The old values were pinned at 2 (`min(2, max_workers)`
    # and a literal `2`), so raising the UI concurrency to 8 still funnelled
    # every browser fallback and relogin through two slots. Combined with the
    # short/no-wait acquires below, that turned "queued" into "skipped" for
    # every account after the first two.
    heavy_lane_slots = _heavy_lane_slots(max_workers)
    # A normal liveness probe is a single HTTP request. Browser sessions are a
    # bounded fallback for 401/Cloudflare responses and must not consume the
    # whole batch's worker pool while they start and tear down.
    browser_slots = threading.BoundedSemaphore(heavy_lane_slots)
    # Recovery is materially heavier than the probe (mailbox/OAuth/browser).
    # Keep it in a separate lane so enabling the 401 checkbox cannot starve
    # lightweight probes for the rest of the batch.
    relogin_slots = threading.BoundedSemaphore(heavy_lane_slots)
    batch_deadline = time.monotonic() + max(30, int(batch_timeout or 900))
    # OTP recovery needs a materially longer budget than a single HTTP probe.
    # ``email.otp_timeout`` is 180s in config.json, but recovery was capped by
    # the per-account probe budget (120s from the desktop planner), so every
    # OTP poll was cut off before the window closed -- ~80% of a batch came
    # back ``existing_login_otp_poll_timeout`` / ``passwordless_email_otp_poll_
    # timeout``. Recovery now gets its own budget, bounded by the batch
    # deadline rather than by the probe's.
    relogin_budget = max(30, int(relogin_timeout or timeout or 300))
    ordered: list[dict[str, Any] | None] = [None] * len(accounts)
    snapshot_path = runtime_file(CFG, "account_liveness_batches") / f"{run_id}.json"

    def persist_snapshot(*, terminal: bool = False) -> None:
        """Persist completed rows while workers are still running.

        The WPF process can be killed at its outer deadline; keeping this
        snapshot beside runtime state makes the already completed probes
        recoverable instead of losing the whole batch envelope.
        """
        rows = [item for item in ordered if item is not None]
        timed_out_rows = [item for item in rows if item.get("timed_out")]
        payload = {
            "run_id": run_id,
            "total": len(accounts),
            "completed": max(0, len(rows) - len(timed_out_rows)),
            "unfinished": len(timed_out_rows) + max(0, len(accounts) - len(rows)),
            "partial": bool(timed_out_rows or len(rows) < len(accounts)),
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
            # A silent snapshot failure left "crash-recoverable" batches that
            # were never actually persisted; make it observable at least.
            logger.warning("liveness snapshot persist failed run_id=%s: %s", run_id, exc)

    def run(index: int, account: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        email = str(account.get("email") or "").strip()
        account_deadline = min(batch_deadline, time.monotonic() + max(30, int(account_timeout or 360)))
        # Widened by the relogin branch below. A recovery that is deliberately
        # allowed to run for ``relogin_budget`` must not then be stamped
        # "account_timeout" by the probe's much shorter deadline.
        effective_deadline = account_deadline
        try:
            if time.monotonic() >= account_deadline:
                return index, _timed_out_health_result(email, "account_timeout_before_probe")
            probe_timeout = max(5, min(int(timeout or 30), int(account_deadline - time.monotonic())))
            if is_permanently_deactivated(account):
                probe = {
                    "ok": False,
                    "mode": "local",
                    "status": "account_deactivated",
                    "quota_status": "account_deactivated",
                    "error": "account_deactivated",
                    "terminal": True,
                }
            elif _is_token_revoked_drop(account):
                # 掉号: the AT was revoked and the account holds no recovery
                # material. Skip the probe — and especially the expensive
                # browser fallback a 401 would trigger — while keeping the
                # token_invalid classification so batch counts stay accurate.
                probe = {
                    "ok": False,
                    "mode": "local",
                    "status": "token_invalid",
                    "quota_status": "401失效",
                    "error": "token_revoked_unrecoverable",
                    "terminal": True,
                }
            else:
                # A scan pass that just ran probe_account_liveness for this
                # account hands its verdict in via ``fresh_probes``; re-probing
                # wham minutes later doubled the Cloudflare-401 exposure and
                # let the two passes disagree. Reuse only definitive
                # classifications -- transport-unknown results still re-probe.
                fresh = (fresh_probes or {}).get(str(email).strip().lower())
                if _probe_is_fresh_definitive(fresh):
                    probe = {**fresh, "probe_source": "scan_reuse"}
                else:
                    # Fast path: use the canonical lightweight quota endpoint for
                    # every account, including accounts that have a browser
                    # identity. This avoids opening a browser for healthy tokens.
                    probe = _probe_liveness_with_retries(
                        account, proxy=proxy, timeout=probe_timeout, browser_fetch=None
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
                    # Queue for a slot until this account's own deadline. The
                    # old one-second wait meant that with more than two
                    # Cloudflare-blocked accounts in a batch, every account
                    # after the second reported "concurrency_limited" instead
                    # of just waiting its turn.
                    acquired = remaining > 0.0 and browser_slots.acquire(timeout=remaining)
                    if acquired:
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
            initial_status = str(probe.get("quota_status") or probe.get("status") or "unknown")
            liveness_401 = _probe_is_token_invalid(probe)
            # Terminal synthetic probes (dropped 掉号 accounts) keep the
            # token_invalid classification so batch counts stay accurate, but
            # must not re-enter the relogin cascade — that is the expensive
            # path the skip exists to avoid.
            relogin_eligible = bool(relogin_on_401 and liveness_401 and email and not probe.get("terminal"))
            relogin_attempted = False
            mailbox_auth_invalid = False
            if email and relogin_on_401 and liveness_401:
                # Persist the probe before the optional recovery path. A stuck
                # OTP/browser recovery must never hide the fact that the AT
                # probe already completed.
                mark_quota_status(email, initial_status, quota_result=probe)
            relogin: dict[str, Any] = {}
            if relogin_eligible and not mailbox_relogin_allowed(email):
                relogin = {"ok": False, "mode": "disabled", "error": "mailbox_pool_repair_required"}
            elif relogin_eligible and _relogin_cooldown_active(account):
                relogin = {"ok": False, "mode": "cooldown", "error": "relogin_cooldown"}
            elif relogin_eligible and time.monotonic() < account_deadline:
                # Queue for a lane against the *batch* deadline, never the
                # per-account one. Time spent waiting for a slot used to be
                # subtracted from the recovery budget, so an account queued
                # behind a full lane started its OTP poll with ~30s left and
                # could never succeed no matter how healthy its mailbox was.
                queue_remaining = max(0.0, batch_deadline - time.monotonic())
                # Queue instead of skipping. A non-blocking acquire meant that
                # enabling "401 relogin" on a batch larger than the lane
                # silently dropped every account that could not grab a slot
                # immediately -- that is what surfaced in the UI as
                # "重登跳过：并发槽已满".
                if queue_remaining > 0.0 and relogin_slots.acquire(timeout=queue_remaining):
                    try:
                        relogin_attempted = True
                        # Recovery owns a full ``relogin_budget`` from the
                        # moment it acquires a lane -- long enough for an OTP
                        # round trip. Only the batch deadline can cut it short.
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
                        elif relogin:
                            _record_relogin_failure(email, relogin)
                        mailbox_auth_invalid = str(relogin.get("error") or "") == "mailbox_auth_invalid"
                    finally:
                        relogin_slots.release()
                else:
                    relogin = {"ok": False, "mode": "concurrency_limited", "error": "relogin_concurrency_limited"}
            if (
                liveness_401
                and not probe.get("ok")
                and not relogin.get("ok")
                and not relogin.get("terminal")
                and not _looks_account_deactivated(relogin)
                and str(relogin.get("mode") or "") not in _TRANSIENT_RELOGIN_MODES
                and not _has_relogin_material(account)
            ):
                # 掉号 marking: the AT is revoked and no relogin strategy has
                # any credential to work with (no password, no refresh token,
                # no mailbox access) — including the case where the mailbox
                # pool breaker disabled relogin entirely. Accounts whose only
                # blocker is that breaker keep their mailbox material and stay
                # unmarked: lifting the breaker lets OTP recovery answer
                # definitively (and relogin itself persists confirmed
                # deactivations via _persist_permanent_deactivation).
                try:
                    if _persist_token_revoked_drop(account):
                        probe = {**probe, "dropped": "token_revoked"}
                except Exception:
                    pass
            if (
                time.monotonic() >= effective_deadline
                and not probe.get("ok")
                and not _probe_is_token_invalid(probe)
                and not probe.get("terminal")
                and str(probe.get("status") or "") != "account_deactivated"
            ):
                # A deadline must never mask a definitive classification. A
                # confirmed 401 (or terminal verdict) that simply ran past the
                # probe budget used to be rewritten to "网络超时", hiding the
                # real failure from the panel and from 掉号 accounting.
                probe = {**probe, "status": "timeout", "error": probe.get("error") or "account_timeout"}
            status = str(probe.get("quota_status") or probe.get("status") or "未知")
            if relogin and not relogin.get("ok"):
                status = _relogin_failure_quota_status(relogin)
            persisted = mark_quota_status(email, status, quota_result=probe) if email else False
            if email and isinstance(probe, dict) and probe.get("ok"):
                _clear_promotion_marker_after_probe(email)
            probe_ok = bool(probe.get("ok"))
            result = {
                "ok": probe_ok,
                "email": email,
                "quota_status": status,
                "probe": probe,
                **({"relogin": relogin} if relogin else {}),
                "probe_ok": probe_ok,
                "persisted": bool(persisted),
                "health_status": _health_status_code(probe, relogin),
                "liveness_401": bool(liveness_401),
                "relogin_attempted": bool(relogin_attempted),
                "mailbox_auth_invalid": bool(mailbox_auth_invalid),
            }
        except Exception as exc:
            result = {
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
        _emit_account_batch_event(
            run_id,
            "account_completed",
            "completed" if result.get("probe_ok") else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(result.get("quota_status") or "检测完成"),
        )
        return index, result

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = [executor.submit(run, index, account) for index, account in enumerate(accounts)]
    try:
        for future in as_completed(futures, timeout=max(30, int(batch_timeout or 900))):
            index, result = future.result()
            ordered[index] = result
            persist_snapshot()
    except TimeoutError:
        for index, future in enumerate(futures):
            if ordered[index] is None:
                future.cancel()
                ordered[index] = _timed_out_health_result(
                    str(accounts[index].get("email") or "").strip(), "batch_timeout"
                )
        persist_snapshot()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    results = [item for item in ordered if item is not None]
    # Remote health is independent from local marker persistence.  A healthy
    # account whose SQLite write failed remains healthy and is reported through
    # ``persist_failed`` instead of being counted as a dead account.
    success = sum(1 for item in results if item.get("probe_ok"))
    persisted = sum(1 for item in results if item.get("persisted"))
    account_deactivated = sum(1 for item in results if _item_is_account_deactivated(item))
    # Count the original liveness result, even when an optional relogin later
    # replaces the probe with a fresh HTTP 200 result.
    at_invalid = sum(1 for item in results if item.get("liveness_401"))
    probe_failed = sum(
        1
        for item in results
        if not item.get("probe_ok")
        and not _item_is_account_deactivated(item)
        and not _probe_is_token_invalid(item.get("probe"))
    )
    relogin_results = [item.get("relogin") for item in results if isinstance(item.get("relogin"), dict)]
    relogin_success = sum(1 for item in relogin_results if item.get("ok"))
    relogin_deactivated = sum(1 for item in relogin_results if _looks_account_deactivated(item))
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
    persist_snapshot(terminal=True)
    _prune_liveness_snapshots(snapshot_path.parent, exclude=snapshot_path.name)
    return {
        "ok": success == len(results) and len(results) == len(accounts),
        "mode": "local",
        "total": len(results),
        "completed": len(results) - sum(1 for item in results if item.get("timed_out")),
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
        "timed_out": sum(1 for item in results if item.get("timed_out") or item.get("health_status") in {"batch_timeout", "account_timeout"}),
        "batch_timed_out": sum(1 for item in results if item.get("health_status") == "batch_timeout"),
        "account_timed_out": sum(1 for item in results if item.get("health_status") == "account_timeout"),
        "partial": len(results) < len(accounts) or any(item.get("timed_out") for item in results),
        "unfinished": sum(1 for item in results if item.get("timed_out")) + max(0, len(accounts) - len(results)),
        "snapshot_path": str(snapshot_path),
    }


def _probe_liveness_with_retries(
    account: dict[str, Any],
    *,
    proxy: str | None,
    timeout: int,
    browser_fetch: Any = None,
) -> dict[str, Any]:
    """Retry transport-only failures against the configured liveness pool."""
    from .account_recovery import (
        CFG,
        browser_fetch_for_account,
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
        candidates = configured[:3] if configured else [None]
        source = "operation_pool" if configured else "default"
    last: dict[str, Any] = {}
    for candidate in candidates:
        effective_proxy = candidate
        last = probe_account_liveness(
            account, proxy=effective_proxy, timeout=timeout, browser_fetch=browser_fetch
        )
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
    from .account_recovery import clear_stale_promotion_at_marker
    """Clear a stale promotion 401 while remaining compatible with test seams."""
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
    from .account_recovery import CFG
    """Prune records for credentials removed or replaced in the repaired pool."""
    try:
        from ..mailbox import _load_mailbox_pool

        _load_mailbox_pool()
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
        from ..desktop_ipc import emit_event

        emit_event({
            "domain": "account_scan",
            "run_id": run_id,
            "account_ref": account_ref,
            "stage": stage,
            "status": status,
            "total": int(total or 0),
            "detail": detail,
        })
    except Exception:
        pass
