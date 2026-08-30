"""Durable, deduplicated and bounded account-health background queue."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping

from .account_health import (
    AccountHealthResult,
    HealthCheckKind,
    liveness_health_result,
    plan_health_result,
)
from .paths import runtime_file


_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_TRANSIENT: dict[str, dict[str, Any]] = {}
_ACTIVE = {"pending", "running"}
_TERMINAL = {"completed", "failed", "cancelled"}


def queue_path() -> Path:
    return runtime_file(None, "account_health") / "queue.json"


def enqueue_account_health(
    email: str,
    kind: str,
    *,
    source: str = "",
    force: bool = False,
    auto_start: bool = True,
    max_pending: int = 1000,
    transient: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_email = str(email or "").strip().lower()
    normalized_kind = str(kind or "").strip().lower()
    if not normalized_email:
        raise ValueError("account_health_missing_email")
    if normalized_kind not in {item.value for item in HealthCheckKind}:
        raise ValueError(f"account_health_unknown_kind:{normalized_kind}")
    now = int(time.time())
    with _LOCK:
        items = _load_unlocked()
        duplicate = next(
            (
                item
                for item in reversed(items)
                if item.get("email") == normalized_email
                and item.get("kind") == normalized_kind
                and (
                    item.get("status") in _ACTIVE
                    or (
                        not force
                        and item.get("status") == "completed"
                        and now - int(item.get("updated_at") or 0) < 300
                    )
                )
            ),
            None,
        )
        if duplicate is not None:
            return _public_item(duplicate)
        active_count = sum(item.get("status") in _ACTIVE for item in items)
        if active_count >= max(1, int(max_pending or 1)):
            raise RuntimeError("account_health_queue_full")
        item = {
            "id": uuid.uuid4().hex,
            "email": normalized_email,
            "kind": normalized_kind,
            "source": str(source or "").strip(),
            "status": "pending",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "last_error": "",
            "result": {},
        }
        items.append(item)
        _write_unlocked(items)
        if transient:
            _TRANSIENT[item["id"]] = dict(transient)
    queued = _public_item(item)
    _emit(queued, "queued")
    if auto_start:
        start_account_health_worker()
    return queued


def enqueue_post_registration_checks(
    account: Mapping[str, Any],
    *,
    source: str = "registration",
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    email = str(account.get("email") or "").strip().lower()
    if not email or not account.get("success"):
        return []
    root = config if isinstance(config, Mapping) else {}
    health = root.get("account_health")
    health = health if isinstance(health, Mapping) else {}
    enabled = _as_bool(health.get("post_registration_enabled", True))
    if not enabled:
        return []
    workers = max(1, min(int(health.get("workers") or 2), 8))
    max_pending = max(1, min(int(health.get("max_pending") or 1000), 10000))
    jobs = [
        enqueue_account_health(
            email,
            HealthCheckKind.PLAN.value,
            source=source,
            auto_start=False,
            max_pending=max_pending,
        )
    ]
    if _as_bool(health.get("post_registration_deep_liveness", True)):
        jobs.append(
            enqueue_account_health(
                email,
                HealthCheckKind.DEEP_LIVENESS.value,
                source=source,
                auto_start=False,
                max_pending=max_pending,
            )
        )
    start_account_health_worker(workers=workers)
    return jobs


def list_account_health_jobs(*, status: str = "", limit: int = 0) -> list[dict[str, Any]]:
    wanted = str(status or "").strip().lower()
    with _LOCK:
        items = _load_unlocked()
    if wanted:
        items = [item for item in items if item.get("status") == wanted]
    items.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    if limit > 0:
        items = items[: int(limit)]
    return [_public_item(item) for item in items]


def process_account_health_jobs(
    *,
    workers: int = 2,
    limit: int = 0,
    handler: Callable[[dict[str, Any]], AccountHealthResult | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    max_workers = max(1, min(int(workers or 1), 8))
    with _LOCK:
        items = _load_unlocked()
        pending = [item for item in items if item.get("status") == "pending"]
        pending.sort(key=lambda item: int(item.get("created_at") or 0))
        selected: list[dict[str, Any]] = []
        selected_emails: set[str] = set()
        for item in pending:
            email = str(item.get("email") or "")
            if email in selected_emails:
                continue
            selected.append(item)
            selected_emails.add(email)
        pending = selected
        if limit > 0:
            pending = pending[: int(limit)]
        claimed_ids = {item["id"] for item in pending}
        now = int(time.time())
        for item in items:
            if item.get("id") in claimed_ids:
                item["status"] = "running"
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["updated_at"] = now
                item["worker_pid"] = os.getpid()
        if claimed_ids:
            _write_unlocked(items)
    if not pending:
        return {"ok": True, "processed": 0, "completed": 0, "failed": 0, "results": []}

    run = handler or _handle_job
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="account-health") as executor:
        futures = {executor.submit(run, dict(item)): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            try:
                value = future.result()
                result = value.to_dict() if isinstance(value, AccountHealthResult) else dict(value or {})
                updated = _update(
                    item["id"],
                    status="completed",
                    result=result,
                    last_error="",
                )
            except Exception as exc:
                updated = _update(
                    item["id"],
                    status="failed",
                    result={},
                    last_error=f"{type(exc).__name__}:{str(exc)[:300]}",
                )
            _TRANSIENT.pop(str(item.get("id") or ""), None)
            _emit(updated, str(updated.get("status") or "failed"))
            results.append(updated)
    completed = sum(item.get("status") == "completed" for item in results)
    return {
        "ok": completed == len(results),
        "processed": len(results),
        "completed": completed,
        "failed": len(results) - completed,
        "results": results,
    }


def start_account_health_worker(*, workers: int = 2) -> bool:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return False
        _WORKER = threading.Thread(
            target=_background_loop,
            kwargs={"workers": max(1, min(int(workers or 1), 8))},
            name="account-health-coordinator",
            daemon=False,
        )
        _WORKER.start()
        return True


def _background_loop(*, workers: int) -> None:
    global _WORKER
    try:
        while True:
            summary = process_account_health_jobs(workers=workers)
            if not summary.get("processed"):
                return
    finally:
        with _WORKER_LOCK:
            if _WORKER is threading.current_thread():
                _WORKER = None


def _handle_job(job: dict[str, Any]) -> AccountHealthResult:
    from .storage import get_account_record, mark_account_health_result

    email = str(job.get("email") or "").strip().lower()
    record = get_account_record(email)
    if not record:
        result = AccountHealthResult(
            email=email,
            check=str(job.get("kind") or ""),
            state="failed",
            ok=False,
            error="account_not_found",
        )
        return result
    account = _account_payload(record)
    transient = _TRANSIENT.get(str(job.get("id") or ""), {})
    proxy = transient.get("proxy")
    if job.get("kind") == HealthCheckKind.PLAN.value:
        from .account_promotion import check_account_promotion
        from .storage import mark_promotion_status

        probe = check_account_promotion(account, proxy=proxy)
        result = plan_health_result(email, probe)
        label_saved = mark_promotion_status(
            email,
            result.promotion_status,
            promotion_result=probe,
        )
        persisted = bool(label_saved and mark_account_health_result(email, result.to_dict()))
        return result.with_persisted(persisted)

    from .account_liveness import probe_account_liveness
    from .account_recovery import is_permanently_deactivated, relogin_codex_account
    from .storage import mark_quota_status

    if is_permanently_deactivated(account):
        initial = {
            "ok": False,
            "status": "account_deactivated",
            "quota_status": "account_deactivated",
            "error": "account_deactivated",
        }
        recovery: dict[str, Any] = {"terminal": True, "error": "account_deactivated"}
        final = initial
    else:
        initial = probe_account_liveness(account, proxy=proxy)
        recovery = {}
        final = initial
        if int(initial.get("status_code") or 0) == 401 or initial.get("status") == "token_invalid":
            recovery = relogin_codex_account(account, proxy=proxy, mode="auto")
            if recovery.get("ok"):
                refreshed_record = get_account_record(email)
                refreshed = _account_payload(refreshed_record or {})
                final = probe_account_liveness(refreshed, proxy=proxy)
    result = liveness_health_result(email, initial, recovery=recovery, final_probe=final)
    quota_saved = mark_quota_status(email, result.quota_status, quota_result=final)
    persisted = bool(quota_saved and mark_account_health_result(email, result.to_dict()))
    return result.with_persisted(persisted)


def _account_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"email": str(record.get("email") or "")}
    try:
        raw = json.loads(str(record.get("raw_json") or "{}"))
        if isinstance(raw, dict):
            value.update(raw)
    except Exception:
        pass
    for key in ("access_token", "id_token", "cookie_header", "device_id", "json_path", "status"):
        if record.get(key) not in (None, ""):
            value[key] = record.get(key)
    return value


def _update(queue_id: str, **changes: Any) -> dict[str, Any]:
    with _LOCK:
        items = _load_unlocked()
        item = next((value for value in items if value.get("id") == queue_id), None)
        if item is None:
            raise KeyError(f"account_health_job_not_found:{queue_id}")
        item.update(changes)
        item["updated_at"] = int(time.time())
        if item.get("status") in _TERMINAL:
            item.pop("worker_pid", None)
        _write_unlocked(items)
        return _public_item(item)


def _load_unlocked() -> list[dict[str, Any]]:
    path = queue_path()
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        items = [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        now = int(time.time())
        recovered = False
        for item in items:
            if item.get("status") != "running":
                continue
            pid = int(item.get("worker_pid") or 0)
            stale = now - int(item.get("updated_at") or 0) > 1800
            if stale or not _pid_alive(pid):
                item["status"] = "pending"
                item["last_error"] = "recovered_interrupted_worker"
                item["updated_at"] = now
                item.pop("worker_pid", None)
                recovered = True
        if recovered:
            _write_unlocked(items)
        return items
    except (OSError, TypeError, ValueError):
        return []


def _write_unlocked(items: list[dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "email",
            "kind",
            "source",
            "status",
            "attempts",
            "created_at",
            "updated_at",
            "last_error",
            "result",
        )
    }


def _emit(item: Mapping[str, Any], status: str) -> None:
    try:
        from .desktop_ipc import emit_event

        emit_event(
            {
                "domain": "account_health",
                "run_id": item.get("id", ""),
                "account_ref": item.get("email", ""),
                "stage": item.get("kind", ""),
                "status": status,
                "detail": str(item.get("last_error") or ""),
            }
        )
    except Exception:
        pass


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_KERNEL32 = None


def _windows_kernel32():
    """Return a lazily bound kernel32 handle, or None off Windows."""
    global _WINDOWS_KERNEL32
    if os.name != "nt":
        return None
    if _WINDOWS_KERNEL32 is None:
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            _WINDOWS_KERNEL32 = kernel32
        except Exception:
            _WINDOWS_KERNEL32 = False
    return _WINDOWS_KERNEL32 or None


def _windows_pid_alive(pid: int) -> bool:
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    import ctypes

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """Return True when *pid* is still running.

    ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: signal 0 aliases
    ``signal.CTRL_C_EVENT``, so the call maps to ``GenerateConsoleCtrlEvent``
    and broadcasts Ctrl+C to that process group.  CI runners start step
    processes with ``CREATE_NEW_PROCESS_GROUP``, which makes the current pid a
    process group id - so probing our own worker pid injected a spurious
    Ctrl+C and aborted the whole test session with KeyboardInterrupt.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


__all__ = [
    "enqueue_account_health",
    "enqueue_post_registration_checks",
    "list_account_health_jobs",
    "process_account_health_jobs",
    "queue_path",
    "start_account_health_worker",
]
