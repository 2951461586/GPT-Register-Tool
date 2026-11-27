"""Report assembly, checkpoint persistence, and canary state for payment batches.

Extracted from :mod:`sms_tool.payment.batch` so pure report/checkpoint/canary
bookkeeping stays cleanly separated from worker orchestration and routing.
Layer 5 (foundation & domain contract): depends only on ``kernel``, ``contracts``
and ``catalog``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..kernel.paths import runtime_file
from ..kernel.sanitizer import load_native_module, sanitize as _canonical_sanitize, sanitize_text as sanitize_text
from .catalog import PAYMENT_CATALOG
from .contracts import PaymentResult, as_bool, payment_history_metadata, payment_retry_allowed

_NATIVE_BATCH_REPORT_PARSE = load_native_module(
    "batch-report-parse-rs",
    "batch_report_parse_rs",
    "RUST_ANYTHING_UNIT_BATCH_REPORT_PARSE",
)
_NATIVE_ACCOUNT_REF = getattr(_NATIVE_BATCH_REPORT_PARSE, "account_ref", None)
_NATIVE_SAFE_BATCH_ID = getattr(_NATIVE_BATCH_REPORT_PARSE, "safe_batch_id", None)


def _normalize_payment_method(value: Any) -> str:
    return PAYMENT_CATALOG.normalize(value, default_for_blank=True)


def build_report(
    *,
    batch_id: str,
    method: str,
    started: float,
    workers: int,
    probe_only: bool,
    selected_count: int,
    results: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    report_path: Path,
    status: str,
    resumed: int,
    run_signature: str,
    resume_checkpoint: bool = False,
) -> dict[str, Any]:
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
        "counts": batch_counts(results, selected_count),
        "matrix": matrix_summary(results, cells),
        "results": results,
        "report_path": str(report_path),
    }


def batch_counts(results: list[dict[str, Any]], requested: int) -> dict[str, int]:
    decisions = [str(row.get("decision") or "").lower() for row in results]
    terminal_states = [
        str(row.get("terminal_state") or row.get("status") or row.get("state") or "").lower() for row in results
    ]
    return {
        "requested": requested,
        "probed": sum(bool(row.get("probed")) for row in results),
        "refreshed": sum(bool(row.get("refreshed")) for row in results),
        "authenticated": sum(bool(row.get("authenticated")) for row in results),
        "eligible": sum(row.get("eligible") is True for row in results),
        "capability_probed": sum(bool(row.get("capability_probed")) for row in results),
        "capability_unknown": sum(
            bool(row.get("capability_probed") and str(row.get("classification") or "") == "unknown") for row in results
        ),
        "attempted": sum(bool(row.get("attempted")) for row in results),
        "completed": sum(bool(row.get("ok") and row.get("attempted")) for row in results),
        "trial_ineligible": sum("trial_ineligible" in value for value in decisions),
        "card_only": sum("card_only" in value or "promo_nonzero" in value for value in decisions),
        "approve_blocked": sum("approve" in value and "ready" not in value for value in decisions),
        "link_ready": sum(bool(row.get("ok") and (row.get("url") or row.get("url_present"))) for row in results),
        "qr_ready": sum(is_qr_ready(row) for row in results),
        "terminal": sum(bool((row.get("auth") or {}).get("terminal")) for row in results),
        "failed": sum(not bool(row.get("ok")) for row in results),
        "cancelled": sum(state in {"cancelled", "canceled"} for state in terminal_states),
        "unknown": sum(state in {"unknown", "outcome_unknown"} for state in terminal_states),
        "reconciliation_required": sum(row.get("requires_reconciliation") is True for row in results),
        "timed_out": sum(state in {"timed_out", "timeout", "timeout_expired"} for state in terminal_states),
        "retryable": sum(row.get("retryable") is True for row in results),
    }


def matrix_cell_for(
    index: int,
    cells: list[dict[str, Any]],
    method: str,
    registration_country: str,
    *,
    normalize_method: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    norm = normalize_method or _normalize_payment_method
    if not cells:
        return {"name": "default"}
    method_cells = [
        cell
        for cell in cells
        if not cell.get("payment_method") or norm(str(cell.get("payment_method") or "")) == method
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
    schedule = [cell for cell in candidates for _ in range(max(1, int(cell.get("sample_size") or 1)))]
    return dict(schedule[index % len(schedule)])


def registration_country(context: Any) -> str:
    if not isinstance(context, dict):
        return ""
    return str(context.get("registration_country") or "").strip().upper()


def matrix_summary(results: list[dict[str, Any]], cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        output.append({"name": name, "registration_countries": countries, **batch_counts(rows, len(rows))})
    return output


def eligible_from_result(method: str, result: dict[str, Any]) -> bool | None:
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
        if result.get("has_kakao") is False or str(result.get("decision") or "") in {
            "nonzero_offer",
            "kakao_not_enabled",
        }:
            return False
    return None


def is_qr_ready(row: dict[str, Any]) -> bool:
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


def is_transient(result: dict[str, Any]) -> bool:
    return payment_retry_allowed(result)


def public_payment_result(result: dict[str, Any]) -> dict[str, Any]:
    blocked = {"access_token", "auth_context", "raw_output", "raw_output_tail", "state_history"}
    token_metadata = {
        "token_telemetry",
        "token_hash",
        "token_changed",
        "authorization_queued",
        "authorization_queue_id",
        "authorization_status",
    }
    return {
        key: value
        for key, value in dict(result or {}).items()
        if key not in blocked and ("token" not in key.lower() or key.lower() in token_metadata)
    }


def sanitize_report_value(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    blocked = {"email", "access_token", "refresh_token", "id_token", "auth_context", "password"}
    token_metadata = {
        "token_telemetry",
        "token_hash",
        "token_changed",
        "authorization_queued",
        "authorization_queue_id",
        "authorization_status",
    }
    if lowered in blocked or "proxy" in lowered or ("token" in lowered and lowered not in token_metadata):
        return None
    if isinstance(value, dict):
        return {
            item_key: sanitized
            for item_key, item_value in value.items()
            if (sanitized := sanitize_report_value(item_value, str(item_key))) is not None
        }
    if isinstance(value, list):
        return [sanitize_report_value(item) for item in value]
    return _canonical_sanitize(value, key=key)


def unique_emails(emails: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in emails or []:
        email = str(value or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            output.append(email)
    return output


def account_ref(email: str) -> str:
    if _NATIVE_ACCOUNT_REF is not None and (email is None or isinstance(email, str)):
        return _NATIVE_ACCOUNT_REF(email)
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def safe_batch_id(value: str) -> str:
    if _NATIVE_SAFE_BATCH_ID is not None and (value is None or isinstance(value, str)):
        return _NATIVE_SAFE_BATCH_ID(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:80]


def report_path_for(config: Any, batch_id: str, *, probe_only: bool = False) -> Path:
    suffix = ".probe" if probe_only else ".extract"
    return runtime_file(config, "payment_batches") / f"{batch_id}{suffix}.json"


def event_path_for(config: Any, batch_id: str, *, probe_only: bool = False) -> Path:
    suffix = ".probe" if probe_only else ".extract"
    return runtime_file(config, "payment_batches") / f"{batch_id}{suffix}.events.jsonl"


def append_event(path: Path, event: dict[str, Any], run_signature: str, lock: threading.Lock) -> None:
    record = _canonical_sanitize({"run_signature": run_signature, "event": event})
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def replay_events(path: Path, run_signature: str, progress: Callable[[dict[str, Any]], None] | None) -> None:
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


def reset_event_log(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_file():
            path.unlink()


def load_checkpoint(path: Path, method: str, run_signature: str) -> dict[str, Any]:
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


def checkpoint_row_resumable(row: dict[str, Any]) -> bool:
    """Reuse a checkpoint row as-is instead of re-running it."""
    contract = PaymentResult.from_mapping(row)
    outcome = contract.outcome
    if outcome.requires_reconciliation or outcome.side_effect_started:
        return True
    if row.get("ok") is True:
        return True
    return row.get("ok") is False and row.get("retryable") is False


def batch_run_signature(
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
        "payment_kwargs": payment_kwargs,
        "proxy": proxy or "",
        "retries": int(retries),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    persisted = payment_history_metadata(report)
    temporary.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def active_canary_pause(
    batch_cfg: dict[str, Any],
    method: str,
    *,
    state_path: Path | None = None,
    state_path_fn: Callable[[], Path] | None = None,
) -> dict[str, Any]:
    if as_bool(batch_cfg.get("pause_on_canary_failure", True)) is False:
        return {}
    resolved_path = state_path if state_path is not None else (state_path_fn() if state_path_fn is not None else None)
    if resolved_path is None or not resolved_path.is_file():
        return {}
    try:
        state = json.loads(resolved_path.read_text(encoding="utf-8"))
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


def record_canary_state(method: str, report: dict[str, Any], *, state_path: Path) -> dict[str, Any]:
    _rows_raw: Any = report.get("results")
    rows: list[Any] = _rows_raw if isinstance(_rows_raw, list) else []
    probe_only = bool(report.get("probe_only"))
    evaluated = [row for row in rows if (row.get("capability_probed") if probe_only else row.get("attempted"))]
    completed = (
        sum(bool(row.get("conclusive")) for row in evaluated)
        if probe_only
        else int((report.get("counts") or {}).get("completed") or 0)
    )
    conclusive_offer = {
        "account_trial_ineligible",
        "card_only_full_price",
        "promo_nonzero",
        "momo_not_enabled",
        "nonzero_offer",
        "wrong_currency",
        "kakao_not_enabled",
        "credential_invalid",
        "account_deactivated",
        "payment_method_unavailable",
    }
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
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
