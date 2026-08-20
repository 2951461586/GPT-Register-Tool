"""Read-only desktop data contract.

The WPF client consumes this boundary instead of opening SQLite. Secret-bearing
mailbox material is written to a local temporary file and only its path crosses
IPC; normal account reads contain the token-free session snapshot.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .config import ConfigInput, RuntimeConfig, resolve_runtime_config
from .mailbox_parsers import parse_mailbox_pool_line
from .paths import PROJECT_ROOT
from .sanitizer import sanitize
from .storage import _account_type, _looks_codex_refresh_token, get_account_record_by_id, list_account_records


_PUBLIC_COLUMNS = (
    "id", "email", "success", "status", "error", "device_id", "source",
    "register_method", "session_type", "plan_type", "paypal_ok",
    "payment_method", "paypal_status", "paypal_updated_at", "refresh_token_status",
    "refresh_token_updated_at", "workspace_status", "workspace_id", "workspace_name",
    "workspace_switch_result", "workspace_updated_at", "account_type",
    "batch_id", "registration_state", "registration_country", "twofa_enrolled_at",
    "twofa_enroll_error", "auth_session_logging_id", "device_id_generated_at",
    "mailbox_provider", "mailbox_source", "purchase_id", "project_name", "price",
    "purchase_total_cost", "balance_after", "json_path", "timing_total_seconds",
    "pipeline_total_seconds", "created_at", "updated_at",
)


def _nested_value(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _access_token_probe_status_code(data: dict[str, Any]) -> str:
    # Both account scans and quota refreshes persist probe results. Pick the
    # newest result so a successful relogin (quota HTTP 200) is not hidden by
    # an older account-scan HTTP 401.
    sources = (
        (("account_scan", "token_probe"), ("account_scan_updated_at",), ("account_scan", "updated_at"), 4),
        (("token_probe",), ("token_probe", "updated_at"), (), 3),
        (("scan", "token_probe"), ("scan_updated_at",), ("scan", "updated_at"), 2),
        (("quota", "last_result"), ("quota_updated_at",), ("quota", "updated_at"), 1),
    )
    candidates: list[tuple[float, int, str]] = []
    for probe_path, primary_time_path, fallback_time_path, priority in sources:
        probe = _nested_value(data, *probe_path)
        if not isinstance(probe, dict):
            continue
        code = str(probe.get("status_code") or "").strip()
        if not code and str(probe.get("status") or "").strip().lower() == "token_invalid":
            code = "401"
        if not code:
            continue
        timestamp = _numeric_timestamp(_nested_value(data, *primary_time_path))
        if not timestamp and fallback_time_path:
            timestamp = _numeric_timestamp(_nested_value(data, *fallback_time_path))
        candidates.append((timestamp, priority, code))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _numeric_timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    result = {key: record.get(key) for key in _PUBLIC_COLUMNS}
    file_state = _session_file_state(record)
    has_access = bool(str(record.get("access_token") or "").strip()) or file_state["has_access_token"]
    stored_oauth_refresh = str(record.get("oauth_refresh_token") or "").strip()
    stored_legacy_refresh = str(record.get("refresh_token") or "").strip()
    has_refresh = bool(stored_oauth_refresh) or _looks_codex_refresh_token(stored_legacy_refresh) or file_state["has_refresh_token"]
    result.update({
        "has_access_token": has_access,
        "has_refresh_token": has_refresh,
        "has_payment_url": bool(str(record.get("paypal_url") or "").strip()),
        "has_totp": bool(str(record.get("totp_secret") or "").strip()),
        # These names intentionally end in `_present`: the shared redaction
        # policy preserves presence metadata while redacting token-bearing
        # fields such as `has_access_token`.
        "access_token_present": has_access,
        "refresh_token_present": has_refresh,
        "payment_url_present": bool(str(record.get("paypal_url") or "").strip()),
        "totp_present": bool(str(record.get("totp_secret") or "").strip()),
        "at_probe_status_code": file_state["at_probe_status_code"],
    })
    if has_refresh and str(result.get("refresh_token_status") or "").strip().lower() in {"", "no_rt"}:
        result["refresh_token_status"] = "oauth_present"
    if file_state["account_type"]:
        result["account_type"] = file_state["account_type"]
    raw_json = record.get("raw_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            session = _parsed_session(raw_json)
            result["at_probe_status_code"] = (
                result["at_probe_status_code"]
                or _access_token_probe_status_code(session)
            )
            # 优惠状态 (plan/promotion) lives in raw_json, not a dedicated column.
            promotion_status = str(session.get("promotion_status") or "").strip()
            if not promotion_status and isinstance(session.get("promotion"), dict):
                promotion_status = str(session["promotion"].get("status") or "").strip()
            # A promotion probe that recorded "AT失效" predates a later verified
            # relogin (quota/account-scan token probe HTTP 200). The stale auth
            # failure must not surface in the 优惠状态 column anymore.
            if promotion_status == "AT失效" and str(result.get("at_probe_status_code") or "").strip() == "200":
                promotion_status = ""
            if promotion_status:
                result["promotion_status"] = promotion_status
            public_session = _sanitized_session(raw_json, session)
            if isinstance(public_session, dict):
                public_session.pop("quota", None)
                public_session.pop("quota_status", None)
                public_session.pop("quota_updated_at", None)
                public_session.pop("wham_usage", None)
            result["session"] = public_session
        except (TypeError, ValueError):
            result["session"] = {}
    else:
        result["session"] = {}
    return result


# raw_json strings repeat verbatim between refreshes; sanitize is deterministic
# and dominates the per-row cost, so cache parse + sanitize on the exact text.
_SESSION_PARSE_CACHE: dict[str, Any] = {}
_SESSION_SANITIZE_CACHE: dict[str, dict[str, Any]] = {}


def _parsed_session(raw_json: str) -> Any:
    cached = _SESSION_PARSE_CACHE.get(raw_json)
    if cached is None:
        cached = json.loads(raw_json)
        if len(_SESSION_PARSE_CACHE) > 4096:
            _SESSION_PARSE_CACHE.clear()
        _SESSION_PARSE_CACHE[raw_json] = cached
    return cached


def _sanitized_session(raw_json: str, session: Any) -> Any:
    cached = _SESSION_SANITIZE_CACHE.get(raw_json)
    if cached is None:
        cached = sanitize(session)
        if len(_SESSION_SANITIZE_CACHE) > 4096:
            _SESSION_SANITIZE_CACHE.clear()
        _SESSION_SANITIZE_CACHE[raw_json] = cached
    return dict(cached) if isinstance(cached, dict) else cached


def _empty_session_state() -> dict[str, Any]:
    return {
        "has_access_token": False,
        "has_refresh_token": False,
        "account_type": "",
        "at_probe_status_code": "",
    }


# (mtime, size) keyed cache for parsed session-file states. A several-hundred
# account pool refresh reparses every session JSON on each call; unchanged
# files hit the cache, which the resident desktop channel relies on.
_SESSION_STATE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _session_file_state(record: dict[str, Any]) -> dict[str, Any]:
    """Recover non-secret presence metadata when SQLite lags the session file."""
    path_text = str(record.get("json_path") or "").strip()
    path = Path(path_text)
    if not path_text or not path.is_file():
        return _empty_session_state()
    try:
        stat = path.stat()
        cached = _SESSION_STATE_CACHE.get(str(path))
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return dict(cached[2])
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError):
        return _empty_session_state()
    if not isinstance(data, dict):
        return _empty_session_state()
    state = _empty_session_state()
    state["has_access_token"] = bool(str(data.get("access_token") or "").strip())
    oauth_refresh = str(data.get("oauth_refresh_token") or "").strip()
    legacy_refresh = str(data.get("refresh_token") or "").strip()
    state["has_refresh_token"] = bool(oauth_refresh and _looks_codex_refresh_token(oauth_refresh)) or _looks_codex_refresh_token(legacy_refresh)
    state["at_probe_status_code"] = _access_token_probe_status_code(data)
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    workspace = data.get("workspace_scan") if isinstance(data.get("workspace_scan"), dict) else {}
    state["account_type"] = _account_type(data, auth_session, workspace, data.get("access_token"))
    _SESSION_STATE_CACHE[str(path)] = (stat.st_mtime, stat.st_size, state)
    return dict(state)


def read_accounts(runtime_config: ConfigInput = None) -> list[dict[str, Any]]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    return [_record_payload(row) for row in list_account_records(runtime_config=config)]


def read_account(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    row = get_account_record_by_id(account_id, runtime_config=config) if str(account_id or "").strip() else {}
    if not row and email:
        from .storage import get_account_record
        row = get_account_record(email, runtime_config=config)
    return _record_payload(row) if row else {}


_MAILBOX_POOL_CANDIDATE_NAMES = ("hotmail.txt", "chatai_mailbox.txt", "chatai.txt")
_MAILBOX_POOL_GLOB = "*chatai*.txt"


def read_mailbox_pool(
    runtime_config: ConfigInput = None,
    extra_files: tuple[str, ...] = (),
    root_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Enumerate the known mailbox-pool files and parse every line.

    Owns the pool-file storage layout (config token file plus the well-known
    chatai/hotmail candidates under the project root). Missing files are
    tolerated and malformed lines are skipped by the canonical line parser.
    """
    config = resolve_runtime_config(runtime_config, workflow="mailbox")
    root = Path(root_dir) if root_dir is not None else PROJECT_ROOT
    files = []
    for path in _known_mailbox_pool_files(config, extra_files, root):
        files.append({
            "path": str(path),
            "name": path.name,
            "lines": _read_pool_lines(path),
        })
    return {"files": files}


def _known_mailbox_pool_files(config: RuntimeConfig, extra_files, root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    def add(candidate) -> None:
        raw = str(candidate or "").strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        if path.suffix.lower() != ".txt" or not path.is_file():
            return
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            paths.append(path)

    for candidate in extra_files or ():
        add(candidate)
    token_file = str((config.data.get("email_registration") or {}).get("token_file") or "").strip()
    add(token_file or "mailbox_tokens.txt")
    for name in _MAILBOX_POOL_CANDIDATE_NAMES:
        add(root / name)
    if root.is_dir():
        for path in sorted(root.glob(_MAILBOX_POOL_GLOB)):
            add(path)
    return paths


def _read_pool_lines(path: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return lines
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        account = parse_mailbox_pool_line(line, path, line_no)
        if account is None:
            continue
        lines.append({"line_no": line_no, **_pool_line_payload(account, line)})
    return lines


def _pool_line_payload(account, raw_line: str) -> dict[str, Any]:
    provider = str(account.provider or "").strip().lower()
    # chatai/chongzhi and Gmail-OAuth lines store the OAuth client id in the
    # generic token slot; surface it explicitly so callers need no format
    # knowledge.
    client_id = account.token if provider in {"chatai", "chongzhi"} or (
        provider == "gmail" and account.auth_mode == "oauth_refresh"
    ) else ""
    return {
        "raw_line": raw_line,
        "email": account.email,
        "provider": provider,
        "token": account.token,
        "client_id": client_id,
        "password": account.password,
        "login_password": account.login_password,
        "refresh_token": account.refresh_token,
        "access_token": account.access_token,
        "client_secret": account.client_secret,
        "auth_mode": account.auth_mode,
        "order_no": account.order_no,
        "purchase_id": account.purchase_id,
        "sender_name": account.sender_name,
    }


def create_mailbox_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    data = _full_account_payload(row)
    mailbox = data.get("mailbox") if isinstance(data, dict) else {}
    if not isinstance(mailbox, dict):
        mailbox = {}
    line = _mailbox_line(mailbox)
    if not line:
        return {"ok": False, "error": "mailbox_credentials_missing"}
    target = _write_temp_text("smsworkbench_mailbox_", line + "\n", suffix=".txt")
    return {"ok": True, "path": str(target), "provider": str(mailbox.get("provider") or "")}


def create_account_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    target = _write_temp_text(
        "smsworkbench_account_",
        json.dumps(_full_account_payload(row), ensure_ascii=False, separators=(",", ":")),
        suffix=".json",
    )
    return {"ok": True, "path": str(target)}


def create_payment_url_file(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    row = _find_record(account_id, email, runtime_config)
    if not row:
        return {"ok": False, "error": "account_not_found"}
    url = str(row.get("paypal_url") or "").strip()
    if not url:
        return {"ok": False, "error": "payment_url_missing"}
    target = _write_temp_text("smsworkbench_payment_url_", url + "\n", suffix=".txt")
    return {"ok": True, "path": str(target)}


def _find_record(account_id: str, email: str, runtime_config: ConfigInput) -> dict[str, Any]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    row = get_account_record_by_id(account_id, runtime_config=config) if str(account_id or "").strip() else {}
    if not row and email:
        from .storage import get_account_record
        row = get_account_record(email, runtime_config=config)
    return row


def _full_account_payload(row: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw_json = str(row.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data.update(parsed)
        except (TypeError, ValueError):
            pass

    json_path = Path(str(row.get("json_path") or "").strip())
    if json_path.is_file():
        try:
            parsed = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                data.update(parsed)
        except (OSError, TypeError, ValueError):
            pass

    for key in (
        "email", "password", "success", "status", "error", "session_token", "access_token",
        "refresh_token", "oauth_refresh_token", "cookie_header", "device_id", "totp_secret",
        "auth_session_logging_id", "registration_country", "refresh_token_status", "payment_method",
    ):
        value = row.get(key)
        if value not in (None, ""):
            data[key] = value

    mailbox = data.get("mailbox") if isinstance(data.get("mailbox"), dict) else {}
    mailbox = dict(mailbox)
    mailbox_defaults = {
        "email": row.get("email"),
        "provider": row.get("mailbox_provider"),
        "source": row.get("mailbox_source"),
        "token": row.get("mailbox_token"),
        "purchase_id": row.get("purchase_id"),
        "project_name": row.get("project_name"),
        "price": row.get("price"),
        "purchase_total_cost": row.get("purchase_total_cost"),
        "balance_after": row.get("balance_after"),
    }
    for key, value in mailbox_defaults.items():
        if value not in (None, "") and not mailbox.get(key):
            mailbox[key] = value
    data["mailbox"] = mailbox

    payment = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    payment = dict(payment)
    for key, column in (
        ("ok", "paypal_ok"), ("url", "paypal_url"), ("status", "paypal_status"),
        ("cs_id", "paypal_cs_id"), ("pm_id", "paypal_pm_id"),
        ("currency", "paypal_currency"), ("amount_due", "paypal_amount_due"),
        ("has_paypal", "paypal_has_paypal"),
    ):
        value = row.get(column)
        if value not in (None, "") and not payment.get(key):
            payment[key] = value
    data["paypal"] = payment
    return data


def _write_temp_text(prefix: str, content: str, *, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=prefix, suffix=suffix, delete=False
    )
    with handle:
        handle.write(content)
    return Path(handle.name)


def _smailr_token_from_source(source: Any) -> str:
    """Recover the smailr mailbox id from the stored create/list response.

    Reused-mailbox snapshots (reuse_reason=domain_level_restricted) carried the
    mailbox id only inside ``source``; ``token`` stayed empty, which made
    create_mailbox_file report mailbox_credentials_missing.
    """
    if isinstance(source, dict):
        candidate = source
    elif isinstance(source, str) and source.strip():
        try:
            parsed = json.loads(source)
        except (TypeError, ValueError):
            return ""
        candidate = parsed if isinstance(parsed, dict) else {}
    else:
        return ""
    nested = candidate.get("data")
    if isinstance(nested, dict):
        candidate = nested
    value = str(candidate.get("id") or candidate.get("mailbox_id") or "").strip()
    # Mailbox ids are UUID-ish; guard against accidentally reusing e.g. user ids.
    if value and "@" not in value:
        return value
    return ""


def _mailbox_line(mailbox: dict[str, Any]) -> str:
    email = str(mailbox.get("email") or "").strip()
    provider = str(mailbox.get("provider") or "").strip().lower()
    if not email:
        return ""
    if provider == "cfworker":
        return f"cfworker://{email}"
    if provider == "smailr":
        token = str(mailbox.get("token") or "").strip()
        if not token:
            token = _smailr_token_from_source(mailbox.get("source"))
        return f"smailr://{email}---{token}" if token else ""
    if provider == "remail":
        token = str(mailbox.get("token") or "").strip()
        order = str(mailbox.get("order_no") or "").strip()
        purchase = str(mailbox.get("purchase_id") or "").strip()
        # Keep the canonical ReMail line format used by mailbox_parsers.
        return "remail://" + "---".join(filter(None, (email, token, order, purchase)))
    if provider == "gmail":
        client = str(mailbox.get("client_id") or mailbox.get("token") or "").strip()
        secret = str(mailbox.get("client_secret") or "").strip()
        refresh = str(mailbox.get("refresh_token") or "").strip()
        if client and secret and refresh:
            return f"gmail://{email}----{client}----{secret}----{refresh}"
        password = str(mailbox.get("login_password") or mailbox.get("password") or "").strip()
        return f"gmail://{email}---{password}" if password else ""
    password = str(mailbox.get("password") or "").strip()
    refresh = str(mailbox.get("refresh_token") or "").strip()
    access = str(mailbox.get("access_token") or "").strip()
    client = str(mailbox.get("client_id") or mailbox.get("clientId") or mailbox.get("token") or "").strip()
    if client and refresh:
        return f"{email}----{password}----{client}----{refresh}"
    if refresh:
        return f"{email}---{password}---{refresh}---{access}---0"
    token = str(mailbox.get("token") or "").strip()
    return f"{email}----{token}" if token else ""
