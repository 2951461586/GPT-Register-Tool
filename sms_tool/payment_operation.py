"""Durable idempotency and recovery boundary for payment operations."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .cross_process_gate import CrossProcessSemaphore, GateTimeoutError
from .paths import runtime_file
from .payment_contracts import PaymentResult
from .sanitizer import sanitize_text


SCHEMA_VERSION = 1


class PaymentOperationConflict(RuntimeError):
    """Raised when replay could duplicate a completed or uncertain payment."""

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.record = dict(record)
        status = str(self.record.get("status") or "unknown")
        super().__init__(f"payment operation cannot be replayed while status is {status}")


@dataclass
class PaymentOperation:
    path: Path
    gate: CrossProcessSemaphore
    record: dict[str, Any]
    _closed: bool = False

    @property
    def operation_id(self) -> str:
        return str(self.record["operation_id"])

    @property
    def idempotency_key_hash(self) -> str:
        return str(self.record["idempotency_key_hash"])

    def checkpoint(
        self,
        stage: str,
        state: str = "running",
        *,
        side_effect_started: bool | None = None,
        error_code: str = "",
    ) -> None:
        if self._closed:
            return
        self.record["sequence"] = int(self.record.get("sequence") or 0) + 1
        self.record["stage"] = str(stage or "unknown")
        self.record["status"] = str(state or "running")
        self.record["updated_at"] = int(time.time())
        if side_effect_started is not None:
            self.record["side_effect_started"] = bool(side_effect_started)
        if error_code:
            self.record["error_code"] = sanitize_text(error_code)[:120]
        _atomic_write(self.path, self.record)

    def finish(self, result: Mapping[str, Any]) -> None:
        contract = PaymentResult.from_mapping(result)
        self.record.update({
            "status": contract.outcome.status,
            "stage": contract.error.stage or "completed",
            "side_effect_started": contract.outcome.side_effect_started,
            "requires_reconciliation": contract.outcome.requires_reconciliation,
            "retryable": contract.error.retryable,
            "error_code": sanitize_text(contract.error.code)[:120],
            "updated_at": int(time.time()),
            "finished_at": int(time.time()),
            "sequence": int(self.record.get("sequence") or 0) + 1,
        })
        _atomic_write(self.path, self.record)
        self.close()

    def fail_unknown(self, stage: str, error_code: str) -> None:
        self.record.update({
            "status": "unknown",
            "stage": str(stage or "executor"),
            "side_effect_started": True,
            "requires_reconciliation": True,
            "retryable": False,
            "error_code": sanitize_text(error_code)[:120],
            "updated_at": int(time.time()),
            "finished_at": int(time.time()),
            "sequence": int(self.record.get("sequence") or 0) + 1,
        })
        _atomic_write(self.path, self.record)
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gate.release()


class PaymentOperationStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PaymentOperationStore":
        return cls(runtime_file(config, "payment_operations"))

    def begin(
        self,
        *,
        payment_method: str,
        operation: str,
        idempotency_key: str,
        operation_id: str = "",
    ) -> PaymentOperation:
        key = str(idempotency_key or operation_id or uuid.uuid4().hex)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.root / f"{key_hash}.json"
        gate = CrossProcessSemaphore(
            f"payment-operation-{key_hash}",
            1,
            base_dir=self.root,
        )
        try:
            gate.acquire(timeout=30)
        except GateTimeoutError as exc:
            previous = _read_record(path)
            raise PaymentOperationConflict(previous or {
                "status": "running",
                "stage": "idempotency",
                "side_effect_started": False,
                "requires_reconciliation": True,
                "operation_id": str(operation_id or ""),
                "idempotency_key_hash": key_hash,
            }) from exc
        try:
            previous = _read_record(path)
            if previous and not _replay_allowed(previous):
                raise PaymentOperationConflict(previous)
            attempt = int(previous.get("attempt") or 0) + 1 if previous else 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "operation_id": _public_operation_id(operation_id),
                "idempotency_key_hash": key_hash,
                "payment_method": str(payment_method or ""),
                "operation": str(operation or "extract_link"),
                "attempt": attempt,
                "sequence": 1,
                "status": "running",
                "stage": "created",
                "side_effect_started": False,
                "requires_reconciliation": False,
                "retryable": False,
                "error_code": "",
                "started_at": int(time.time()),
                "updated_at": int(time.time()),
                "finished_at": 0,
                "recovered_from_stale": bool(previous and previous.get("status") == "running"),
            }
            _atomic_write(path, record)
            return PaymentOperation(path=path, gate=gate, record=record)
        except BaseException:
            gate.release()
            raise


def conflict_result(conflict: PaymentOperationConflict) -> dict[str, Any]:
    previous = conflict.record
    uncertain = bool(
        previous.get("requires_reconciliation")
        or previous.get("side_effect_started")
        or previous.get("status") in {"running", "unknown", "timed_out"}
    )
    return {
        "ok": False,
        "status": "unknown" if uncertain else "failed",
        "error": sanitize_text(str(conflict)),
        "error_code": "payment_operation_reconciliation_required" if uncertain else "payment_operation_already_finalized",
        "error_stage": str(previous.get("stage") or "idempotency"),
        "retryable": False,
        "side_effect_started": bool(previous.get("side_effect_started")),
        "requires_reconciliation": uncertain,
        "operation_id": str(previous.get("operation_id") or ""),
        "idempotency_key_hash": str(previous.get("idempotency_key_hash") or ""),
    }


def _replay_allowed(record: Mapping[str, Any]) -> bool:
    if record.get("requires_reconciliation") or record.get("side_effect_started"):
        return False
    status = str(record.get("status") or "unknown")
    if status == "running":
        return True
    return status in {"failed", "timed_out"} and record.get("retryable") is True


def _public_operation_id(value: Any) -> str:
    """Keep correlation IDs opaque even when callers derive them from emails or URLs."""
    raw = str(value or uuid.uuid4().hex).strip()
    if re.fullmatch(r"[0-9a-f]{32}", raw, re.IGNORECASE):
        return raw.lower()
    return "op_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _read_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise PaymentOperationConflict({
            "status": "unknown",
            "stage": "operation_journal",
            "side_effect_started": True,
            "requires_reconciliation": True,
            "error_code": f"operation_journal_{type(exc).__name__.lower()}",
        }) from exc
    return dict(value) if isinstance(value, Mapping) else {}


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "PaymentOperation",
    "PaymentOperationConflict",
    "PaymentOperationStore",
    "conflict_result",
]
