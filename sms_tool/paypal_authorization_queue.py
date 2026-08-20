"""Durable queue for PayPal BA follow-up authorization.

Link extraction and BA authorization are deliberately separate operations.  The
queue persists the hand-off so a desktop/process restart cannot lose an already
generated approval link or accidentally execute it inline with extraction.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .paths import runtime_file
from .paypal_protocol import extract_ba_token
from .sanitizer import sanitize_text


_LOCK = threading.Lock()
_TERMINAL = {"completed", "failed", "cancelled", "unknown"}


def queue_path() -> Path:
    return runtime_file(None, "payment_queues") / "paypal_ba_authorization.json"


def enqueue_paypal_ba_authorization(
    *,
    email: str,
    approval_url: str,
    batch_id: str = "",
    account_ref: str = "",
    source_report: str = "",
) -> dict[str, Any]:
    url = str(approval_url or "").strip()
    ba_token = extract_ba_token(url) or ""
    if not ba_token:
        raise ValueError("PayPal approval URL does not contain a BA token")
    normalized_email = str(email or "").strip().lower()
    ref = str(account_ref or "").strip() or _account_ref(normalized_email)
    now = int(time.time())
    item = {
        "id": uuid.uuid4().hex,
        "email": normalized_email,
        "account_ref": ref,
        "batch_id": str(batch_id or "").strip(),
        "approval_url": url,
        "ba_token": ba_token,
        "source_report": str(source_report or "").strip(),
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        "last_error": "",
        "result": {},
    }
    with _LOCK:
        items = _load_unlocked()
        duplicate = next((existing for existing in items if existing.get("ba_token") == ba_token), None)
        if duplicate:
            return _public_item(duplicate)
        items.append(item)
        _write_unlocked(items)
    return _public_item(item)


def list_paypal_ba_authorizations(*, status: str = "", limit: int = 0) -> list[dict[str, Any]]:
    wanted = str(status or "").strip().lower()
    with _LOCK:
        items = _load_unlocked()
    if wanted:
        items = [item for item in items if str(item.get("status") or "").lower() == wanted]
    items.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    if limit > 0:
        items = items[: int(limit)]
    return [_public_item(item) for item in items]


def process_paypal_ba_authorizations(
    handler: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    limit: int = 0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    pending = list_paypal_ba_authorizations(status="pending", limit=limit)
    for queued in reversed(pending):
        queue_id = str(queued.get("id") or "")
        current = _update(queue_id, status="running", increment_attempt=True, last_error="")
        _emit(progress, current, "authorize", "running")
        try:
            result = dict(handler(_private_item(queue_id)) or {})
            status = _result_status(result)
            current = _update(
                queue_id,
                status=status,
                result=result,
                last_error="" if status == "completed" else str(result.get("error") or result.get("error_code") or ""),
            )
        except Exception as exc:
            current = _update(queue_id, status="failed", last_error=sanitize_text(str(exc)))
        _emit(progress, current, "authorize", str(current.get("status") or "failed"), account_terminal=True)
        processed.append(current)
    counts = {state: sum(item.get("status") == state for item in processed) for state in (*_TERMINAL, "running", "pending")}
    return {"ok": bool(processed) and counts.get("failed", 0) == 0 and counts.get("unknown", 0) == 0,
            "processed": len(processed), "counts": counts, "results": processed}


def _result_status(result: dict[str, Any]) -> str:
    if result.get("ok") is True:
        return "completed"
    status = str(result.get("status") or result.get("outcome") or "failed").strip().lower()
    return status if status in _TERMINAL else "failed"


def _update(queue_id: str, **changes: Any) -> dict[str, Any]:
    with _LOCK:
        items = _load_unlocked()
        item = next((value for value in items if value.get("id") == queue_id), None)
        if item is None:
            raise KeyError(f"PayPal BA queue item not found: {queue_id}")
        if changes.pop("increment_attempt", False):
            item["attempts"] = int(item.get("attempts") or 0) + 1
        item.update(changes)
        item["updated_at"] = int(time.time())
        _write_unlocked(items)
        return _public_item(item)


def _private_item(queue_id: str) -> dict[str, Any]:
    with _LOCK:
        item = next((value for value in _load_unlocked() if value.get("id") == queue_id), None)
    if item is None:
        raise KeyError(f"PayPal BA queue item not found: {queue_id}")
    return dict(item)


def _load_unlocked() -> list[dict[str, Any]]:
    path = queue_path()
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_unlocked(items: list[dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    value = {key: item.get(key) for key in (
        "id", "email", "account_ref", "batch_id", "source_report", "status",
        "attempts", "created_at", "updated_at", "last_error", "result",
    )}
    value["approval_url_present"] = bool(item.get("approval_url"))
    value["ba_token_present"] = bool(item.get("ba_token"))
    return value


def _account_ref(email: str) -> str:
    return hashlib.sha256(str(email or "").encode("utf-8")).hexdigest()[:16]


def _emit(progress: Callable[[dict[str, Any]], None] | None, item: dict[str, Any], stage: str, status: str, **extra: Any) -> None:
    if progress is None:
        return
    progress({"domain": "payment", "operation": "paypal_ba_authorize", "method": "paypal",
              "batch_id": item.get("batch_id", ""), "run_id": item.get("id", ""),
              "account_ref": item.get("account_ref", ""), "stage": stage, "status": status, **extra})


__all__ = ["enqueue_paypal_ba_authorization", "list_paypal_ba_authorizations", "process_paypal_ba_authorizations", "queue_path"]
