"""First-class batch executor for protocol payment extraction."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import CFG
from .paths import runtime_file
from .payment_auth import ensure_payment_access_token, public_payment_auth_result
from .payment_link_manager import generate_payment_link, normalize_payment_method


_TRANSIENT_MARKERS = (
    "network", "timeout", "timed out", "tls", "proxy", "http_407", "http_429",
    "rate_limit", "cloudflare", "checkout_failed", "stripe_init_failed",
)


def load_payment_matrix(value: Any = None) -> list[dict[str, Any]]:
    """Load matrix cells from a JSON string/path or protocol_payments config."""
    raw = value
    if raw in (None, "", False):
        protocol = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), dict) else {}
        raw = protocol.get("matrix") or []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        path = Path(text)
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig") if path.is_file() else text)
        except (OSError, ValueError, TypeError):
            return []
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
    retries: int = 1,
    timeout: int = 30,
) -> dict[str, Any]:
    method = normalize_payment_method(payment_method)
    if not method:
        raise ValueError(f"unsupported payment method: {payment_method}")
    selected = _unique_emails(emails)
    if canary:
        selected = selected[: max(1, int(canary))]
    started = time.time()
    batch_id = _safe_batch_id(batch_id) or f"{method}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    cells = load_payment_matrix(matrix)
    protocol_cfg = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), dict) else {}
    batch_cfg = protocol_cfg.get("batch") if isinstance(protocol_cfg.get("batch"), dict) else {}
    if not canary:
        paused = _active_canary_pause(batch_cfg, method)
        if paused:
            raise RuntimeError(f"payment_batch_paused_by_canary:{paused.get('reason') or 'protocol_profile_failed'}")
    method_caps = batch_cfg.get("method_workers") if isinstance(batch_cfg.get("method_workers"), dict) else {}
    default_cap = 2 if method in {"momo", "kakao"} else 4
    cap = max(1, int(method_caps.get(method) or default_cap))
    max_workers = max(1, min(int(workers or 1), cap, len(selected) or 1))
    retry_count = max(0, min(int(retries or 0), 2))
    base_kwargs = dict(payment_kwargs or {})
    report_path = _report_path(batch_id)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_checkpoint(report_path, method)
    existing_by_ref = {
        str(row.get("account_ref") or ""): row
        for row in (existing.get("results") or [])
        if isinstance(row, dict) and row.get("account_ref")
    }
    ordered: list[dict[str, Any] | None] = [
        existing_by_ref.get(_account_ref(email)) for email in selected
    ]
    pending = [(index, email) for index, email in enumerate(selected) if ordered[index] is None]
    checkpoint_lock = threading.Lock()

    def run_one(index: int, email: str) -> tuple[int, dict[str, Any]]:
        auth = ensure_payment_access_token(
            email=email,
            proxy=proxy,
            timeout=timeout,
            relogin_on_401=jit_refresh,
            stabilization_probes=1,
        )
        registration_country = str((auth.get("auth_context") or {}).get("registration_country") or "").upper()
        cell = _matrix_cell_for(index, cells, method, registration_country)
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
            "attempted": False,
            "ok": False,
            "decision": "",
            "error": "",
        }
        if not auth.get("ok"):
            row["decision"] = str(auth.get("error") or "jit_auth_failed")
            row["error"] = row["decision"]
            return index, row
        if cell.get("matrix_mismatch"):
            row["eligible"] = False
            row["decision"] = "matrix_registration_country_mismatch"
            row["error"] = row["decision"]
            return index, row
        kwargs = _cell_payment_kwargs(base_kwargs, cell, proxy)
        if probe_only:
            kwargs["probe_only"] = True
        last: dict[str, Any] = {}
        for attempt in range(1, retry_count + 2):
            row["attempted"] = True
            last = generate_payment_link(
                access_token=str(auth.get("access_token") or ""),
                proxy=proxy,
                payment_method=method,
                auth_context=auth.get("auth_context") if isinstance(auth.get("auth_context"), dict) else None,
                **kwargs,
            )
            if last.get("ok") or not _is_transient(last) or attempt > retry_count:
                break
        public = _public_payment_result(last)
        decision = str(public.get("decision") or public.get("error_code") or ("ready" if public.get("ok") else "failed"))
        eligible = _eligible_from_result(method, public)
        row.update(public)
        row.update({
            "auth": public_auth,
            "attempted": True,
            "eligible": eligible,
            "decision": decision,
            "attempts": attempt,
        })
        if probe_only:
            row["ok"] = bool(row.get("authenticated") and row.get("decision") not in {"credential_invalid", "account_deactivated", "checkout_failed"})
        return index, row

    def checkpoint(status: str) -> dict[str, Any]:
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
        )
        with checkpoint_lock:
            _write_checkpoint(report_path, report)
        return report

    if pending:
        checkpoint("running")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_one, index, email): (index, email) for index, email in pending}
        for future in as_completed(futures):
            fallback_index, fallback_email = futures[future]
            try:
                index, row = future.result()
            except Exception as exc:
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
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ordered[index] = row
            checkpoint("running")
    report = checkpoint("finished")
    if canary:
        report["canary_state"] = _record_canary_state(method, report)
        _write_checkpoint(report_path, report)
    return report


def _build_report(*, batch_id: str, method: str, started: float, workers: int,
                  probe_only: bool, selected_count: int, results: list[dict[str, Any]],
                  cells: list[dict[str, Any]], report_path: Path, status: str,
                  resumed: int) -> dict[str, Any]:
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
        "resumed": resumed,
        "counts": _batch_counts(results, selected_count),
        "matrix": _matrix_summary(results, cells),
        "results": results,
        "report_path": str(report_path),
    }


def _batch_counts(results: list[dict[str, Any]], requested: int) -> dict[str, int]:
    decisions = [str(row.get("decision") or "").lower() for row in results]
    return {
        "requested": requested,
        "probed": sum(bool(row.get("probed")) for row in results),
        "refreshed": sum(bool(row.get("refreshed")) for row in results),
        "authenticated": sum(bool(row.get("authenticated")) for row in results),
        "eligible": sum(row.get("eligible") is True for row in results),
        "attempted": sum(bool(row.get("attempted")) for row in results),
        "completed": sum(bool(row.get("ok")) for row in results),
        "trial_ineligible": sum("trial_ineligible" in value for value in decisions),
        "card_only": sum("card_only" in value or "promo_nonzero" in value for value in decisions),
        "approve_blocked": sum("approve" in value and "ready" not in value for value in decisions),
        "link_ready": sum(bool(row.get("ok") and row.get("url")) for row in results),
        "qr_ready": sum(_is_qr_ready(row) for row in results),
        "terminal": sum(bool((row.get("auth") or {}).get("terminal")) for row in results),
        "failed": sum(not bool(row.get("ok")) for row in results),
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


def _cell_payment_kwargs(base: dict[str, Any], cell: dict[str, Any], proxy: Any) -> dict[str, Any]:
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
    values["stage_proxy_countries"] = countries
    for key in ("strategy", "checkout_country", "target_country"):
        if cell.get(key) not in (None, ""):
            values[key] = cell[key]
    seed = str(proxy or values.get("checkout_proxy") or "").strip()
    if seed and countries:
        from .paypal_proxy import retarget_proxy_country, rotate_proxy_session
        chain_seed = rotate_proxy_session(seed, countries.get("checkout") or "")
        for stage in ("checkout", "promotion", "provider", "approve", "redirect"):
            country = countries.get(stage)
            if country and not str(values.get(f"{stage}_proxy") or "").strip():
                values[f"{stage}_proxy"] = retarget_proxy_country(chain_seed, country)
    return values


def _eligible_from_result(method: str, result: dict[str, Any]) -> bool | None:
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
        and (row.get("qr_path") or "payment.momo.vn" in str(row.get("url") or "").lower())
    )


def _is_transient(result: dict[str, Any]) -> bool:
    text = json.dumps(result or {}, ensure_ascii=False).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _public_payment_result(result: dict[str, Any]) -> dict[str, Any]:
    blocked = {"access_token", "auth_context", "raw_output", "raw_output_tail", "state_history"}
    return {key: value for key, value in dict(result or {}).items() if key not in blocked and "token" not in key.lower()}


def _sanitize_report_value(value: Any, key: str = "") -> Any:
    blocked = {"email", "access_token", "refresh_token", "id_token", "auth_context", "password"}
    lowered = key.lower()
    token_metadata = {"token_telemetry", "token_hash", "token_changed"}
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
    if isinstance(value, str):
        return re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", value)
    return value


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


def _report_path(batch_id: str) -> Path:
    return runtime_file(CFG, "payment_batches") / f"{batch_id}.json"


def _load_checkpoint(path: Path, method: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get("payment_method") != method:
        return {}
    return value


def _write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
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
    attempted = [row for row in rows if row.get("attempted")]
    completed = int((report.get("counts") or {}).get("completed") or 0)
    conclusive_offer = {
        "account_trial_ineligible", "card_only_full_price", "promo_nonzero", "momo_not_enabled",
        "nonzero_offer", "wrong_currency", "kakao_not_enabled", "credential_invalid", "account_deactivated",
    }
    decisions = {str(row.get("decision") or "") for row in attempted}
    systemic = bool(attempted and not completed and decisions and not decisions.issubset(conclusive_offer))
    state = {
        "payment_method": method,
        "paused": systemic,
        "reason": "protocol_profile_canary_failed" if systemic else "",
        "attempted": len(attempted),
        "completed": completed,
        "decisions": sorted(decisions),
        "updated_at": int(time.time()),
    }
    path = _canary_state_path()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
