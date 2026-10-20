"""persistence submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any
from ..config import current_config_data
from ..payment_contracts import payment_history_metadata
from ..payment_catalog import (
    PAYMENT_METHODS as CATALOG_METHODS,
    normalize_payment_method as normalize_catalog_payment_method,
    validate_catalog_consistency,
)
from ..payment_operation import (
    PaymentOperationConflict,
    PaymentOperationStore,
    conflict_result as payment_operation_conflict_result,
)
from ..payment_routing import (
    PaymentRoutePlan,
    PaymentRoutePlanner,
    coerce_approve_country as canonical_coerce_approve_country,
    parse_proxy_pool,
    payment_proxy_pools as canonical_payment_proxy_pools,
)

from .base import _STATE_LOCK, _redact_sensitive_values, _state_path

#: Rotate the append-only run trail at this size. The live file is in the
#: retention planner's ``NEVER_DELETE`` set (it is reconciliation state, not
#: output), so rotation is the only way to bound it: at 8 MiB it held ~5.7 MiB of
#: complete records on 2026-10-20 and only ever grows.
ROTATE_AFTER_BYTES = 8 * 1024 * 1024


def _rotate_if_needed(path: Path) -> Path | None:
    """Move the live trail aside once it crosses :data:`ROTATE_AFTER_BYTES`.

    Returns the archive path, or ``None`` when nothing was rotated. The archive
    keeps a UTC timestamp so a same-second rotation cannot collide, and
    ``scripts/runtime_retention.py`` sweeps ``payment_link_runs.*.jsonl`` after
    30 days -- the live file itself stays protected.
    """
    try:
        if path.stat().st_size < ROTATE_AFTER_BYTES:
            return None
    except FileNotFoundError:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    suffix = 1
    while archive.exists():
        archive = path.with_name(f"{path.stem}.{stamp}-{suffix}{path.suffix}")
        suffix += 1
    os.replace(path, archive)
    return archive


def _persist_run(result: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {}
    for key, value in result.items():
        lowered = key.lower()
        # Key 黑名单：原始子进程输出、token、proxy 已知不能落盘；
        # card_* / card_last4 / pan 同样是敏感凭据 —— 浏览器支付路径
        # 会把卡号末四位塞进返回 dict，这里按 key 名拦截，避免进 jsonl。
        if (
            lowered in {"raw_output", "raw_output_tail"}
            or "token" in lowered
            or "proxy" in lowered
            or lowered.startswith("card_")
            or lowered in {"card", "pan", "cardnumber", "card_number"}
        ):
            continue
        record[key] = value
    record = payment_history_metadata(record)
    record = _redact_sensitive_values(record)
    with _STATE_LOCK:
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _safe_persist_run(result: dict[str, Any]) -> None:
    try:
        _persist_run(result)
    except (OSError, TypeError, ValueError) as exc:
        result["persistence_warning"] = f"payment run state was not persisted: {type(exc).__name__}"
