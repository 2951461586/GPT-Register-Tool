"""Account recovery state, deactivation classification, and relogin cooldown guard.

Extracted from :mod:`sms_tool.accounts.account_recovery` so local deactivation
checks, token-revoked/permanent persistence, failure fingerprinting, and
cooldown guard state live apart from the network relogin workflows.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..transport.http_client import is_transient_transport_error
from .account_terminal import text_has_account_deactivated

_TRANSIENT_RELOGIN_MODES = frozenset({"cooldown", "concurrency_limited"})
_RELOGIN_STATUS_RE = re.compile(r'"status"\s*:\s*(\d{3})')
_RELOGIN_CODE_RE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_\-]{1,40})"')

_PRE_OTP_RETRY_MARKERS = (
    "session_circuit_open",
    "missing_csrf_token",
    "existing_login_continue_failed:429",
    "existing_login_continue_failed:403",
    "existing_login_continue_failed:502",
    "existing_login_continue_failed:503",
    "existing_login_continue_failed:504",
    "edge_challenge",
    "connect tunnel failed",
)

#: Attempt errors that prove the email-OTP strategy failed *before* it asked for a code.
_NO_CODE_SPENT_MARKERS = (
    "existing_login_password_verify_failed",
    "existing_login_password_required",
    "existing_login_password_step_unknown",
)

#: Markers that mean a verification code / mailbox was actually involved.
_CODE_PHASE_MARKERS = ("otp", "email_code", "verification_code", "mailbox")


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _looks_account_deactivated(value: Any) -> bool:
    return text_has_account_deactivated(json.dumps(value or {}, ensure_ascii=False))


def _item_is_account_deactivated(value: Any) -> bool:
    if not isinstance(value, dict):
        return _looks_account_deactivated(value)
    return _looks_account_deactivated(value.get("probe")) or _looks_account_deactivated(value.get("relogin"))


def _is_pre_otp_proxy_retryable(result: Any) -> bool:
    """True when ``chatgpt_email_otp`` failed before spending an email code."""
    if not isinstance(result, dict) or result.get("ok"):
        return False
    if result.get("terminal") or _looks_account_deactivated(result):
        return False
    if str(result.get("error") or "") == "mailbox_auth_invalid":
        return False
    if _relogin_spent_email_code(result):
        return False
    error = str(result.get("error") or "").lower()
    if any(marker in error for marker in _PRE_OTP_RETRY_MARKERS):
        return True
    return is_transient_transport_error(error)


def mark_successful_relogin(
    data: dict[str, Any],
    probe: dict[str, Any],
    *,
    safe_result_fn: Callable[[dict[str, Any] | None], dict[str, Any]],
    promotion_auth_failure_fn: Callable[[Any], bool],
    now: int | None = None,
) -> None:
    """Replace stale 401 metadata after a newly acquired AT passes HTTP 200."""
    timestamp = int(now or time.time())
    data["success"] = True
    if str(data.get("status") or "").strip().lower() in {
        "at_invalid",
        "access_token_invalid",
        "token_invalidated",
    }:
        data["status"] = "registered"
    data.pop("error", None)
    if promotion_auth_failure_fn(data):
        data["promotion_status"] = ""
        data.pop("promotion_state", None)
        _promo_raw: object = data.get("promotion")
        if isinstance(_promo_raw, dict):
            _promo_raw.pop("state", None)
            data["promotion"] = _promo_raw
    _raw_promotion: object = data.get("promotion")
    promotion: dict = _raw_promotion if isinstance(_raw_promotion, dict) else {}
    if promotion and promotion_auth_failure_fn({**data, "promotion_status": promotion.get("status")}):
        promotion["status"] = ""
        data["promotion"] = promotion
    _scan_raw: object = data.get("account_scan")
    account_scan: dict[str, Any] = _scan_raw if isinstance(_scan_raw, dict) else {}
    account_scan.update(
        {
            "ok": True,
            "scan_status": "alive",
            "token_probe": safe_result_fn(probe),
        }
    )
    data["account_scan"] = account_scan
    data["account_scan_status"] = "alive"
    data["account_scan_updated_at"] = timestamp
    _quota_raw: object = data.get("quota")
    quota: dict[str, Any] = _quota_raw if isinstance(_quota_raw, dict) else {}
    quota_status = str(probe.get("quota_status") or "").strip()
    if not quota_status or quota_status in {"401失效", "token_invalid", "HTTP 401"}:
        quota_status = "可用"
    quota["status"] = quota_status
    quota["updated_at"] = timestamp
    quota["last_result"] = {
        key: value
        for key, value in safe_result_fn(probe).items()
        if key not in {"body", "access_token", "authorization", "cookie", "cookie_header"}
    }
    data["quota"] = quota
    data["quota_status"] = quota_status
    data["quota_updated_at"] = timestamp


def recovery_allow_direct(cfg: Any) -> bool:
    """Whether the recovery chain may fall back to a direct (proxy-less) exit."""
    _proxy_raw: object = cfg.get("proxy")
    proxy_cfg: dict[str, Any] = _proxy_raw if isinstance(_proxy_raw, dict) else {}
    return bool(proxy_cfg.get("recovery_allow_direct"))


def recovery_proxy_candidates(
    affinity_proxy: str | None,
    selected_proxy: str | None,
    *,
    allow_direct: bool,
) -> list[str | None]:
    """Exits to try for one strategy, best first."""
    ordered: list[str | None] = []
    for value in (selected_proxy, affinity_proxy):
        candidate = str(value or "").strip()
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    if not ordered:
        ordered.append(None)
    elif allow_direct:
        ordered.append(None)
    return ordered


def _recoverable_on_other_proxy(result: Any) -> bool:
    """True when the same strategy deserves a retry through a different exit."""
    if not isinstance(result, dict) or result.get("ok"):
        return False
    if result.get("terminal") or _looks_account_deactivated(result):
        return False
    last_status = str(result.get("last_status") or "").strip()
    if last_status:
        return last_status != "200"
    return is_transient_transport_error(result.get("error") or "")


def is_permanently_deactivated(account: dict[str, Any]) -> bool:
    if not isinstance(account, dict):
        return False
    values = [account.get("status"), account.get("error"), account.get("account_scan_status")]
    terminal = account.get("terminal_failure")
    if isinstance(terminal, dict):
        values.extend((terminal.get("code"), terminal.get("reason")))
    raw_json = str(account.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                values.extend((parsed.get("status"), parsed.get("error"), parsed.get("account_scan_status")))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return _looks_account_deactivated(values)


def _local_account_data(record: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw_json = str((record or {}).get("raw_json") or "")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data.update(parsed)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    for key, value in (record or {}).items():
        if value not in (None, ""):
            data[key] = value
    return data


def local_quota_accounts(
    emails: list[str] | None,
    *,
    list_accounts: Callable[[], list[dict[str, Any]]],
    get_record: Callable[[str], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    requested = [_normalize_email(email) for email in (emails or []) if _normalize_email(email)]
    if not requested:
        requested = [
            _normalize_email(row.get("email")) for row in list_accounts() if _normalize_email(row.get("email"))
        ]
    accounts = []
    seen = set()
    for email in requested:
        if email in seen:
            continue
        seen.add(email)
        record = get_record(email)
        accounts.append(_local_account_data(record) if record else {"email": email})
    return accounts


def _has_relogin_material(account: dict[str, Any]) -> bool:
    """Whether any relogin strategy has a credential to work with."""
    if not isinstance(account, dict):
        return False
    for key in ("refresh_token", "oauth_refresh_token", "mailbox_token", "mailbox_provider"):
        if str(account.get(key) or "").strip():
            return True
    mailbox = account.get("mailbox")
    if isinstance(mailbox, dict):
        for key in ("token", "refresh_token", "provider"):
            if str(mailbox.get(key) or "").strip():
                return True
    return False


def _is_token_revoked_drop(account: dict[str, Any]) -> bool:
    if not isinstance(account, dict):
        return False
    candidates = [account.get("terminal_failure")]
    raw_json = str(account.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                candidates.append(parsed.get("terminal_failure"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    for terminal in candidates:
        if isinstance(terminal, dict) and str(terminal.get("code") or "").strip() == "token_revoked":
            return True
    return False


def persist_token_revoked_drop(
    account: dict[str, Any],
    *,
    upsert: Callable[..., bool],
    write_text: Callable[[str, str], Any],
) -> bool:
    """Persist 掉号: AT revoked with zero recovery material."""
    data = _local_account_data(account)
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return False
    now = int(time.time())
    data.update(
        {
            "email": email,
            "status": "at_invalid",
            "error": "token_revoked_unrecoverable",
            "terminal_failure": {
                "code": "token_revoked",
                "reason": "token_invalid_no_relogin_material",
                "updated_at": now,
            },
        }
    )
    json_path = str(data.get("json_path") or account.get("json_path") or "").strip()
    if json_path:
        try:
            write_text(json_path, json.dumps(data, ensure_ascii=False, indent=2))
        except (OSError, TypeError, ValueError):
            pass
    return upsert(data, json_path=json_path)


def persist_permanent_deactivation(
    account: dict[str, Any],
    result: dict[str, Any] | None = None,
    *,
    upsert: Callable[..., bool],
    write_text: Callable[[str, str], Any],
) -> bool:
    del result
    data = _local_account_data(account)
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return False
    now = int(time.time())
    data.update(
        {
            "email": email,
            "success": False,
            "status": "account_deactivated",
            "error": "account_deactivated",
            "account_scan_status": "account_deactivated",
            "terminal_failure": {
                "code": "account_deactivated",
                "reason": "account_deactivated",
                "updated_at": now,
            },
        }
    )
    json_path = str(data.get("json_path") or account.get("json_path") or "").strip()
    if json_path:
        try:
            write_text(json_path, json.dumps(data, ensure_ascii=False, indent=2))
        except (OSError, TypeError, ValueError):
            pass
    return upsert(data, json_path=json_path)


def timed_out_health_result(
    email: str,
    reason: str,
    *,
    mark_quota: Callable[..., bool],
) -> dict[str, Any]:
    """Return a stable result for an account skipped by a health deadline."""
    probe = {
        "ok": False,
        "mode": "local",
        "status": "timeout",
        "quota_status": "health_timeout",
        "error": str(reason or "health_timeout"),
    }
    persisted = mark_quota(email, probe["quota_status"], quota_result=probe) if email else False
    return {
        "ok": False,
        "email": email,
        "quota_status": probe["quota_status"],
        "probe": probe,
        "probe_ok": False,
        "persisted": bool(persisted),
        "health_status": "batch_timeout" if reason == "batch_timeout" else "account_timeout",
        "timed_out": True,
        "timeout_reason": str(reason or "health_timeout"),
        "liveness_401": False,
        "relogin_attempted": False,
        "mailbox_auth_invalid": False,
    }


def relogin_cooldown_seconds(cfg: Any) -> int:
    _health_raw: object = cfg.get("account_health")
    health: dict[str, Any] = _health_raw if isinstance(_health_raw, dict) else {}
    try:
        return max(60, int(health.get("relogin_cooldown_seconds") or 1800))
    except (TypeError, ValueError):
        return 1800


def relogin_dead_end_permanent(cfg: Any) -> bool:
    _health_raw: object = cfg.get("account_health")
    health: dict[str, Any] = _health_raw if isinstance(_health_raw, dict) else {}
    return bool(health.get("relogin_dead_end_permanent", True))


def relogin_repeat_cooldown_seconds(cfg: Any) -> int:
    _health_raw: object = cfg.get("account_health")
    health: dict[str, Any] = _health_raw if isinstance(_health_raw, dict) else {}
    try:
        return max(60, int(health.get("relogin_repeat_cooldown_seconds") or 21600))
    except (TypeError, ValueError):
        return 21600


def relogin_cooldown_active(
    account: dict[str, Any],
    *,
    guard_path: Path,
    cooldown_seconds: int,
) -> bool:
    email = _normalize_email(account.get("email"))
    if not email:
        return False
    _quota_raw: object = account.get("quota")
    quota: dict[str, Any] = _quota_raw if isinstance(_quota_raw, dict) else {}
    status = str(account.get("quota_status") or quota.get("status") or "").strip().lower()
    try:
        updated_at = int(account.get("quota_updated_at") or quota.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    if updated_at and time.time() - updated_at < cooldown_seconds:
        if status in {"relogin_cooldown", "relogin_otp_failed"} or (
            "otp" in status and any(marker in status for marker in ("fail", "失", "ʧ"))
        ):
            return True
    try:
        data = json.loads(guard_path.read_text(encoding="utf-8"))
        entry = data.get(email) or {}
        if entry.get("permanent"):
            return True
        until = float(entry.get("cooldown_until") or 0)
        return until > time.time()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _relogin_dead_end_reason(relogin: Any) -> str:
    """The relogin outcome that cannot succeed however often it is retried."""
    text = json.dumps(relogin or {}, ensure_ascii=False).lower()
    if "user_already_exists" in text:
        return "account_exists_login_loop"
    return ""


def _relogin_attempt_shape(attempt: dict[str, Any]) -> str:
    """``mode`` plus the fields that say *why* -- minus anything per-attempt."""
    mode = str(attempt.get("mode") or "")
    error = str(attempt.get("error") or "")
    head = error.split("{", 1)[0].strip()[:80]
    marks = []
    status = _RELOGIN_STATUS_RE.search(error)
    if status:
        marks.append(f"status={status.group(1)}")
    code = _RELOGIN_CODE_RE.search(error)
    if code:
        marks.append(f"code={code.group(1)}")
    last_status = str(attempt.get("last_status") or "").strip()
    if last_status:
        marks.append(f"last={last_status}")
    return f"{mode}={head}({','.join(marks)})" if marks else f"{mode}={head}"


def _relogin_failure_shape(relogin: Any) -> str:
    """A fingerprint of *why* relogin failed, stable across retries."""
    attempts = relogin.get("attempts") if isinstance(relogin, dict) else None
    parts = []
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict):
                parts.append(_relogin_attempt_shape(attempt))
    if not parts:
        parts.append(_relogin_attempt_shape(relogin if isinstance(relogin, dict) else {}))
    return "|".join(parts)


def clear_relogin_failure(email: str, *, guard_path: Path) -> None:
    """Drop the guard entry once a relogin succeeds."""
    key = _normalize_email(email)
    if not key:
        return
    try:
        data = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.is_file() else {}
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(data, dict) or key not in data:
        return
    data.pop(key, None)
    try:
        temp = guard_path.with_suffix(guard_path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(guard_path)
    except OSError:
        return


def record_relogin_failure(
    email: str,
    relogin: dict[str, Any],
    *,
    guard_path: Path,
    cooldown_seconds: int,
    repeat_cooldown_seconds: int,
    dead_end_permanent: bool,
) -> None:
    if not email or str(relogin.get("mode") or "").lower() == "cooldown":
        return
    text = json.dumps(relogin, ensure_ascii=False).lower()
    dead_end = _relogin_dead_end_reason(relogin)
    if not dead_end and not any(marker in text for marker in ("otp", "mailbox", "email")):
        return
    try:
        data = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError, TypeError):
        data = {}
    key = _normalize_email(email)
    _previous_raw: object = data.get(key)
    previous: dict[str, Any] = _previous_raw if isinstance(_previous_raw, dict) else {}
    shape = _relogin_failure_shape(relogin)
    repeated = bool(shape) and previous.get("last_shape") == shape
    cooldown = repeat_cooldown_seconds if repeated else cooldown_seconds
    entry: dict[str, Any] = {
        "failure_class": "account"
        if dead_end
        else ("relogin_otp_failed" if _relogin_spent_email_code(relogin) else "relogin_failed"),
        "last_error": str(relogin.get("error") or "relogin_failed")[:200],
        "cooldown_until": int(time.time() + cooldown),
        "updated_at": int(time.time()),
        "last_shape": shape,
    }
    if repeated:
        entry["repeat_failure"] = True
    if dead_end:
        entry["dead_end"] = dead_end
        if dead_end_permanent:
            entry["permanent"] = True
    data[key] = entry
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    temp = guard_path.with_suffix(guard_path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    temp.replace(guard_path)


def _relogin_spent_email_code(relogin: Any) -> bool:
    """True only when a relogin attempt actually reached the email-code phase."""
    if not isinstance(relogin, dict):
        return False
    attempts = relogin.get("attempts")
    errors: list[str] = []
    if isinstance(attempts, list) and attempts:
        for attempt in attempts:
            if isinstance(attempt, dict):
                errors.append(str(attempt.get("error") or "").lower())
    if not errors:
        errors = [str(relogin.get("error") or "").lower()]
    for error in errors:
        if not error:
            continue
        if any(marker in error for marker in _NO_CODE_SPENT_MARKERS):
            continue
        if any(marker in error for marker in _CODE_PHASE_MARKERS):
            return True
    return False


def health_status_code(
    probe: dict[str, Any],
    relogin: dict[str, Any],
    *,
    probe_is_token_invalid: Callable[[dict[str, Any]], bool],
) -> str:
    if _item_is_account_deactivated({"probe": probe, "relogin": relogin}):
        return "account_deactivated"
    if relogin and not relogin.get("ok"):
        if str(relogin.get("mode") or "") == "cooldown":
            return "relogin_cooldown"
        if str(relogin.get("error") or "") == "mailbox_pool_repair_required":
            return "mailbox_relogin_blocked"
        if str(relogin.get("error") or "") == "mailbox_auth_invalid":
            return "mailbox_auth_invalid"
        if str(relogin.get("error") or "") == "relogin_concurrency_limited":
            return "relogin_concurrency_limited"
        text = json.dumps(relogin, ensure_ascii=False).lower()
        if "mailbox_transport" in text or "mailbox_transport_unavailable" in text:
            return "relogin_mailbox_transport_failed"
        if _relogin_spent_email_code(relogin):
            return "relogin_otp_failed"
        return "relogin_failed"
    if probe_is_token_invalid(probe):
        return "token_invalid"
    if probe.get("status") == "timeout":
        return "probe_timeout"
    if probe.get("ok"):
        return "active"
    return "probe_failed"


def _normalize_relogin_mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"web", "web_session", "session", "chatgpt_session"}:
        return "web_session"
    if text in {"email_otp", "chatgpt_email_otp", "passwordless", "passwordless_email"}:
        return "chatgpt_email_otp"
    if text in {"codex", "codex_oauth", "oauth", "pkce"}:
        return "codex_oauth"
    if text in {"browser", "browser_session", "camoufox"}:
        return "browser_session"
    return "auto"
