"""Read-only desktop data contract.

The WPF client consumes this boundary instead of opening SQLite. Secret-bearing
mailbox material is written to a local temporary file and only its path crosses
IPC; normal account reads contain the token-free session snapshot.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .config import ConfigInput, RuntimeConfig, resolve_runtime_config
from .mailbox.parsers import parse_mailbox_pool_line
from .kernel.paths import PROJECT_ROOT, config_dir, resolve_config_file
from .promotion_states import promotion_marker_is_stale
from .registration.retry_guard import RegistrationRetryGuard, mailbox_registration_status
from .kernel.sanitizer import sanitize
from .storage import (
    _account_type,
    _looks_codex_refresh_token,
    get_account_record_by_id,
    get_account_records,
    list_account_records,
)


_PUBLIC_COLUMNS = (
    "id",
    "email",
    "success",
    "status",
    "error",
    "device_id",
    "source",
    "register_method",
    "session_type",
    "plan_type",
    "paypal_ok",
    "payment_method",
    "paypal_status",
    "paypal_updated_at",
    "refresh_token_status",
    "refresh_token_updated_at",
    "workspace_status",
    "workspace_id",
    "workspace_name",
    "workspace_switch_result",
    "workspace_updated_at",
    "account_type",
    "batch_id",
    "registration_state",
    "registration_country",
    "twofa_enrolled_at",
    "twofa_enroll_error",
    "auth_session_logging_id",
    "device_id_generated_at",
    "mailbox_provider",
    "mailbox_source",
    "purchase_id",
    "project_name",
    "price",
    "purchase_total_cost",
    "balance_after",
    "json_path",
    "timing_total_seconds",
    "pipeline_total_seconds",
    "created_at",
    "updated_at",
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _imported_status(session: Any) -> str:
    if not isinstance(session, dict):
        return ""
    imported = []
    for key, label in (("cpa_import", "CPA"), ("sub2api_import", "SUB2")):
        value = session.get(key)
        if isinstance(value, dict) and _truthy(value.get("ok")):
            imported.append(label)
    return "已导入" + "/".join(imported) if imported else ""


def _paypal_amount(session: Any) -> str:
    if not isinstance(session, dict) or not isinstance(session.get("paypal"), dict):
        return ""
    paypal = session["paypal"]
    currency = str(paypal.get("currency") or "").strip().upper()
    raw_amount = next(
        (
            str(paypal.get(key) or "").strip()
            for key in ("amount_due", "due", "expected_amount")
            if str(paypal.get(key) or "").strip()
        ),
        "",
    )
    if not raw_amount:
        return ""
    try:
        from decimal import Decimal, InvalidOperation

        display = Decimal(raw_amount) / Decimal("100")
        text = f"{display:.2f}"
    except (InvalidOperation, ValueError):
        text = raw_amount
    return f"{text} {currency}".strip()


def _record_payload(record: dict[str, Any], *, include_session: bool = True) -> dict[str, Any]:
    result = {key: record.get(key) for key in _PUBLIC_COLUMNS}
    file_state = _session_file_state(record)
    has_access = bool(str(record.get("access_token") or "").strip()) or file_state["has_access_token"]
    stored_oauth_refresh = str(record.get("oauth_refresh_token") or "").strip()
    stored_legacy_refresh = str(record.get("refresh_token") or "").strip()
    has_refresh = (
        bool(stored_oauth_refresh)
        or _looks_codex_refresh_token(stored_legacy_refresh)
        or file_state["has_refresh_token"]
    )
    result.update(
        {
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
        }
    )
    if has_refresh and str(result.get("refresh_token_status") or "").strip().lower() in {"", "no_rt"}:
        result["refresh_token_status"] = "oauth_present"
    if file_state["account_type"]:
        result["account_type"] = file_state["account_type"]
    raw_json = record.get("raw_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            session = _parsed_session(raw_json)
            result["at_probe_status_code"] = result["at_probe_status_code"] or _access_token_probe_status_code(session)
            promotion_status, promotion_state = _promotion_presentation(session)
            # A promotion probe that recorded an auth failure predates a later
            # verified relogin (quota/account-scan token probe HTTP 200). The
            # stale marker must not surface in the 优惠状态 column anymore.
            if promotion_marker_is_stale(promotion_status, promotion_state, result.get("at_probe_status_code")):
                promotion_status = ""
                promotion_state = ""
            if promotion_status:
                result["promotion_status"] = promotion_status
            if promotion_state:
                result["promotion_state"] = promotion_state
            if promotion_status:
                result["promotion_display"] = promotion_status
            qualification, qualification_state = _payment_qualification_presentation(session)
            if qualification:
                result["payment_qualification_display"] = qualification
            if qualification_state:
                result["payment_qualification_state"] = qualification_state
            result["imported_status"] = _imported_status(session)
            result["paypal_amount"] = _paypal_amount(session)
            if include_session:
                public_session = _sanitized_session(raw_json, session)
                if isinstance(public_session, dict):
                    public_session.pop("quota", None)
                    public_session.pop("quota_status", None)
                    public_session.pop("quota_updated_at", None)
                    public_session.pop("wham_usage", None)
                result["session"] = public_session
        except (TypeError, ValueError):
            if include_session:
                result["session"] = {}
    elif include_session:
        result["session"] = {}
    return result


def _promotion_presentation(session: dict[str, Any]) -> tuple[str, str]:
    """Return promotion-only desktop fields, stripping legacy method suffixes."""
    _raw_promotion: object = session.get("promotion")
    promotion = _raw_promotion if isinstance(_raw_promotion, dict) else {}
    status = str(session.get("promotion_status") or promotion.get("status") or "").strip()
    state = str(session.get("promotion_state") or promotion.get("state") or "").strip()
    status = status.split("｜可支付:", 1)[0].strip()
    return status, state


def _payment_qualification_presentation(session: dict[str, Any]) -> tuple[str, str]:
    """Return the 支付资格 badge and its machine state.

    The persisted shape is ``payment_qualification: {status, state, ...}`` (see
    ``store/markers.mark_payment_qualification_status``); the payload exposes
    the label as ``payment_qualification_display`` so it cannot be confused with
    that session block. A pre-2026-10-26 record has no block at all and yields
    two empty strings, which the grid renders as a blank cell.
    """
    _raw: object = session.get("payment_qualification")
    qualification = _raw if isinstance(_raw, dict) else {}
    return (
        str(qualification.get("status") or "").strip(),
        str(qualification.get("state") or "").strip(),
    )


# raw_json strings repeat verbatim between refreshes; sanitize is deterministic
# and dominates the per-row cost, so cache parse + sanitize on the exact text.
#
# Row caches hold the parsed and sanitized session trees for the resident
# channel. Two properties matter beyond "it is bounded":
#
# * the key is a 16-byte digest, NOT the raw JSON. Keying on the JSON kept a
#   second full copy of every session in the dict (sessions are 5-20 KB, so a
#   full 4096-entry cache pinned 20-80 MB of strings *on top of* the parsed
#   trees), and the hash costs ~1 us against a json.loads;
# * overflow evicts the oldest entry, not the whole dict. Clearing wholesale
#   re-parsed the entire pool on the next refresh -- a latency spike exactly
#   when the pool was largest.
#
# Each cache keeps its own lock: the build happens outside it (sanitize() and
# json.loads() must not serialise the row scan), and `while len > limit` makes
# an interleaved overshoot self-correct instead of relying on the size check.
_SESSION_CACHE_LIMIT = 4096
_MISS = object()
_SESSION_PARSE_CACHE: "OrderedDict[bytes, Any]" = OrderedDict()
_SESSION_PARSE_LOCK = threading.Lock()
_SESSION_SANITIZE_CACHE: "OrderedDict[bytes, dict[str, Any]]" = OrderedDict()
_SESSION_SANITIZE_LOCK = threading.Lock()


def _session_cache_key(raw_json: str) -> bytes:
    return hashlib.blake2b(raw_json.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def _cache_lookup(cache, lock, key):
    with lock:
        try:
            value = cache[key]
        except KeyError:
            return _MISS
        cache.move_to_end(key)
        return value


def _cache_store(cache, lock, key, value) -> None:
    with lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _SESSION_CACHE_LIMIT:
            cache.popitem(last=False)


def _parsed_session(raw_json: str) -> Any:
    key = _session_cache_key(raw_json)
    cached = _cache_lookup(_SESSION_PARSE_CACHE, _SESSION_PARSE_LOCK, key)
    if cached is _MISS:
        # Tolerated locally rather than left to the caller's ``except``: the
        # cache must not be skipped on a malformed session (it would re-parse
        # the same garbage on every refresh), and "no session" is the same
        # outcome the caller already produces. ``raw_json`` comes from SQLite,
        # so a truncated blob is a real (if rare) possibility.
        try:
            cached = json.loads(raw_json)
        except (TypeError, ValueError):
            cached = {}
        _cache_store(_SESSION_PARSE_CACHE, _SESSION_PARSE_LOCK, key, cached)
    return cached


def _sanitized_session(raw_json: str, session: Any) -> Any:
    key = _session_cache_key(raw_json)
    cached = _cache_lookup(_SESSION_SANITIZE_CACHE, _SESSION_SANITIZE_LOCK, key)
    if cached is _MISS:
        cached = sanitize(session)
        _cache_store(_SESSION_SANITIZE_CACHE, _SESSION_SANITIZE_LOCK, key, cached)
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
# files hit the cache, which the resident desktop channel relies on. The bound
# matches the row caches above: the resident channel is long-lived, so a pool
# that grows (or a sessions directory an operator prunes by hand) must not grow
# this dict without limit. Clearing wholesale is the same trade the parse and
# sanitize caches take -- a miss costs one file read.
_SESSION_STATE_CACHE: "OrderedDict[str, tuple[float, int, dict[str, Any]]]" = OrderedDict()
_SESSION_STATE_CACHE_LIMIT = 4096
_SESSION_STATE_LOCK = threading.Lock()


def _session_file_state(record: dict[str, Any]) -> dict[str, Any]:
    """Recover non-secret presence metadata when SQLite lags the session file."""
    path_text = str(record.get("json_path") or "").strip()
    path = Path(path_text)
    if not path_text or not path.is_file():
        return _empty_session_state()
    try:
        stat = path.stat()
        with _SESSION_STATE_LOCK:
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
    state["has_refresh_token"] = bool(
        oauth_refresh and _looks_codex_refresh_token(oauth_refresh)
    ) or _looks_codex_refresh_token(legacy_refresh)
    state["at_probe_status_code"] = _access_token_probe_status_code(data)
    _raw_auth_session: object = data.get("auth_session")
    auth_session: dict = _raw_auth_session if isinstance(_raw_auth_session, dict) else {}
    _raw_workspace: object = data.get("workspace_scan")
    workspace: dict = _raw_workspace if isinstance(_raw_workspace, dict) else {}
    state["account_type"] = _account_type(data, auth_session, workspace, data.get("access_token"))
    with _SESSION_STATE_LOCK:
        while len(_SESSION_STATE_CACHE) >= _SESSION_STATE_CACHE_LIMIT:
            _SESSION_STATE_CACHE.popitem(last=False)
        _SESSION_STATE_CACHE[str(path)] = (stat.st_mtime, stat.st_size, state)
    return dict(state)


def read_accounts(runtime_config: ConfigInput = None, *, include_session: bool = True) -> list[dict[str, Any]]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    return [
        _record_payload(row, include_session=include_session) for row in list_account_records(runtime_config=config)
    ]


def _as_epoch(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_accounts_delta(
    runtime_config: ConfigInput = None,
    *,
    since: int | None = None,
    include_session: bool = False,
) -> dict[str, Any]:
    """Accounts changed since ``since`` plus the complete current id set.

    ``accounts.updated_at`` is an epoch integer, so the cursor is simply the
    largest value in the table and a client can send it back verbatim. ``ids``
    is **always** the full id set -- that is what lets the desktop drop rows
    another process deleted without a second round trip, and it stays tiny
    (~3 KB for 434 accounts) next to the ~623 KB full payload.

    ``full`` tells the caller whether ``accounts`` is the whole table (no
    ``since``) or only the changed rows. The response is additive to the
    existing ``accounts`` list shape, so one-shot readers keep working.
    """
    config = resolve_runtime_config(runtime_config, workflow="storage")
    records = list_account_records(runtime_config=config)
    ids = [str(record.get("id") or "") for record in records]
    cursor = max((_as_epoch(record.get("updated_at")) for record in records), default=0)
    changed = (
        records if since is None else [record for record in records if _as_epoch(record.get("updated_at")) > since]
    )
    return {
        "accounts": [_record_payload(record, include_session=include_session) for record in changed],
        "ids": ids,
        "cursor": cursor,
        "full": since is None,
    }


def read_account(account_id: str = "", email: str = "", runtime_config: ConfigInput = None) -> dict[str, Any]:
    config = resolve_runtime_config(runtime_config, workflow="storage")
    row = get_account_record_by_id(account_id, runtime_config=config) if str(account_id or "").strip() else {}
    if not row and email:
        from .storage import get_account_record

        row = get_account_record(email, runtime_config=config)
    return _record_payload(row) if row else {}


_MAILBOX_POOL_CANDIDATE_NAMES = ("hotmail.txt", "chatai_mailbox.txt", "chatai.txt")


def read_proxy_pool(
    runtime_config: ConfigInput = None,
    *,
    lane: str = "protocol_registration",
) -> dict[str, Any]:
    """Return the configured pool for the desktop ProxyPool window.

    Delegates to :mod:`sms_tool.proxy.admin` (one owner for masking, cached geo
    and the health rank). Imported inside the function because the read path is
    the desktop's hot start path and must not pull the proxy admin stack in at
    module scope for the account-only reads.
    """
    config = resolve_runtime_config(runtime_config, workflow="storage")
    from .proxy.admin import list_pool

    return list_pool(config.data, lane=lane)


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
    partial = RegistrationRetryGuard(config.data).dead_end_emails()
    files = []
    for path in _known_mailbox_pool_files(config, extra_files, root, include_config_dir=root_dir is None):
        files.append(
            {
                "path": str(path),
                "name": path.name,
                "lines": _read_pool_lines(path),
            }
        )
    lines = [line for file in files for line in file["lines"]]
    try:
        records = get_account_records([line["email"] for line in lines], runtime_config=config)
    except Exception:
        records = {}
    records = {str(email).strip().casefold(): row for email, row in records.items()}
    for line in lines:
        email = str(line["email"]).strip().casefold()
        status = mailbox_registration_status(records.get(email), known_partial=email in partial)
        line.update(registration_status=status, registration_eligible=status == "unknown")
    return {"files": files}


def _known_mailbox_pool_files(
    config: RuntimeConfig,
    extra_files,
    root: Path,
    include_config_dir: bool = True,
) -> list[Path]:
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
    scan_roots = [root]
    if include_config_dir:
        add(resolve_config_file(token_file))
        user_config = config_dir()
        if user_config.resolve() != root.resolve():
            scan_roots.append(user_config)
    for scan_root in scan_roots:
        for name in _MAILBOX_POOL_CANDIDATE_NAMES:
            add(scan_root / name)
        if scan_root.is_dir():
            for path in sorted(scan_root.glob(_MAILBOX_POOL_GLOB)):
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
        account = parse_mailbox_pool_line(line, str(path), line_no)
        if account is None:
            continue
        lines.append({"line_no": line_no, **_pool_line_payload(account, line)})
    return lines


def _pool_line_payload(account, raw_line: str) -> dict[str, Any]:
    provider = str(account.provider or "").strip().lower()
    # chatai and Gmail-OAuth lines store the OAuth client id in the generic
    # token slot; surface it explicitly so callers need no format knowledge.
    client_id = (
        account.token if provider == "chatai" or (provider == "gmail" and account.auth_mode == "oauth_refresh") else ""
    )
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


def create_payment_url_file(
    account_id: str = "", email: str = "", runtime_config: ConfigInput = None
) -> dict[str, Any]:
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
        "email",
        "password",
        "success",
        "status",
        "error",
        "session_token",
        "access_token",
        "refresh_token",
        "oauth_refresh_token",
        "cookie_header",
        "device_id",
        "totp_secret",
        "auth_session_logging_id",
        "registration_country",
        "refresh_token_status",
        "payment_method",
    ):
        value = row.get(key)
        if value not in (None, ""):
            data[key] = value

    _raw_mailbox: object = data.get("mailbox")
    mailbox: dict = _raw_mailbox if isinstance(_raw_mailbox, dict) else {}
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

    _raw_payment: object = data.get("paypal")
    payment: dict = _raw_payment if isinstance(_raw_payment, dict) else {}
    payment = dict(payment)
    for key, column in (
        ("ok", "paypal_ok"),
        ("url", "paypal_url"),
        ("status", "paypal_status"),
        ("cs_id", "paypal_cs_id"),
        ("pm_id", "paypal_pm_id"),
        ("currency", "paypal_currency"),
        ("amount_due", "paypal_amount_due"),
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
        _raw_candidate: object = parsed
        candidate: dict = _raw_candidate if isinstance(_raw_candidate, dict) else {}
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
        # A pickup credential is mandatory.  Emitting a partial line such as
        # ``remail://email---order`` only defers the failure to the parser and
        # makes selected-mailbox registration look like a malformed-file bug.
        if not token:
            return ""
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
